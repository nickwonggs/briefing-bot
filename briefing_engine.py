"""
briefing_engine.py — Core data fetching, formatting, and task management.

Log format: [TIMESTAMP] [ACTION] [STATUS] — never personal data.
"""

import os
import json
import logging
import logging.handlers
import tempfile
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import base64

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_PATH = "briefing_bot.log"
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3
)
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s]"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("engine")

# ── Constants ──────────────────────────────────────────────────────────────────
TZ = ZoneInfo("Asia/Singapore")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks",
]
REQUIRED_ACCOUNT = "nickwys@sph.com.sg"
TASK_FILE = "task_state.json.enc"
AUTO_ARCHIVE_DAYS = 14

SPAM_PATTERNS = re.compile(
    r"(noreply@|no-reply@|notifications@|donotreply@|mailer@|"
    r"newsletter|unsubscribe|marketing|calendar-notification|"
    r"automated|do.not.reply)",
    re.IGNORECASE,
)

CALENDAR_TAGS = {
    "work":       "💼",
    "gym":        "🏋️",
    "personal":   "👤",
    "ryan chia":  "🤝",
    "vacation":   "🌴",
    "others":     "📌",
}

PERSONAL_CALENDAR_KEYWORDS = ("gym", "personal", "ryan chia", "vacation", "others")

# ── Credential helpers ─────────────────────────────────────────────────────────

def _get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if not key:
        log.error("[ENCRYPTION_KEY] [MISSING]")
        raise RuntimeError("ENCRYPTION_KEY not set")
    return Fernet(key.encode())


def _build_credentials() -> Credentials:
    """
    Reconstruct Google credentials from env vars into temp files, never disk.
    Returns a valid Credentials object.
    """
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    token_json = os.getenv("GOOGLE_TOKEN_JSON", "").strip()

    if not creds_json or not token_json:
        log.error("[GOOGLE_CREDENTIALS] [MISSING_ENV_VARS]")
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON or GOOGLE_TOKEN_JSON not set")

    # Write to temp files in the OS temp directory (not project dir)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        tf.write(token_json)
        token_path = tf.name

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    finally:
        os.unlink(token_path)

    if creds and creds.expired and creds.refresh_token:
        log.info("[TOKEN_REFRESH] [START]")
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        # Update token env var in memory (not persisted — Railway restarts pick up
        # the latest token from the OAuth refresh; user must re-run auth_setup.py
        # if refresh token expires)
        log.info("[TOKEN_REFRESH] [OK]")

    return creds


def _validate_account(service) -> None:
    """Abort if authenticated account is not the required work account."""
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    if email.lower() != REQUIRED_ACCOUNT.lower():
        log.error("[ACCOUNT_VALIDATION] [WRONG_ACCOUNT]")
        raise RuntimeError(f"Authenticated as wrong account — expected {REQUIRED_ACCOUNT}")
    log.info("[ACCOUNT_VALIDATION] [OK]")


# ── Task state (encrypted) ─────────────────────────────────────────────────────

def load_tasks() -> list[dict]:
    if not os.path.exists(TASK_FILE):
        return []
    try:
        f = _get_fernet()
        with open(TASK_FILE, "rb") as fh:
            return json.loads(f.decrypt(fh.read()).decode())
    except (InvalidToken, Exception):
        log.error("[LOAD_TASKS] [DECRYPT_FAIL]")
        return []


def save_tasks(tasks: list[dict]) -> None:
    try:
        f = _get_fernet()
        payload = json.dumps(tasks, ensure_ascii=False).encode()
        with open(TASK_FILE, "wb") as fh:
            fh.write(f.encrypt(payload))
        log.info("[SAVE_TASKS] [OK]")
    except Exception:
        log.error("[SAVE_TASKS] [FAIL]")


def _archive_old_done_tasks(tasks: list[dict]) -> list[dict]:
    cutoff = datetime.now(TZ) - timedelta(days=AUTO_ARCHIVE_DAYS)
    result = []
    for t in tasks:
        if t.get("status") == "done":
            done_at = t.get("done_at")
            if done_at:
                try:
                    dt = datetime.fromisoformat(done_at).replace(tzinfo=TZ)
                    if dt < cutoff:
                        continue  # silently archive
                except ValueError:
                    pass
        result.append(t)
    return result


def mark_task_done(task_name: str) -> Optional[str]:
    """
    Fuzzy match task name across both local task list and Google Tasks.
    Marks done in whichever source it finds a match. Returns matched title or None.
    """
    needle = task_name.lower().strip()

    # Try Google Tasks first
    google_match = _mark_google_task_done(needle)
    if google_match:
        return google_match

    # Fall back to local encrypted task list
    tasks = load_tasks()
    best = None
    best_score = 0
    for t in tasks:
        title = t.get("title", "").lower()
        score = len([w for w in needle.split() if w in title])
        if score > best_score:
            best_score = score
            best = t
    if best and best_score > 0:
        best["status"] = "done"
        best["done_at"] = datetime.now(TZ).isoformat()
        save_tasks(tasks)
        return best.get("title")
    return None


def _mark_google_task_done(needle: str) -> Optional[str]:
    """Find and complete a Google Task matching needle. Returns title or None."""
    try:
        creds = _build_credentials()
        service = build("tasks", "v1", credentials=creds)
        lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = lists_result.get("items", [])

        best_title = None
        best_score = 0
        best_list_id = None
        best_task_id = None

        for tl in task_lists:
            tl_id = tl["id"]
            tasks_result = service.tasks().list(
                tasklist=tl_id, showCompleted=False, maxResults=100
            ).execute()
            for task in tasks_result.get("items", []):
                title = task.get("title", "").lower()
                score = len([w for w in needle.split() if w in title])
                if score > best_score:
                    best_score = score
                    best_title = task.get("title")
                    best_list_id = tl_id
                    best_task_id = task["id"]

        if best_score > 0 and best_task_id:
            service.tasks().patch(
                tasklist=best_list_id,
                task=best_task_id,
                body={"status": "completed"},
            ).execute()
            log.info("[GOOGLE_TASK_DONE] [OK]")
            return best_title
    except Exception:
        log.error("[GOOGLE_TASK_DONE] [FAIL]")
    return None


def add_task(title: str, source: str = "Manual", task_type: str = "👤 Personal",
             due_date: Optional[str] = None) -> None:
    tasks = load_tasks()
    tasks.append({
        "title": title[:200],
        "status": "in_progress",
        "source": source,
        "type": task_type,
        "due_date": due_date,
        "created_at": datetime.now(TZ).isoformat(),
    })
    save_tasks(tasks)


# ── Google Tasks ──────────────────────────────────────────────────────────────

def fetch_google_tasks() -> list[dict]:
    """
    Fetch all incomplete tasks from Google Tasks.
    Returns list of dicts: {title, due, task_list, overdue, google_task: True}
    """
    log.info("[FETCH_GOOGLE_TASKS] [START]")
    try:
        creds = _build_credentials()
        service = build("tasks", "v1", credentials=creds)
        lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = lists_result.get("items", [])

        results = []
        now = datetime.now(TZ)

        for tl in task_lists:
            tl_id = tl["id"]
            tl_name = tl.get("title", "My Tasks")
            tasks_result = service.tasks().list(
                tasklist=tl_id,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            for task in tasks_result.get("items", []):
                due_str = task.get("due")
                due_date = None
                overdue = False
                if due_str:
                    try:
                        due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                        due_date = due_dt.strftime("%d %b")
                        overdue = due_dt.astimezone(TZ) < now
                    except ValueError:
                        pass
                results.append({
                    "title": task.get("title", "(no title)"),
                    "due": due_date,
                    "task_list": tl_name,
                    "overdue": overdue,
                    "google_task": True,
                })

        log.info("[FETCH_GOOGLE_TASKS] [OK]")
        return results
    except Exception:
        log.error("[FETCH_GOOGLE_TASKS] [FAIL]")
        return []


# ── Gmail ──────────────────────────────────────────────────────────────────────

def _is_spam(sender: str, subject: str) -> bool:
    return bool(SPAM_PATTERNS.search(sender) or SPAM_PATTERNS.search(subject))


def fetch_emails(since_hours: int = 24) -> dict:
    """
    Returns {"urgent": [...], "normal": [...], "deferred_count": int}
    Each item: {"sender": str, "subject": str, "is_unread": bool, "age_hours": float}
    """
    log.info("[FETCH_EMAILS] [START]")
    creds = _build_credentials()
    service = build("gmail", "v1", credentials=creds)
    _validate_account(service)

    cutoff = datetime.now(TZ) - timedelta(hours=since_hours)
    query = f"after:{int(cutoff.timestamp())}"

    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()
        messages = result.get("messages", [])
    except Exception:
        log.error("[FETCH_EMAILS] [API_FAIL]")
        return {"urgent": [], "normal": [], "deferred_count": 0}

    urgent = []
    normal = []

    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except Exception:
            continue

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        labels = msg.get("labelIds", [])
        is_unread = "UNREAD" in labels

        if _is_spam(sender, subject):
            continue

        # Age calculation
        internal_date_ms = int(msg.get("internalDate", 0))
        msg_time = datetime.fromtimestamp(internal_date_ms / 1000, tz=TZ)
        age_hours = (datetime.now(TZ) - msg_time).total_seconds() / 3600

        item = {
            "sender": sender,
            "subject": subject,
            "is_unread": is_unread,
            "age_hours": round(age_hours, 1),
        }

        if is_unread and age_hours > 24:
            urgent.append(item)
        else:
            normal.append(item)

    log.info("[FETCH_EMAILS] [OK]")
    return {"urgent": urgent, "normal": normal, "deferred_count": 0}


def fetch_emails_weekend() -> dict:
    """Weekend version: scan but return deferred count only."""
    result = fetch_emails(since_hours=48)
    total = len(result["urgent"]) + len(result["normal"])
    return {"urgent": [], "normal": [], "deferred_count": total}


# ── Calendar ──────────────────────────────────────────────────────────────────

def _tag_calendar(name: str) -> str:
    lower = name.lower()
    for key, tag in CALENDAR_TAGS.items():
        if key in lower:
            return tag
    return "📌"


def _is_personal_calendar(name: str) -> bool:
    lower = name.lower()
    return any(k in lower for k in PERSONAL_CALENDAR_KEYWORDS)


def fetch_calendar_events(target_date: date, is_weekend: bool) -> list[dict]:
    """
    Returns list of events for target_date.
    is_weekend=True → personal calendars only (enforced at API query level).
    Each event: {"time": str, "title": str, "calendar": str, "tag": str, "all_day": bool}
    """
    log.info("[FETCH_CALENDAR] [START]")
    creds = _build_credentials()
    service = build("calendar", "v3", credentials=creds)

    start_dt = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=TZ)
    end_dt = start_dt + timedelta(days=1)
    time_min = start_dt.isoformat()
    time_max = end_dt.isoformat()

    try:
        cal_list = service.calendarList().list().execute()
        calendars = cal_list.get("items", [])
    except Exception:
        log.error("[FETCH_CALENDAR] [CAL_LIST_FAIL]")
        return []

    events = []
    for cal in calendars:
        cal_name = cal.get("summary", "")
        cal_id = cal.get("id", "")

        # Weekend enforcement: skip work calendar entirely at API level
        if is_weekend and not _is_personal_calendar(cal_name):
            if "work" in cal_name.lower() or cal_id.lower() == REQUIRED_ACCOUNT.lower():
                log.info("[FETCH_CALENDAR] [SKIP_WORK_WEEKEND]")
                continue

        tag = _tag_calendar(cal_name)

        try:
            ev_result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            ev_items = ev_result.get("items", [])
        except Exception:
            log.error(f"[FETCH_CALENDAR] [EVENTS_FAIL]")
            continue

        for ev in ev_items:
            start = ev.get("start", {})
            all_day = "date" in start and "dateTime" not in start

            if all_day:
                time_str = "All day"
            else:
                dt = datetime.fromisoformat(start.get("dateTime", "")).astimezone(TZ)
                time_str = dt.strftime("%I:%M %p")

            events.append({
                "time": time_str,
                "title": ev.get("summary", "(No title)"),
                "calendar": cal_name,
                "tag": tag,
                "all_day": all_day,
            })

    events.sort(key=lambda e: (e["time"] == "All day", e["time"]))
    log.info("[FETCH_CALENDAR] [OK]")
    return events


# ── Conflict detection ─────────────────────────────────────────────────────────

def _detect_conflicts(events: list[dict]) -> list[str]:
    """Flag back-to-back timed events. All-day events excluded per spec."""
    flags = []
    timed = [e for e in events if not e["all_day"]]
    for i in range(len(timed) - 1):
        flags.append(
            f"Back-to-back: {timed[i]['title']} → {timed[i+1]['title']}"
        )
    return flags


# ── Auto-task extraction ───────────────────────────────────────────────────────

_ACTION_KEYWORDS = re.compile(
    r"\b(please|action required|deadline|by [A-Z][a-z]+day|asap|urgent|"
    r"follow.up|review|approve|confirm|submit|send|complete)\b",
    re.IGNORECASE,
)


def extract_auto_tasks(emails: list[dict]) -> list[dict]:
    """Extract potential tasks from email subjects. Returns list of task dicts."""
    new_tasks = []
    for email in emails:
        subject = email.get("subject", "")
        if _ACTION_KEYWORDS.search(subject):
            new_tasks.append({
                "title": subject[:200],
                "status": "in_progress",
                "source": "Email (auto)",
                "type": "💼 Work",
                "due_date": None,
                "created_at": datetime.now(TZ).isoformat(),
                "auto_generated": True,
            })
    return new_tasks


# ── Briefing formatters ────────────────────────────────────────────────────────

def _format_task_list(tasks: list[dict], google_tasks: list[dict] = None,
                      weekend: bool = False) -> str:
    if weekend:
        tasks = [t for t in tasks if "personal" in t.get("type", "").lower()]

    done = [t for t in tasks if t.get("status") == "done"]
    in_prog = [t for t in tasks if t.get("status") == "in_progress"]
    overdue_local = [t for t in tasks if t.get("status") == "overdue"]
    auto_gen = [t for t in in_prog if t.get("auto_generated")]
    in_prog_normal = [t for t in in_prog if not t.get("auto_generated")]

    lines = ["✅ Task Status"]
    lines.append("🟢 Completed: " + (", ".join(t["title"] for t in done) or "None"))
    lines.append("🟡 In progress: " + (", ".join(t["title"] for t in in_prog_normal) or "None"))
    lines.append("🔴 Overdue / urgent: " + (", ".join(t["title"] for t in overdue_local) or "None"))
    if auto_gen:
        lines.append("🆕 New (auto-generated — please confirm): " +
                      ", ".join(t["title"] for t in auto_gen))

    if google_tasks:
        lines.append("")
        lines.append("📋 Google Tasks")
        overdue_gt = [t for t in google_tasks if t.get("overdue")]
        pending_gt = [t for t in google_tasks if not t.get("overdue")]
        if overdue_gt:
            for t in overdue_gt:
                due = f" (due {t['due']})" if t.get("due") else ""
                lines.append(f"🔴 {t['title']}{due} [{t['task_list']}]")
        for t in pending_gt:
            due = f" (due {t['due']})" if t.get("due") else ""
            lines.append(f"🟡 {t['title']}{due} [{t['task_list']}]")

    return "\n".join(lines)


def _top_priorities(tasks: list[dict], google_tasks: list[dict],
                    events: list[dict]) -> str:
    urgent_local = [t for t in tasks if t.get("status") == "overdue"]
    in_prog = [t for t in tasks if t.get("status") == "in_progress"
               and not t.get("auto_generated")]
    overdue_gt = [t for t in google_tasks if t.get("overdue")]

    combined = (overdue_gt + urgent_local + in_prog)[:3]
    lines = []
    for i, p in enumerate(combined, 1):
        lines.append(f"{i}. {p['title']}")
    while len(lines) < 3:
        lines.append(f"{len(lines)+1}. (Add manually)")
    return "\n".join(lines)


def build_evening_briefing(target_date: date) -> str:
    now = datetime.now(TZ)
    today = now.date()
    weekday = today.weekday()  # 0=Mon … 6=Sun
    is_weekend_target = target_date.weekday() >= 5

    # Email fetch
    if weekday == 4:  # Friday evening → brief Saturday
        email_data = fetch_emails_weekend()
    elif weekday >= 5:  # Weekend evening
        email_data = fetch_emails_weekend()
    else:
        email_data = fetch_emails(since_hours=24)

    # Calendar fetch
    events = fetch_calendar_events(target_date, is_weekend=is_weekend_target)

    # Tasks
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    # Auto-task extraction from urgent emails
    if email_data["urgent"]:
        new_auto = extract_auto_tasks(email_data["urgent"])
        if new_auto:
            tasks.extend(new_auto)
            save_tasks(tasks)

    # Flags
    conflict_flags = _detect_conflicts(events)
    gym_events = [e for e in events if "🏋️" in e["tag"]]
    vacation_events = [e for e in events if "🌴" in e["tag"]]
    ryan_events = [e for e in events if "🤝" in e["tag"]]

    day_label = target_date.strftime("%A, %d %B %Y")
    today_label = today.strftime("%A, %d %B %Y")

    lines = [
        f"📅 *Evening Prep — {today_label}*",
        f"_Preparing you for {day_label}_",
        "",
        "📬 *Email Summary*",
    ]

    for e in email_data["urgent"]:
        lines.append(f"🔴 {e['sender']} — {e['subject']} _(action required)_")
    for e in email_data["normal"]:
        lines.append(f"• {e['sender']} — {e['subject']}")
    if email_data["deferred_count"]:
        lines.append(f"📥 {email_data['deferred_count']} work emails received — deferred to Monday")
    if not email_data["urgent"] and not email_data["normal"] and not email_data["deferred_count"]:
        lines.append("No new emails.")

    lines += ["", "🗓️ *Tomorrow's Schedule*"]
    if events:
        for ev in events:
            note = ""
            if "🏋️" in ev["tag"]:
                note = " — 🔒 Protected block"
            elif "🌴" in ev["tag"]:
                note = " — 🌴 You're on leave"
            elif "🤝" in ev["tag"]:
                note = " — Joint commitment"
            lines.append(f"{ev['time']} — {ev['title']} {ev['tag']}{note}")
    else:
        lines.append("No events scheduled.")

    lines += ["", "⚠️ *Flags*"]
    if conflict_flags:
        for f in conflict_flags:
            lines.append(f"• {f}")
    if gym_events:
        lines.append(f"• 🏋️ Gym block protected — never schedule over this")
    if vacation_events:
        lines.append(f"• 🌴 You are on leave — adjust plans accordingly")
    if ryan_events:
        for e in ryan_events:
            lines.append(f"• 🤝 Joint commitment with Ryan Chia: {e['title']}")
    if not conflict_flags and not gym_events and not vacation_events and not ryan_events:
        lines.append("• No flags.")

    lines += ["", _format_task_list(tasks, google_tasks, weekend=is_weekend_target)]
    lines += ["", "🎯 *Top 3 Priorities for Tomorrow*",
              _top_priorities(tasks, google_tasks, events)]

    return "\n".join(lines)


def build_morning_briefing() -> str:
    now = datetime.now(TZ)
    today = now.date()
    weekday = today.weekday()
    is_weekend = weekday >= 5

    email_data = fetch_emails(since_hours=12)  # overnight only
    events = fetch_calendar_events(today, is_weekend=is_weekend)
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    if email_data["urgent"]:
        new_auto = extract_auto_tasks(email_data["urgent"])
        if new_auto:
            tasks.extend(new_auto)
            save_tasks(tasks)

    conflict_flags = _detect_conflicts(events)
    gym_events = [e for e in events if "🏋️" in e["tag"]]
    vacation_events = [e for e in events if "🌴" in e["tag"]]
    ryan_events = [e for e in events if "🤝" in e["tag"]]

    today_label = today.strftime("%A, %d %B %Y")

    lines = [
        f"☀️ *Morning Refresh — {today_label}*",
        "",
        "📬 *Overnight Emails*",
    ]

    for e in email_data["urgent"]:
        lines.append(f"🔴 {e['sender']} — {e['subject']} _(action required)_")
    for e in email_data["normal"]:
        lines.append(f"• {e['sender']} — {e['subject']}")
    if not email_data["urgent"] and not email_data["normal"]:
        lines.append("No overnight emails.")

    lines += ["", "🗓️ *Today's Schedule*"]
    if events:
        for ev in events:
            note = ""
            if "🏋️" in ev["tag"]:
                note = " — 🔒 Protected block"
            elif "🌴" in ev["tag"]:
                note = " — 🌴 You're on leave"
            elif "🤝" in ev["tag"]:
                note = " — Joint commitment"
            lines.append(f"{ev['time']} — {ev['title']} {ev['tag']}{note}")
    else:
        lines.append("No events today.")

    lines += ["", "⚠️ *Flags*"]
    if conflict_flags:
        for f in conflict_flags:
            lines.append(f"• {f}")
    if gym_events:
        lines.append(f"• 🏋️ Gym block protected")
    if vacation_events:
        lines.append(f"• 🌴 You are on leave")
    if ryan_events:
        for e in ryan_events:
            lines.append(f"• 🤝 Joint commitment with Ryan Chia: {e['title']}")
    if not conflict_flags and not gym_events and not vacation_events and not ryan_events:
        lines.append("• No flags.")

    lines += ["", _format_task_list(tasks, google_tasks, weekend=is_weekend)]

    # Morning shows updated task list with auto-generated section
    auto_tasks = [t for t in tasks if t.get("auto_generated") and t.get("status") == "in_progress"]
    if auto_tasks:
        lines.append("🆕 *New (auto-generated from emails — please confirm):*")
        for t in auto_tasks:
            lines.append(f"  • {t['title']}")

    lines += ["", "🎯 *Focus for Today*", _top_priorities(tasks, google_tasks, events)]

    return "\n".join(lines)


def build_weekend_briefing() -> str:
    """Personal-only schedule for today."""
    now = datetime.now(TZ)
    today = now.date()
    events = fetch_calendar_events(today, is_weekend=True)
    today_label = today.strftime("%A, %d %B %Y")

    lines = [f"📅 *Weekend View — {today_label}*", ""]
    if events:
        for ev in events:
            lines.append(f"{ev['time']} — {ev['title']} {ev['tag']}")
    else:
        lines.append("Nothing scheduled today. Enjoy your day!")
    return "\n".join(lines)


def process_update(text: str) -> str:
    """
    Parse a /update message. Returns a reply string.
    Input is already sanitised before this is called.
    """
    tasks = load_tasks()
    marked_done = []
    added = []
    notes = []

    # Simple keyword parsing
    for part in re.split(r",\s*", text):
        part = part.strip()
        if re.match(r"^(finished|completed|done)\s+", part, re.IGNORECASE):
            task_name = re.sub(r"^(finished|completed|done)\s+", "", part, flags=re.IGNORECASE)
            matched = mark_task_done(task_name)
            if matched:
                marked_done.append(matched)
        elif re.match(r"^(new task|add|task)\s+", part, re.IGNORECASE):
            task_title = re.sub(r"^(new task|add|task)\s+", "", part, flags=re.IGNORECASE)
            if task_title:
                add_task(task_title[:200], source="Manual")
                added.append(task_title[:200])
        elif part:
            notes.append(part)

    parts = []
    if marked_done:
        parts.append(f"marked {', '.join(marked_done)} done")
    if added:
        parts.append(f"added {', '.join(added)}")
    if notes:
        parts.append(f"noted: {', '.join(notes)}")

    if parts:
        return "Got it — " + ", ".join(parts) + ". Task list updated. ✅"
    return "No changes detected. Try: 'finished X, new task Y, FYI Z'"


def get_task_list_text(weekend: bool = False) -> str:
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()
    if not tasks and not google_tasks:
        return "No tasks yet. Use /add to create one."
    return _format_task_list(tasks, google_tasks, weekend=weekend)


def get_next_briefing_target() -> date:
    """Return the date the evening briefing should cover."""
    now = datetime.now(TZ)
    today = now.date()
    weekday = today.weekday()

    if weekday == 4:  # Friday → Saturday
        return today + timedelta(days=1)
    elif weekday == 5:  # Saturday → Sunday
        return today + timedelta(days=1)
    elif weekday == 6:  # Sunday → Monday
        return today + timedelta(days=1)
    else:  # Mon–Thu → next day
        return today + timedelta(days=1)

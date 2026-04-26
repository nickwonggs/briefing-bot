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

log = logging.getLogger("engine")

# ── Constants ──────────────────────────────────────────────────────────────────
TZ = ZoneInfo("Asia/Singapore")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks",
]
PERSONAL_TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks"]
REQUIRED_ACCOUNT = "nickwys@sph.com.sg"
_DATA_DIR = os.getenv("TASK_DATA_DIR", ".")
TASK_FILE = os.path.join(_DATA_DIR, "task_state.json.enc")
AUTO_ARCHIVE_DAYS = 14

# Filters out newsletters, auto-notifications, calendar invites, and acceptances/declines
SPAM_PATTERNS = re.compile(
    r"(noreply@|no-reply@|notifications@|donotreply@|mailer@|"
    r"newsletter|unsubscribe|marketing|calendar-notification|invites-noreply@|"
    r"automated|do.not.reply|"
    r"accepted this event|declined this event|tentatively accepted|"
    r"(?:accepted|declined|tentative): |invitation: |updated invitation)",
    re.IGNORECASE,
)

CALENDAR_TAGS = {
    "work":      "💼",
    "gym":       "🏋️",
    "personal":  "👤",
    "ryan chia": "🤝",
    "vacation":  "🌴",
    "others":    "📌",
}

# Only these calendars are shown — everything else (Birthdays, Holidays, etc.) is skipped
CALENDAR_WHITELIST_KEYWORDS = ("gym", "personal", "ryan chia", "vacation", "others")

PERSONAL_CALENDAR_KEYWORDS = ("gym", "personal", "ryan chia", "vacation", "others")

# ── Credential helpers ─────────────────────────────────────────────────────────

def _get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if not key:
        log.error("[ENCRYPTION_KEY] [MISSING]")
        raise RuntimeError("ENCRYPTION_KEY not set")
    return Fernet(key.encode())


def _build_credentials() -> Credentials:
    """Reconstruct work account credentials from env vars. Never persists to disk."""
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    token_json = os.getenv("GOOGLE_TOKEN_JSON", "").strip()

    if not creds_json or not token_json:
        log.error("[GOOGLE_CREDENTIALS] [MISSING_ENV_VARS]")
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON or GOOGLE_TOKEN_JSON not set")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        tf.write(token_json)
        token_path = tf.name

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    finally:
        os.unlink(token_path)

    if creds and creds.expired and creds.refresh_token:
        log.info("[TOKEN_REFRESH] [START]")
        creds.refresh(google.auth.transport.requests.Request())
        log.info("[TOKEN_REFRESH] [OK]")

    return creds


def _build_personal_credentials() -> Optional[Credentials]:
    """
    Reconstruct wongnicholas98@gmail.com credentials for Google Tasks.
    Returns None if GOOGLE_TOKEN_JSON_PERSONAL is not set.
    """
    token_json = os.getenv("GOOGLE_TOKEN_JSON_PERSONAL", "").strip()
    if not token_json:
        return None

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        tf.write(token_json)
        token_path = tf.name

    try:
        creds = Credentials.from_authorized_user_file(token_path, PERSONAL_TASKS_SCOPES)
    finally:
        os.unlink(token_path)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        log.info("[PERSONAL_TOKEN_REFRESH] [OK]")

    return creds if creds and creds.valid else None


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
                        continue
                except ValueError:
                    pass
        result.append(t)
    return result


def mark_task_done(task_name: str) -> Optional[str]:
    """Fuzzy match across Google Tasks (both accounts) and local list. Returns matched title."""
    needle = task_name.lower().strip()

    google_match = _mark_google_task_done(needle)
    if google_match:
        return google_match

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
    """Search both accounts' Google Tasks and complete the best match."""
    best_title = None
    best_score = 0
    best_list_id = None
    best_task_id = None
    best_service = None

    def _search_service(service):
        nonlocal best_title, best_score, best_list_id, best_task_id, best_service
        try:
            for tl in service.tasklists().list(maxResults=20).execute().get("items", []):
                for task in service.tasks().list(
                    tasklist=tl["id"], showCompleted=False, maxResults=100
                ).execute().get("items", []):
                    title = task.get("title", "").lower()
                    score = len([w for w in needle.split() if w in title])
                    if score > best_score:
                        best_score = score
                        best_title = task.get("title")
                        best_list_id = tl["id"]
                        best_task_id = task["id"]
                        best_service = service
        except Exception:
            log.error("[MARK_GOOGLE_TASK] [SEARCH_FAIL]")

    try:
        _search_service(build("tasks", "v1", credentials=_build_credentials()))
    except Exception:
        log.error("[MARK_GOOGLE_TASK] [WORK_CREDS_FAIL]")

    personal_creds = _build_personal_credentials()
    if personal_creds:
        try:
            _search_service(build("tasks", "v1", credentials=personal_creds))
        except Exception:
            log.error("[MARK_GOOGLE_TASK] [PERSONAL_CREDS_FAIL]")

    if best_score > 0 and best_task_id and best_service:
        try:
            best_service.tasks().patch(
                tasklist=best_list_id,
                task=best_task_id,
                body={"status": "completed"},
            ).execute()
            log.info("[GOOGLE_TASK_DONE] [OK]")
            return best_title
        except Exception:
            log.error("[GOOGLE_TASK_DONE] [PATCH_FAIL]")
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

def _fetch_tasks_for_service(service, label: str) -> list[dict]:
    """Fetch incomplete tasks from one Tasks API service instance."""
    results = []
    now = datetime.now(TZ)
    try:
        task_lists = service.tasklists().list(maxResults=20).execute().get("items", [])
        for tl in task_lists:
            tl_id = tl["id"]
            tl_name = tl.get("title", "My Tasks")
            for task in service.tasks().list(
                tasklist=tl_id,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute().get("items", []):
                title = task.get("title", "").strip()
                if not title:
                    continue
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
                    "title": title,
                    "due": due_date,
                    "task_list": f"{tl_name} ({label})",
                    "overdue": overdue,
                    "google_task": True,
                })
    except Exception:
        log.error(f"[FETCH_GOOGLE_TASKS] [{label}] [FAIL]")
    return results


def fetch_google_tasks() -> list[dict]:
    """Fetch incomplete tasks from both work and personal Google accounts."""
    log.info("[FETCH_GOOGLE_TASKS] [START]")
    results = []

    # Work account (nickwys@sph.com.sg)
    try:
        creds = _build_credentials()
        results.extend(_fetch_tasks_for_service(build("tasks", "v1", credentials=creds), "Work"))
    except Exception:
        log.error("[FETCH_GOOGLE_TASKS] [WORK] [FAIL]")

    # Personal account (wongnicholas98@gmail.com) — only if token is configured
    personal_creds = _build_personal_credentials()
    if personal_creds:
        try:
            results.extend(_fetch_tasks_for_service(
                build("tasks", "v1", credentials=personal_creds), "Personal"
            ))
        except Exception:
            log.error("[FETCH_GOOGLE_TASKS] [PERSONAL] [FAIL]")
    else:
        log.info("[FETCH_GOOGLE_TASKS] [PERSONAL] [NO_TOKEN_CONFIGURED]")

    log.info("[FETCH_GOOGLE_TASKS] [OK]")
    return results


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


def _is_whitelisted_calendar(cal_name: str, cal_id: str) -> bool:
    """Return True only for the 6 approved calendars. All others are ignored."""
    if cal_id.lower() == REQUIRED_ACCOUNT.lower():
        return True  # Work calendar
    lower = cal_name.lower()
    return any(k in lower for k in CALENDAR_WHITELIST_KEYWORDS)


def fetch_calendar_events(target_date: date, is_weekend: bool) -> list[dict]:
    """
    Returns sorted list of events for target_date from whitelisted calendars only.
    is_weekend=True → Work calendar skipped entirely at API level.
    Each event: {time, title, calendar, tag, all_day, start_dt, end_dt}
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

        # Whitelist: only the 6 approved calendars
        if not _is_whitelisted_calendar(cal_name, cal_id):
            continue

        # Weekend enforcement: skip Work calendar at API level
        if is_weekend and cal_id.lower() == REQUIRED_ACCOUNT.lower():
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
            log.error("[FETCH_CALENDAR] [EVENTS_FAIL]")
            continue

        for ev in ev_items:
            # Skip events without a title
            title = ev.get("summary", "").strip()
            if not title:
                continue

            # Skip events the user has declined
            declined = any(
                a.get("self") and a.get("responseStatus") == "declined"
                for a in ev.get("attendees", [])
            )
            if declined:
                continue

            start = ev.get("start", {})
            end = ev.get("end", {})
            all_day = "date" in start and "dateTime" not in start

            if all_day:
                time_str = "All day"
                start_dt_obj = None
                end_dt_obj = None
            else:
                start_dt_obj = datetime.fromisoformat(start.get("dateTime", "")).astimezone(TZ)
                time_str = start_dt_obj.strftime("%I:%M %p")
                end_str = end.get("dateTime", "")
                end_dt_obj = (
                    datetime.fromisoformat(end_str).astimezone(TZ)
                    if end_str else start_dt_obj + timedelta(hours=1)
                )

            events.append({
                "time": time_str,
                "title": title,
                "calendar": cal_name,
                "tag": tag,
                "all_day": all_day,
                "start_dt": start_dt_obj,
                "end_dt": end_dt_obj,
                "description": ev.get("description", "").strip(),
                "attachments": ev.get("attachments", []),
            })

    # Sort all-day events first, then timed events by actual start time
    events.sort(key=lambda e: (
        e["start_dt"] is not None,  # all-day (start_dt=None) sorts first
        e["start_dt"] if e["start_dt"] else datetime.min.replace(tzinfo=TZ),
    ))
    log.info("[FETCH_CALENDAR] [OK]")
    return events


# ── Conflict detection ─────────────────────────────────────────────────────────

def _detect_conflicts(events: list[dict]) -> list[str]:
    """Flag overlapping or back-to-back (<15 min gap) timed events. All-day excluded."""
    flags = []
    timed = [e for e in events if not e["all_day"] and e.get("start_dt") and e.get("end_dt")]
    for i in range(len(timed) - 1):
        gap_min = (timed[i + 1]["start_dt"] - timed[i]["end_dt"]).total_seconds() / 60
        if gap_min < 0:
            flags.append(f"Overlap: {timed[i]['title']} → {timed[i+1]['title']}")
        elif gap_min < 15:
            flags.append(f"Back-to-back: {timed[i]['title']} → {timed[i+1]['title']}")
    return flags


# ── Auto-task extraction ───────────────────────────────────────────────────────

_ACTION_KEYWORDS = re.compile(
    r"\b(please|action required|deadline|by [A-Z][a-z]+day|asap|urgent|"
    r"follow.up|review|approve|confirm|submit|send|complete)\b",
    re.IGNORECASE,
)

# Matches action items in event descriptions — bullet points or lines with action verbs
_EVENT_ACTION_RE = re.compile(
    r"(?m)^\s*[-•*\d.)]*\s*"
    r"(?:action item|action|todo|to-do|to do|follow.?up|please|"
    r"deadline|action required|prep|prepare|review|submit|send|complete)"
    r"[:\s]+(.{5,200})",
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


def extract_tasks_from_events(events: list[dict]) -> list[dict]:
    """
    Parse event descriptions for action items and distil them into tasks.
    Strips HTML tags (Google Calendar sometimes sends rich-text descriptions).
    Attachment content is not read — Drive API scope would be required for that.
    """
    new_tasks = []
    for ev in events:
        desc = ev.get("description", "")
        if not desc:
            continue
        desc_plain = re.sub(r"<[^>]+>", " ", desc)  # strip HTML
        desc_plain = re.sub(r"&[a-z]+;", " ", desc_plain)  # strip HTML entities
        for match in _EVENT_ACTION_RE.finditer(desc_plain):
            item = match.group(1).strip()[:200]
            if item:
                new_tasks.append({
                    "title": f"[{ev['title']}] {item}",
                    "status": "in_progress",
                    "source": "Calendar (auto)",
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
        if pending_gt:
            for t in pending_gt:
                due = f" (due {t['due']})" if t.get("due") else ""
                lines.append(f"🟡 {t['title']}{due} [{t['task_list']}]")
        if not overdue_gt and not pending_gt:
            lines.append("No pending Google Tasks.")

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

    if weekday >= 4:  # Friday or weekend evening
        email_data = fetch_emails_weekend()
    else:
        email_data = fetch_emails(since_hours=24)

    events = fetch_calendar_events(target_date, is_weekend=is_weekend_target)
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    # Auto-tasks from urgent emails
    if email_data["urgent"]:
        new_auto = extract_auto_tasks(email_data["urgent"])
        if new_auto:
            tasks.extend(new_auto)
            save_tasks(tasks)

    # Auto-tasks from meeting descriptions
    event_tasks = extract_tasks_from_events(events)
    if event_tasks:
        tasks.extend(event_tasks)
        save_tasks(tasks)

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
            # Flag if description has action items or attachments
            desc_plain = re.sub(r"<[^>]+>", " ", ev.get("description", ""))
            if _EVENT_ACTION_RE.search(desc_plain):
                note += " — 📋 prep items in tasks"
            if ev.get("attachments"):
                att = ", ".join(a.get("title", "file") for a in ev["attachments"][:2])
                note += f" — 📎 {att}"
            lines.append(f"{ev['time']} — {ev['title']} {ev['tag']}{note}")
    else:
        lines.append("No events scheduled.")

    lines += ["", "⚠️ *Flags*"]
    if conflict_flags:
        for f in conflict_flags:
            lines.append(f"• {f}")
    if gym_events:
        lines.append("• 🏋️ Gym block protected — never schedule over this")
    if vacation_events:
        lines.append("• 🌴 You are on leave — adjust plans accordingly")
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

    email_data = fetch_emails(since_hours=12)
    events = fetch_calendar_events(today, is_weekend=is_weekend)
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    if email_data["urgent"]:
        new_auto = extract_auto_tasks(email_data["urgent"])
        if new_auto:
            tasks.extend(new_auto)
            save_tasks(tasks)

    event_tasks = extract_tasks_from_events(events)
    if event_tasks:
        tasks.extend(event_tasks)
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
            desc_plain = re.sub(r"<[^>]+>", " ", ev.get("description", ""))
            if _EVENT_ACTION_RE.search(desc_plain):
                note += " — 📋 prep items in tasks"
            if ev.get("attachments"):
                att = ", ".join(a.get("title", "file") for a in ev["attachments"][:2])
                note += f" — 📎 {att}"
            lines.append(f"{ev['time']} — {ev['title']} {ev['tag']}{note}")
    else:
        lines.append("No events today.")

    lines += ["", "⚠️ *Flags*"]
    if conflict_flags:
        for f in conflict_flags:
            lines.append(f"• {f}")
    if gym_events:
        lines.append("• 🏋️ Gym block protected")
    if vacation_events:
        lines.append("• 🌴 You are on leave")
    if ryan_events:
        for e in ryan_events:
            lines.append(f"• 🤝 Joint commitment with Ryan Chia: {e['title']}")
    if not conflict_flags and not gym_events and not vacation_events and not ryan_events:
        lines.append("• No flags.")

    lines += ["", _format_task_list(tasks, google_tasks, weekend=is_weekend)]

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
    """Parse a /update message. Input is already sanitised before this is called."""
    tasks = load_tasks()
    marked_done = []
    added = []
    notes = []

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
    return today + timedelta(days=1)

"""
briefing_engine.py — Core data fetching, formatting, and task management.

Log format: [TIMESTAMP] [ACTION] [STATUS] — never personal data.
All user-supplied content is HTML-escaped before inclusion in briefing output.
"""

import html as _html
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
PERSONAL_TASKS_SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
]
REQUIRED_ACCOUNT = "nickwys@sph.com.sg"
_DATA_DIR = os.getenv("TASK_DATA_DIR", ".")
TASK_FILE = os.path.join(_DATA_DIR, "task_state.json.enc")
AUTO_ARCHIVE_DAYS = 14

# Filters newsletters, auto-notifications, calendar invites, acceptances, and calendar shares
SPAM_PATTERNS = re.compile(
    r"(noreply@|no-reply@|notifications@|donotreply@|mailer@|"
    r"newsletter|unsubscribe|marketing|calendar-notification|invites-noreply@|"
    r"automated|do.not.reply|"
    r"accepted this event|declined this event|tentatively accepted|"
    r"(?:accepted|declined|tentative): |invitation: |updated invitation|"
    r"has shared a calendar|shared their calendar|invited you to view|"
    r"calendar sharing|shared calendar)",
    re.IGNORECASE,
)

# Out-of-office patterns matched against Work calendar all-day event titles
_OOO_RE = re.compile(
    r"\b(out[\s\-]of[\s\-]office|ooo|annual leave|on leave|day off|"
    r"public holiday|medical leave|mc|sick leave|no work|off[\s\-]day|leave)\b",
    re.IGNORECASE,
)


def _is_ooo_day(events: list[dict]) -> bool:
    """Return True if there is a Work calendar all-day OOO event in this event list."""
    for ev in events:
        if "💼" not in ev.get("tag", ""):
            continue
        if ev.get("all_day") and _OOO_RE.search(ev.get("title", "")):
            log.info("[OOO_DETECTED] [WORK_CALENDAR]")
            return True
    return False


CALENDAR_TAGS = {
    "work":      "💼",
    "gym":       "🏋️",
    "personal":  "👤",
    "ryan chia": "🤝",
    "vacation":  "🌴",
    "others":    "📌",
}

# Only these calendars are shown — everything else (Birthdays, Holidays, etc.) is ignored
CALENDAR_WHITELIST_KEYWORDS = ("gym", "personal", "ryan chia", "vacation", "others")
PERSONAL_CALENDAR_KEYWORDS = ("gym", "personal", "ryan chia", "vacation", "others")


def _h(text) -> str:
    """HTML-escape user-supplied content for safe Telegram HTML parse mode."""
    return _html.escape(str(text))


# ── Priority helpers ───────────────────────────────────────────────────────────

_PRIORITY_EMOJI  = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}
_PRIORITY_ORDER  = {"P0": 0,    "P1": 1,    "P2": 2,    "P3": 3}


# ── Date / recurrence parsers ──────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_DAY_MAP = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_RRULE_DAY_MAP = {
    "monday": "MO", "mon": "MO", "tuesday": "TU", "tue": "TU",
    "wednesday": "WE", "wed": "WE", "thursday": "TH", "thu": "TH",
    "friday": "FR", "fri": "FR", "saturday": "SA", "sat": "SA",
    "sunday": "SU", "sun": "SU",
}


def parse_task_date(text: str) -> Optional[date]:
    """
    Parse common natural-language date expressions into a date object (SGT-aware).
    Supported: today, tomorrow/tmr, friday, next monday, in 3 days,
               5 may, may 5, 1st jan, 2026-05-01, 5/5, 5/5/2026
    """
    if not text:
        return None
    t = text.lower().strip().rstrip(".,")
    today = datetime.now(TZ).date()

    if t in ("today",):
        return today
    if t in ("tomorrow", "tmr", "tom"):
        return today + timedelta(days=1)
    if t == "next week":
        return today + timedelta(weeks=1)

    # "in N days / weeks / months"
    m = re.match(r"^in (\d+) (day|days|week|weeks|month|months)$", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if "week" in unit:
            return today + timedelta(weeks=n)
        if "month" in unit:
            return today + timedelta(days=n * 30)
        return today + timedelta(days=n)

    # Day name (strip optional "next"/"this" prefix)
    day_text = t
    for prefix in ("next ", "this "):
        if t.startswith(prefix):
            day_text = t[len(prefix):]
    if day_text in _DAY_MAP:
        target = _DAY_MAP[day_text]
        ahead = (target - today.weekday()) % 7 or 7  # always future
        return today + timedelta(days=ahead)

    # "DD [month]" e.g. "5 may", "1st jan"
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)$", t)
    if m:
        month_idx = _MONTH_MAP.get(m.group(2)) or _MONTH_MAP.get(m.group(2)[:3])
        if month_idx:
            try:
                d = date(today.year, month_idx, int(m.group(1)))
                return d if d >= today else date(today.year + 1, month_idx, int(m.group(1)))
            except ValueError:
                pass

    # "[month] DD" e.g. "may 5"
    m = re.match(r"^([a-z]+)\s+(\d{1,2})$", t)
    if m:
        month_idx = _MONTH_MAP.get(m.group(1)) or _MONTH_MAP.get(m.group(1)[:3])
        if month_idx:
            try:
                d = date(today.year, month_idx, int(m.group(2)))
                return d if d >= today else date(today.year + 1, month_idx, int(m.group(2)))
            except ValueError:
                pass

    # DD/MM or DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?$", t)
    if m:
        dy, mo = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else today.year
        if yr < 100:
            yr += 2000
        try:
            d = date(yr, mo, dy)
            return d if d >= today else (date(yr + 1, mo, dy) if not m.group(3) else d)
        except ValueError:
            pass

    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    return None


def parse_task_recurrence(text: str) -> tuple[Optional[str], str]:
    """
    Parse recurrence text into (RRULE, human_display).
    Returns (None, original_text) if unrecognised.

    Supported: daily, weekly, monthly, yearly, weekdays,
               every N days/weeks/months, monday (→ weekly on that day)
    """
    if not text:
        return None, ""
    t = text.lower().strip()

    simple = {
        "daily": ("RRULE:FREQ=DAILY", "daily"),
        "day":   ("RRULE:FREQ=DAILY", "daily"),
        "weekly": ("RRULE:FREQ=WEEKLY", "weekly"),
        "week":   ("RRULE:FREQ=WEEKLY", "weekly"),
        "monthly": ("RRULE:FREQ=MONTHLY", "monthly"),
        "month":   ("RRULE:FREQ=MONTHLY", "monthly"),
        "yearly":  ("RRULE:FREQ=YEARLY", "yearly"),
        "year":    ("RRULE:FREQ=YEARLY", "yearly"),
        "annual":  ("RRULE:FREQ=YEARLY", "yearly"),
        "annually":("RRULE:FREQ=YEARLY", "yearly"),
        "weekdays":("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "weekdays"),
        "weekday": ("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "weekdays"),
    }
    if t in simple:
        return simple[t]

    # "N days / weeks / months"
    m = re.match(r"^(\d+) (days?|weeks?|months?)$", t)
    if m:
        n, unit = m.group(1), m.group(2).rstrip("s")
        freq = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY"}[unit]
        return f"RRULE:FREQ={freq};INTERVAL={n}", f"every {n} {unit}s"

    # Day name → weekly on that day
    if t in _RRULE_DAY_MAP:
        byday = _RRULE_DAY_MAP[t]
        return f"RRULE:FREQ=WEEKLY;BYDAY={byday}", f"every {t}"

    return None, text  # unrecognised — pass through as display string


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
    """Reconstruct wongnicholas98@gmail.com credentials for Google Tasks. Returns None if not set."""
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


def mark_local_task_done_by_title(title: str) -> bool:
    """Mark the first local task with exactly this title as done. Returns True on success."""
    tasks = load_tasks()
    for t in tasks:
        if t.get("title") == title and t.get("status") != "done":
            t["status"] = "done"
            t["done_at"] = datetime.now(TZ).isoformat()
            save_tasks(tasks)
            log.info("[LOCAL_TASK_DONE] [OK]")
            return True
    return False


def mark_google_task_done_by_id(task_list_id: str, task_id: str, label: str) -> bool:
    """Mark a specific Google Task as completed by IDs. label = 'Personal' | 'Work'."""
    try:
        if label == "Personal":
            creds = _build_personal_credentials()
        else:
            creds = _build_credentials()
        if not creds:
            log.error(f"[MARK_GOOGLE_TASK_BY_ID] [{label}] [NO_CREDS]")
            return False
        service = build("tasks", "v1", credentials=creds)
        service.tasks().patch(
            tasklist=task_list_id,
            task=task_id,
            body={"status": "completed"},
        ).execute()
        log.info(f"[MARK_GOOGLE_TASK_BY_ID] [{label}] [OK]")
        return True
    except Exception:
        log.error(f"[MARK_GOOGLE_TASK_BY_ID] [{label}] [FAIL]")
        return False


def reschedule_google_task(task_list_id: str, task_id: str, label: str, new_date: date) -> bool:
    """Patch the due date of a specific Google Task. label = 'Personal' | 'Work'."""
    try:
        creds = _build_personal_credentials() if label == "Personal" else _build_credentials()
        if not creds:
            log.error(f"[RESCHEDULE_GOOGLE_TASK] [{label}] [NO_CREDS]")
            return False
        service = build("tasks", "v1", credentials=creds)
        due_str = new_date.strftime("%Y-%m-%dT00:00:00.000Z")
        service.tasks().patch(
            tasklist=task_list_id,
            task=task_id,
            body={"due": due_str},
        ).execute()
        log.info(f"[RESCHEDULE_GOOGLE_TASK] [{label}] [OK]")
        return True
    except Exception as exc:
        log.error(f"[RESCHEDULE_GOOGLE_TASK] [{label}] [FAIL] [{type(exc).__name__}]")
        return False


def reschedule_local_task_by_title(title: str, new_date: date) -> bool:
    """Update the due_date field of a local task by exact title match."""
    tasks = load_tasks()
    for t in tasks:
        if t.get("title") == title and t.get("status") != "done":
            t["due_date"] = new_date.isoformat()
            save_tasks(tasks)
            log.info("[RESCHEDULE_LOCAL_TASK] [OK]")
            return True
    return False


def rename_task(old_title: str, new_title: str) -> Optional[str]:
    """
    Rename a task by fuzzy-matching old_title across local store and Google Tasks.
    Personal tasks are synced to Google. Returns the matched original title, or None.
    """
    needle = old_title.lower().strip()
    new_title = new_title[:200].strip()
    if not new_title:
        return None

    # Search local first
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
        old = best["title"]
        is_personal = "personal" in best.get("type", "").lower()
        best["title"] = new_title
        save_tasks(tasks)
        log.info("[RENAME_TASK] [LOCAL] [OK]")
        if is_personal:
            _rename_google_task_fuzzy(old, new_title, "Personal")
        return old

    # Not in local — try Google Tasks directly
    return _rename_google_task_fuzzy(needle, new_title, None)


def _rename_google_task_fuzzy(needle: str, new_title: str, label: Optional[str]) -> Optional[str]:
    """Fuzzy-search Google Tasks across one or both accounts and rename the best match."""
    best_title = None
    best_score = 0
    best_list_id = None
    best_task_id = None
    best_service = None
    needle_lower = needle.lower()

    def _search(service):
        nonlocal best_title, best_score, best_list_id, best_task_id, best_service
        try:
            for tl in service.tasklists().list(maxResults=20).execute().get("items", []):
                for task in service.tasks().list(
                    tasklist=tl["id"], showCompleted=False, maxResults=100
                ).execute().get("items", []):
                    t = task.get("title", "").lower()
                    score = len([w for w in needle_lower.split() if w in t])
                    if score > best_score:
                        best_score = score
                        best_title = task.get("title")
                        best_list_id = tl["id"]
                        best_task_id = task["id"]
                        best_service = service
        except Exception:
            pass

    if label in (None, "Personal"):
        personal_creds = _build_personal_credentials()
        if personal_creds:
            try:
                _search(build("tasks", "v1", credentials=personal_creds))
            except Exception:
                pass

    if label in (None, "Work"):
        try:
            _search(build("tasks", "v1", credentials=_build_credentials()))
        except Exception:
            pass

    if best_score > 0 and best_task_id and best_service:
        try:
            best_service.tasks().patch(
                tasklist=best_list_id,
                task=best_task_id,
                body={"title": new_title},
            ).execute()
            log.info("[RENAME_GOOGLE_TASK] [FUZZY] [OK]")
            return best_title
        except Exception as exc:
            log.error(f"[RENAME_GOOGLE_TASK] [FUZZY] [FAIL] [{type(exc).__name__}]")
    return None


def rename_google_task_by_id(task_list_id: str, task_id: str, new_title: str, label: str) -> bool:
    """Rename a specific Google Task by IDs. label = 'Personal' | 'Work'."""
    try:
        creds = _build_personal_credentials() if label == "Personal" else _build_credentials()
        if not creds:
            return False
        service = build("tasks", "v1", credentials=creds)
        service.tasks().patch(
            tasklist=task_list_id,
            task=task_id,
            body={"title": new_title},
        ).execute()
        log.info(f"[RENAME_GOOGLE_TASK_BY_ID] [{label}] [OK]")
        return True
    except Exception as exc:
        log.error(f"[RENAME_GOOGLE_TASK_BY_ID] [{label}] [FAIL] [{type(exc).__name__}]")
        return False


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
             due_date: Optional[str] = None, priority: Optional[str] = None,
             recurrence: Optional[str] = None) -> None:
    """
    Add a task to local encrypted store.
    priority: 'P0'–'P3' (work tasks only; None for personal)
    recurrence: human-readable string e.g. 'weekly', 'every 2 weeks' (display only)
    due_date: ISO date string 'YYYY-MM-DD'
    """
    tasks = load_tasks()
    tasks.append({
        "title": title[:200],
        "status": "in_progress",
        "source": source,
        "type": task_type,
        "due_date": due_date,
        "priority": priority,
        "recurrence": recurrence,
        "created_at": datetime.now(TZ).isoformat(),
    })
    save_tasks(tasks)


def add_personal_google_task(title: str, due_date: Optional[date] = None) -> bool:
    """
    Add a task to personal Google Tasks (wongnicholas98@gmail.com).
    due_date defaults to today (SGT). Task appears on the Google Calendar grid.
    """
    creds = _build_personal_credentials()
    if not creds:
        log.error("[ADD_PERSONAL_GOOGLE_TASK] [NO_CREDS]")
        return False
    try:
        service = build("tasks", "v1", credentials=creds)
        d = due_date or datetime.now(TZ).date()
        due_str = d.strftime("%Y-%m-%dT00:00:00.000Z")
        service.tasks().insert(
            tasklist="@default",
            body={"title": title[:200], "due": due_str},
        ).execute()
        log.info("[ADD_PERSONAL_GOOGLE_TASK] [OK]")
        return True
    except Exception as exc:
        log.error(f"[ADD_PERSONAL_GOOGLE_TASK] [FAIL] [{type(exc).__name__}]")
        return False


def add_personal_google_calendar_event(title: str, event_date: date,
                                        rrule: Optional[str] = None) -> bool:
    """
    Create an all-day event on the personal Google Calendar (wongnicholas98@gmail.com).
    If rrule is provided (e.g. 'RRULE:FREQ=WEEKLY'), the event repeats.
    Requires Calendar scope on the personal token — re-run auth_personal_tasks.py
    if this returns False with a 403 error.
    """
    creds = _build_personal_credentials()
    if not creds:
        log.error("[ADD_PERSONAL_CAL_EVENT] [NO_CREDS]")
        return False
    try:
        service = build("calendar", "v3", credentials=creds)
        body: dict = {
            "summary": title[:200],
            "start": {"date": event_date.isoformat()},
            # Google Calendar all-day events: end is exclusive (day after)
            "end":   {"date": (event_date + timedelta(days=1)).isoformat()},
        }
        if rrule:
            body["recurrence"] = [rrule]
        service.events().insert(calendarId="primary", body=body).execute()
        log.info("[ADD_PERSONAL_CAL_EVENT] [OK]")
        return True
    except Exception as exc:
        log.error(f"[ADD_PERSONAL_CAL_EVENT] [FAIL] [{type(exc).__name__}]")
        return False


# ── Google Tasks ──────────────────────────────────────────────────────────────

def _is_due_today_or_overdue(task: dict) -> bool:
    """Return True if a Google Task is overdue or due today (SGT)."""
    if task.get("overdue"):
        return True
    due_dt = task.get("due_dt")
    if due_dt is None:
        return False
    return due_dt.astimezone(TZ).date() == datetime.now(TZ).date()


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
                due_date_str = None
                due_dt = None
                overdue = False
                if due_str:
                    try:
                        due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                        due_date_str = due_dt.strftime("%d %b")
                        overdue = due_dt.astimezone(TZ).date() < now.date()
                    except ValueError:
                        pass
                results.append({
                    "title": title,
                    "due": due_date_str,
                    "due_dt": due_dt,
                    "task_list": f"{tl_name} ({label})",
                    "task_list_id": tl_id,
                    "task_id": task["id"],
                    "overdue": overdue,
                    "google_task": True,
                    "label": label,
                })
    except Exception:
        log.error(f"[FETCH_GOOGLE_TASKS] [{label}] [FAIL]")
    return results


def fetch_google_tasks() -> list[dict]:
    """Fetch incomplete tasks from both work and personal Google accounts."""
    log.info("[FETCH_GOOGLE_TASKS] [START]")
    results = []

    try:
        creds = _build_credentials()
        results.extend(_fetch_tasks_for_service(build("tasks", "v1", credentials=creds), "Work"))
    except Exception:
        log.error("[FETCH_GOOGLE_TASKS] [WORK] [FAIL]")

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


def get_tasks_for_dropdown() -> dict:
    """
    Return structured task data for the /done inline keyboard dropdown.
    Result: {"personal": [...], "work": [...]}
    Each entry: {title, source ("local"|"google"), task_id|None, task_list_id|None, label, priority}

    Personal tasks are deduplicated: if a local task's title matches a Google Task,
    the Google version is preferred (so done/reschedule actions use the correct task ID).
    """
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    # Build set of all Google personal task titles for deduplication
    google_personal_titles = {t["title"] for t in google_tasks if t.get("label") == "Personal"}

    personal = []
    work = []

    for t in tasks:
        if t.get("status") != "in_progress":
            continue
        if t.get("auto_generated"):
            continue
        typ = t.get("type", "").lower()
        if "personal" in typ:
            # Skip local copy if an identical Google Task exists — Google version added below
            if t["title"] not in google_personal_titles:
                personal.append({
                    "title": t["title"], "source": "local",
                    "task_id": None, "task_list_id": None, "label": "Personal",
                })
        elif "work" in typ:
            work.append({
                "title": t["title"], "source": "local",
                "task_id": None, "task_list_id": None, "label": "Work",
                "priority": t.get("priority", "P2"),
            })

    for t in google_tasks:
        if not _is_due_today_or_overdue(t):
            continue
        entry = {
            "title": t["title"],
            "source": "google",
            "task_id": t.get("task_id"),
            "task_list_id": t.get("task_list_id"),
            "label": t.get("label", "Personal"),
        }
        if t.get("label") == "Personal":
            personal.append(entry)
        else:
            work.append(entry)

    return {"personal": personal, "work": work}


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

def _tag_calendar(cal_name: str, cal_id: str) -> str:
    """Return the emoji tag for a calendar. Work calendar matched by ID, others by name."""
    if cal_id.lower() == REQUIRED_ACCOUNT.lower():
        return "💼"
    lower = cal_name.lower()
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
        return True
    lower = cal_name.lower()
    return any(k in lower for k in CALENDAR_WHITELIST_KEYWORDS)


def fetch_calendar_events(target_date: date, is_weekend: bool) -> list[dict]:
    """
    Returns sorted list of events for target_date from whitelisted calendars only.
    is_weekend=True → Work calendar skipped entirely at API level.
    Each event: {time, title, calendar, tag, all_day, start_dt, end_dt, description, attachments}
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

        if not _is_whitelisted_calendar(cal_name, cal_id):
            continue

        if is_weekend and cal_id.lower() == REQUIRED_ACCOUNT.lower():
            log.info("[FETCH_CALENDAR] [SKIP_WORK_WEEKEND]")
            continue

        tag = _tag_calendar(cal_name, cal_id)

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
            title = ev.get("summary", "").strip()
            if not title:
                continue

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

            description = (ev.get("description") or "").strip()
            attachments = ev.get("attachments") or []

            events.append({
                "time": time_str,
                "title": title,
                "calendar": cal_name,
                "tag": tag,
                "all_day": all_day,
                "start_dt": start_dt_obj,
                "end_dt": end_dt_obj,
                "description": description,
                "attachments": attachments,
            })

    events.sort(key=lambda e: (
        e["start_dt"] is not None,
        e["start_dt"] if e["start_dt"] else datetime.min.replace(tzinfo=TZ),
    ))
    log.info("[FETCH_CALENDAR] [OK]")
    return events


# ── Conflict detection ─────────────────────────────────────────────────────────

def _detect_conflicts(events: list[dict]) -> list[str]:
    """Flag overlapping or back-to-back (<15 min gap) timed events. Titles are HTML-escaped."""
    flags = []
    timed = [e for e in events if not e["all_day"] and e.get("start_dt") and e.get("end_dt")]
    for i in range(len(timed) - 1):
        gap_min = (timed[i + 1]["start_dt"] - timed[i]["end_dt"]).total_seconds() / 60
        a = _h(timed[i]["title"])
        b = _h(timed[i + 1]["title"])
        if gap_min < 0:
            flags.append(f"Overlap: {a} → {b}")
        elif gap_min < 15:
            flags.append(f"Back-to-back: {a} → {b}")
    return flags


# ── Auto-task extraction ───────────────────────────────────────────────────────

_ACTION_KEYWORDS = re.compile(
    r"\b(please|action required|deadline|by [A-Z][a-z]+day|asap|urgent|"
    r"follow.up|review|approve|confirm|submit|send|complete)\b",
    re.IGNORECASE,
)

_EVENT_ACTION_RE = re.compile(
    r"(?m)^\s*[-•*\d.)]*\s*"
    r"(?:action item|action|todo|to-do|to do|follow.?up|please|"
    r"deadline|action required|prep|prepare|review|submit|send|complete)"
    r"[:\s]+(.{5,200})",
    re.IGNORECASE,
)


def extract_auto_tasks(emails: list[dict]) -> list[dict]:
    """Extract potential tasks from email subjects."""
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
    """Parse event descriptions for action items and distil them into tasks."""
    new_tasks = []
    for ev in events:
        desc = ev.get("description") or ""
        if not desc:
            continue
        desc_plain = re.sub(r"<[^>]+>", " ", desc)
        desc_plain = re.sub(r"&[a-z]+;", " ", desc_plain)
        for match in _EVENT_ACTION_RE.finditer(desc_plain):
            item = match.group(1).strip()[:200]
            if item:
                new_tasks.append({
                    "title": f"{ev['title']}: {item}",
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
    """Format tasks into Personal Tasks and Work Tasks sections. Work hidden on weekends."""
    google_tasks = google_tasks or []

    # Split local tasks by type and status
    local_personal_active = [t for t in tasks
                             if "personal" in t.get("type", "").lower()
                             and t.get("status") == "in_progress"
                             and not t.get("auto_generated")]
    local_personal_done = [t for t in tasks
                           if "personal" in t.get("type", "").lower()
                           and t.get("status") == "done"]
    local_personal_overdue = [t for t in tasks
                              if "personal" in t.get("type", "").lower()
                              and t.get("status") == "overdue"]

    local_work_active = [t for t in tasks
                         if "work" in t.get("type", "").lower()
                         and t.get("status") == "in_progress"
                         and not t.get("auto_generated")]
    local_work_overdue = [t for t in tasks
                          if "work" in t.get("type", "").lower()
                          and t.get("status") == "overdue"]

    # Google Tasks due today / overdue, split by label
    gt_personal = [t for t in google_tasks if t.get("label") == "Personal" and _is_due_today_or_overdue(t)]
    gt_work = [t for t in google_tasks if t.get("label") == "Work" and _is_due_today_or_overdue(t)]

    def _task_due_rec(t: dict) -> str:
        """Return ' (due DD Mon, recurring)' annotation for a local task."""
        parts = []
        if t.get("due_date"):
            try:
                d = date.fromisoformat(t["due_date"])
                parts.append(f"due {d.strftime('%d %b')}")
            except ValueError:
                pass
        if t.get("recurrence"):
            parts.append(f"🔁 {t['recurrence']}")
        return f" ({', '.join(parts)})" if parts else ""

    # Deduplicate: skip local personal tasks whose title already appears in Google Tasks
    all_google_personal_titles = {t["title"] for t in google_tasks if t.get("label") == "Personal"}

    lines = ["👤 <b>Personal Tasks</b>"]
    if local_personal_done:
        lines.append("🟢 Completed: " + ", ".join(_h(t["title"]) for t in local_personal_done))
    if local_personal_overdue:
        lines.append("🔴 Overdue: " + ", ".join(_h(t["title"]) for t in local_personal_overdue))
    for t in local_personal_active:
        if t["title"] not in all_google_personal_titles:
            lines.append(f"• {_h(t['title'])}{_task_due_rec(t)}")
    for t in gt_personal:
        due = f" (due {_h(t['due'])})" if t.get("due") else ""
        emoji = "🔴" if t.get("overdue") else "🟡"
        lines.append(f"{emoji} {_h(t['title'])}{due}")
    if (not local_personal_done and not local_personal_overdue
            and not local_personal_active and not gt_personal):
        lines.append("No personal tasks.")

    if not weekend:
        lines += ["", "💼 <b>Work Tasks</b>"]
        # Combine local work tasks and sort by priority (P0 first), overdue first within each
        work_items = sorted(
            local_work_overdue + local_work_active,
            key=lambda t: (
                _PRIORITY_ORDER.get(t.get("priority", "P2"), 2),
                0 if t.get("status") == "overdue" else 1,
            ),
        )
        for t in work_items:
            p = t.get("priority", "P2")
            badge = _PRIORITY_EMOJI.get(p, "🟡")
            overdue_tag = " ⚠️" if t.get("status") == "overdue" else ""
            lines.append(f"{badge} <b>{p}</b> {_h(t['title'])}{_task_due_rec(t)}{overdue_tag}")
        for t in gt_work:
            due = f" (due {_h(t['due'])})" if t.get("due") else ""
            emoji = "🔴" if t.get("overdue") else "🟡"
            lines.append(f"{emoji} {_h(t['title'])}{due}")
        if not work_items and not gt_work:
            lines.append("No work tasks.")

    return "\n".join(lines)


def _top_priorities_split(tasks: list[dict], google_tasks: list[dict],
                          weekend: bool = False) -> str:
    """Return top 3 Work priorities and top 3 Personal priorities as formatted text."""
    today_gt = [t for t in google_tasks if _is_due_today_or_overdue(t)]

    work_gt = [t for t in today_gt if t.get("label") == "Work"]
    personal_gt = [t for t in today_gt if t.get("label") == "Personal"]

    work_local = [t for t in tasks
                  if "work" in t.get("type", "").lower()
                  and t.get("status") in ("overdue", "in_progress")
                  and not t.get("auto_generated")]
    personal_local = [t for t in tasks
                      if "personal" in t.get("type", "").lower()
                      and t.get("status") in ("overdue", "in_progress")
                      and not t.get("auto_generated")]

    def _pick3(candidates, show_priority: bool = False):
        seen: set[str] = set()
        unique = []
        for c in candidates:
            title = c["title"]
            if title not in seen:
                seen.add(title)
                unique.append(c)
            if len(unique) == 3:
                break
        result = []
        for i, p in enumerate(unique):
            if show_priority and p.get("priority"):
                badge = f"{_PRIORITY_EMOJI.get(p['priority'], '')} <b>{p['priority']}</b> "
            else:
                badge = ""
            result.append(f"{i+1}. {badge}{_h(p['title'])}")
        while len(result) < 3:
            result.append(f"{len(result)+1}. (Add manually)")
        return result

    personal_combined = (
        [t for t in personal_gt if t.get("overdue")] +
        [t for t in personal_local if t.get("status") == "overdue"] +
        personal_gt +
        personal_local
    )

    lines = []

    if not weekend:
        # Sort work by priority first, then overdue status
        work_combined = sorted(
            (
                [t for t in work_gt if t.get("overdue")] +
                [t for t in work_local if t.get("status") == "overdue"] +
                work_gt +
                work_local
            ),
            key=lambda t: (
                _PRIORITY_ORDER.get(t.get("priority", "P2"), 2),
                0 if t.get("status") == "overdue" else 1,
            ),
        )
        lines += ["💼 <b>Work</b>"]
        lines.extend(_pick3(work_combined, show_priority=True))
        lines += [""]

    lines += ["👤 <b>Personal</b>"]
    lines.extend(_pick3(personal_combined))
    return "\n".join(lines)


def _render_event_line(ev: dict) -> str:
    """Format a single calendar event line with notes. Title is HTML-escaped."""
    note = ""
    if "🌴" in ev["tag"]:
        note = " — 🌴 You're on leave"
    elif "🤝" in ev["tag"]:
        note = " — Joint commitment"
    desc = ev.get("description") or ""
    desc_plain = re.sub(r"<[^>]+>", " ", desc)
    if _EVENT_ACTION_RE.search(desc_plain):
        note += " — 📋 prep items in tasks"
    if ev.get("attachments"):
        att = ", ".join(_h(a.get("title", "file")) for a in ev["attachments"][:2])
        note += f" — 📎 {att}"
    return f"{ev['time']} — {_h(ev['title'])} {ev['tag']}{note}"


def build_evening_briefing(target_date: date) -> str:
    now = datetime.now(TZ)
    today = now.date()
    weekday = today.weekday()
    is_weekend_target = target_date.weekday() >= 5

    # Fetch target day events first so we can detect OOO before email mode decision
    events = fetch_calendar_events(target_date, is_weekend=is_weekend_target)
    is_ooo_tomorrow = not is_weekend_target and _is_ooo_day(events)
    # If tomorrow is OOO or a weekend, treat work content as suppressed
    effective_weekend = is_weekend_target or is_ooo_tomorrow

    # Emails are today's (not tomorrow's), but defer if it's a Friday or OOO tomorrow
    if weekday >= 4 or is_ooo_tomorrow:
        email_data = fetch_emails_weekend()
    else:
        email_data = fetch_emails(since_hours=24)

    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    existing_titles = {t["title"] for t in tasks}
    if email_data["urgent"]:
        new_auto = [t for t in extract_auto_tasks(email_data["urgent"])
                    if t["title"] not in existing_titles]
        if new_auto:
            tasks.extend(new_auto)
            existing_titles.update(t["title"] for t in new_auto)
            save_tasks(tasks)

    event_tasks = [t for t in extract_tasks_from_events(events)
                   if t["title"] not in existing_titles]
    if event_tasks:
        tasks.extend(event_tasks)
        save_tasks(tasks)

    conflict_flags = _detect_conflicts(events)
    vacation_events = [e for e in events if "🌴" in e["tag"]]
    ryan_events = [e for e in events if "🤝" in e["tag"]]

    day_label = target_date.strftime("%A, %d %B %Y")
    today_label = today.strftime("%A, %d %B %Y")

    lines = [
        f"📅 <b>Evening Prep — {today_label}</b>",
        f"<i>Preparing you for {day_label}</i>",
    ]
    if is_ooo_tomorrow:
        lines.append("🏖️ <i>Tomorrow is OOO — work emails and events hidden</i>")
    lines += ["", "📬 <b>Email Summary</b>"]

    if email_data["deferred_count"]:
        lines.append(f"📥 {email_data['deferred_count']} work emails received — deferred (OOO/weekend)")
    elif not effective_weekend:
        for e in email_data["urgent"]:
            lines.append(f"🔴 {_h(e['sender'])} — {_h(e['subject'])} <i>(action required)</i>")
        for e in email_data["normal"]:
            lines.append(f"• {_h(e['sender'])} — {_h(e['subject'])}")
    if not email_data["urgent"] and not email_data["normal"] and not email_data["deferred_count"]:
        lines.append("No new emails.")

    lines += ["", "🗓️ <b>Tomorrow's Schedule</b>"]
    visible_events = [e for e in events if not (is_ooo_tomorrow and "💼" in e["tag"])]
    if visible_events:
        for ev in visible_events:
            lines.append(_render_event_line(ev))
    else:
        lines.append("No events scheduled.")

    lines += ["", "⚠️ <b>Flags</b>"]
    if is_ooo_tomorrow:
        lines.append("• 🏖️ OOO tomorrow — no work commitments shown")
    if conflict_flags:
        for f in conflict_flags:
            lines.append(f"• {f}")
    if vacation_events:
        lines.append("• 🌴 You are on leave — adjust plans accordingly")
    if ryan_events:
        for e in ryan_events:
            lines.append(f"• 🤝 Joint commitment with Ryan Chia: {_h(e['title'])}")
    if not is_ooo_tomorrow and not conflict_flags and not vacation_events and not ryan_events:
        lines.append("• No flags.")

    lines += ["", _format_task_list(tasks, google_tasks, weekend=effective_weekend)]
    lines += [
        "",
        "🎯 <b>Top 3 Priorities for Tomorrow</b>",
        _top_priorities_split(tasks, google_tasks, weekend=effective_weekend),
    ]

    return "\n".join(lines)


def build_morning_briefing() -> str:
    now = datetime.now(TZ)
    today = now.date()
    weekday = today.weekday()
    is_weekend = weekday >= 5

    # Fetch events first so we can detect OOO before deciding email mode
    events = fetch_calendar_events(today, is_weekend=is_weekend)
    is_ooo = not is_weekend and _is_ooo_day(events)
    # Treat OOO days like weekends: skip work emails and work content
    effective_weekend = is_weekend or is_ooo

    email_data = fetch_emails_weekend() if effective_weekend else fetch_emails(since_hours=12)
    tasks = load_tasks()
    tasks = _archive_old_done_tasks(tasks)
    google_tasks = fetch_google_tasks()

    existing_titles = {t["title"] for t in tasks}
    if email_data["urgent"]:
        new_auto = [t for t in extract_auto_tasks(email_data["urgent"])
                    if t["title"] not in existing_titles]
        if new_auto:
            tasks.extend(new_auto)
            existing_titles.update(t["title"] for t in new_auto)
            save_tasks(tasks)

    event_tasks = [t for t in extract_tasks_from_events(events)
                   if t["title"] not in existing_titles]
    if event_tasks:
        tasks.extend(event_tasks)
        save_tasks(tasks)

    conflict_flags = _detect_conflicts(events)
    vacation_events = [e for e in events if "🌴" in e["tag"]]
    ryan_events = [e for e in events if "🤝" in e["tag"]]

    today_label = today.strftime("%A, %d %B %Y")

    lines = [
        f"☀️ <b>Morning Refresh — {today_label}</b>",
    ]
    if is_ooo:
        lines.append("🏖️ <i>You are OOO today — work emails and events hidden</i>")
    lines += ["", "📬 <b>Overnight Emails</b>"]

    if effective_weekend and email_data.get("deferred_count"):
        lines.append(f"📥 {email_data['deferred_count']} work emails received — deferred (OOO/weekend)")
    elif not effective_weekend:
        for e in email_data["urgent"]:
            lines.append(f"🔴 {_h(e['sender'])} — {_h(e['subject'])} <i>(action required)</i>")
        for e in email_data["normal"]:
            lines.append(f"• {_h(e['sender'])} — {_h(e['subject'])}")
    if not email_data["urgent"] and not email_data["normal"] and not email_data.get("deferred_count"):
        lines.append("No overnight emails.")

    lines += ["", "🗓️ <b>Today's Schedule</b>"]
    visible_events = [e for e in events if not (is_ooo and "💼" in e["tag"])]
    if visible_events:
        for ev in visible_events:
            lines.append(_render_event_line(ev))
    else:
        lines.append("No events today.")

    lines += ["", "⚠️ <b>Flags</b>"]
    if is_ooo:
        lines.append("• 🏖️ OOO today — work commitments hidden")
    if conflict_flags:
        for f in conflict_flags:
            lines.append(f"• {f}")
    if vacation_events:
        lines.append("• 🌴 You are on leave")
    if ryan_events:
        for e in ryan_events:
            lines.append(f"• 🤝 Joint commitment with Ryan Chia: {_h(e['title'])}")
    if not is_ooo and not conflict_flags and not vacation_events and not ryan_events:
        lines.append("• No flags.")

    lines += ["", _format_task_list(tasks, google_tasks, weekend=effective_weekend)]

    auto_tasks = [t for t in tasks if t.get("auto_generated") and t.get("status") == "in_progress"]
    if auto_tasks:
        lines += ["", "🆕 <b>Auto-generated tasks (please confirm):</b>"]
        for t in auto_tasks:
            lines.append(f"  • {_h(t['title'])}")

    lines += [
        "",
        "🎯 <b>Focus for Today</b>",
        _top_priorities_split(tasks, google_tasks, weekend=effective_weekend),
    ]

    return "\n".join(lines)


def build_weekend_briefing() -> str:
    """Personal-only schedule for today."""
    now = datetime.now(TZ)
    today = now.date()
    events = fetch_calendar_events(today, is_weekend=True)
    today_label = today.strftime("%A, %d %B %Y")

    lines = [f"📅 <b>Weekend View — {today_label}</b>", ""]
    if events:
        for ev in events:
            lines.append(f"{ev['time']} — {_h(ev['title'])} {ev['tag']}")
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

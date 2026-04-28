"""
telegram_bot.py — Telegram interface with full security hardening.

Security model:
- Chat ID whitelist enforced on every message
- Rate limiting: 3-second cooldown per Chat ID
- Input sanitisation + length limits
- Replay attack prevention via update_id tracking
- No personal data ever sent to chat
- Unknown commands logged but not echoed

/update command:
  /update              → inline keyboard: Done / Today / +1 day per task
  /update old > new    → rename task (personal syncs to Google)
  /update done [task]  → quick mark-done alias
"""

import os
import re
import time as _time_mod
import logging
import logging.handlers
from datetime import datetime, timezone, date, timedelta, time
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import briefing_engine as engine
import gym_engine as gym

load_dotenv()

log = logging.getLogger("telegram_bot")

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID_STR = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{35,}$")
# Allow > for rename separator ("old name > new name")
_SAFE_TEXT_RE = re.compile(r"[^a-zA-Z0-9 ,.\-_/@#!\?':()+&=>]")

MAX_COMMAND_LEN = 500
MAX_ADD_LEN = 200
RATE_LIMIT_SECONDS = 3
GENERIC_ERROR = "Something went wrong. Please try again later."
UNKNOWN_CMD = "Unknown command. Send /help for the list."

# ── In-memory state ────────────────────────────────────────────────────────────
_last_command_time: dict[int, float] = {}
_processed_update_ids: set[int] = set()
_task_dropdown_cache: dict[int, dict] = {}   # /done cache
_update_dropdown_cache: dict[int, dict] = {} # /update cache


def validate_token() -> None:
    if not _TOKEN_RE.match(BOT_TOKEN):
        log.error("[TOKEN_VALIDATION] [MALFORMED]")
        raise SystemExit("TELEGRAM_BOT_TOKEN is malformed — check your .env")
    log.info("[TOKEN_VALIDATION] [OK]")


def _get_allowed_chat_id() -> Optional[int]:
    try:
        return int(ALLOWED_CHAT_ID_STR)
    except (ValueError, TypeError):
        return None


def _sanitise(text: str, max_len: int = MAX_COMMAND_LEN) -> str:
    cleaned = _SAFE_TEXT_RE.sub("", text)
    return cleaned[:max_len]


def _is_authorised(chat_id: int) -> bool:
    allowed = _get_allowed_chat_id()
    if allowed is None:
        log.error("[AUTH_CHECK] [CHAT_ID_NOT_CONFIGURED]")
        return False
    return chat_id == allowed


def _is_rate_limited(chat_id: int) -> bool:
    now = _time_mod.monotonic()
    last = _last_command_time.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        log.info("[RATE_LIMIT] [BLOCKED]")
        return True
    _last_command_time[chat_id] = now
    return False


def _is_replay(update_id: int) -> bool:
    if update_id in _processed_update_ids:
        log.info("[REPLAY_PREVENTION] [DUPLICATE_UPDATE_ID]")
        return True
    _processed_update_ids.add(update_id)
    if len(_processed_update_ids) > 10000:
        oldest = min(_processed_update_ids)
        _processed_update_ids.discard(oldest)
    return False


async def _guard(update: Update) -> bool:
    if update.update_id and _is_replay(update.update_id):
        return False
    msg = update.message or update.edited_message
    if not msg:
        return False
    chat_id = msg.chat_id
    if not _is_authorised(chat_id):
        log.warning(f"[UNAUTHORISED_ACCESS] [CHAT_ID_BLOCKED] [{datetime.now(timezone.utc).isoformat()}]")
        return False
    if _is_rate_limited(chat_id):
        return False
    text = msg.text or ""
    if len(text) > MAX_COMMAND_LEN:
        await msg.reply_text(GENERIC_ERROR)
        log.info("[INPUT_VALIDATION] [TOO_LONG]")
        return False
    return True


async def _guard_callback(update: Update) -> bool:
    query = update.callback_query
    if not query:
        return False
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None or not _is_authorised(chat_id):
        await query.answer()
        return False
    return True


# ── Inline keyboard builders ───────────────────────────────────────────────────

def _build_done_keyboard(chat_id: int) -> Optional[InlineKeyboardMarkup]:
    """Build inline keyboard for /done — one task per row, click to mark done."""
    try:
        tasks = engine.get_tasks_for_dropdown()
        _task_dropdown_cache[chat_id] = tasks
    except Exception:
        log.error("[DONE_KEYBOARD] [FETCH_FAIL]")
        return None

    personal = tasks.get("personal", [])
    work = tasks.get("work", [])
    if not personal and not work:
        return None

    buttons = []
    if personal:
        buttons.append([InlineKeyboardButton("── Personal Tasks ──", callback_data="noop")])
        for i, t in enumerate(personal[:10]):
            label = t["title"][:40] + ("…" if len(t["title"]) > 40 else "")
            buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"dp:{i}")])
    if work:
        buttons.append([InlineKeyboardButton("── Work Tasks ──", callback_data="noop")])
        for i, t in enumerate(work[:10]):
            p = t.get("priority", "P2")
            prefix = f"[{p}] " if t["source"] == "local" else ""
            label = (prefix + t["title"])[:42] + ("…" if len(prefix + t["title"]) > 42 else "")
            buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"dw:{i}")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)


def _build_update_keyboard(chat_id: int) -> Optional[InlineKeyboardMarkup]:
    """
    Build inline keyboard for /update.
    Each task gets a name row (label) + an action row: Done | Today | +1 day.
    """
    try:
        tasks = engine.get_tasks_for_dropdown()
        _update_dropdown_cache[chat_id] = tasks
    except Exception:
        log.error("[UPDATE_KEYBOARD] [FETCH_FAIL]")
        return None

    personal = tasks.get("personal", [])
    work = tasks.get("work", [])
    if not personal and not work:
        return None

    buttons = []

    if personal:
        buttons.append([InlineKeyboardButton("── Personal Tasks ──", callback_data="noop")])
        for i, t in enumerate(personal[:10]):
            name = t["title"][:46] + ("…" if len(t["title"]) > 46 else "")
            buttons.append([InlineKeyboardButton(name, callback_data="noop")])
            buttons.append([
                InlineKeyboardButton("✅ Done",      callback_data=f"ua:p:{i}:done"),
                InlineKeyboardButton("📅 Today",     callback_data=f"ua:p:{i}:today"),
                InlineKeyboardButton("📅 +1 day",    callback_data=f"ua:p:{i}:tom"),
            ])

    if work:
        buttons.append([InlineKeyboardButton("── Work Tasks ──", callback_data="noop")])
        for i, t in enumerate(work[:10]):
            p = t.get("priority", "P2")
            prefix = f"[{p}] " if t["source"] == "local" else ""
            raw_name = prefix + t["title"]
            name = raw_name[:46] + ("…" if len(raw_name) > 46 else "")
            buttons.append([InlineKeyboardButton(name, callback_data="noop")])
            buttons.append([
                InlineKeyboardButton("✅ Done",      callback_data=f"ua:w:{i}:done"),
                InlineKeyboardButton("📅 Today",     callback_data=f"ua:w:{i}:today"),
                InlineKeyboardButton("📅 +1 day",    callback_data=f"ua:w:{i}:tom"),
            ])

    buttons.append([InlineKeyboardButton("Cancel", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_START] [OK]")
    await update.message.reply_text(
        "👋 Daily Briefing Bot is online.\n\n"
        "Briefing:\n"
        "/morning — Morning refresh\n"
        "/evening — Evening prep\n"
        "/tasks — Current task list\n"
        "/done — Mark task done (dropdown)\n"
        "/add [task] — Add personal task (synced to Google)\n"
        "/add w [task] — Add work task (local only)\n"
        "/update — Update tasks: reschedule or rename\n"
        "/append [task] + [text] — Append text to a task\n"
        "/weekend — Personal schedule only\n\n"
        "Gym:\n"
        "/days MON WED FRI — Schedule gym days next week\n"
        "/skip — Skip today, roll split forward\n"
        "/status — This week's gym sessions\n"
        "/next — Next split day (no change)\n"
        "/reschedule [date] [time] — Schedule on a date/time\n"
        "/setsplit [date] [Push/Pull/Legs/Rest] — Change a day's split\n\n"
        "/help — Full command list",
        parse_mode=None,
    )


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_MORNING] [START]")
    try:
        briefing = engine.build_morning_briefing()
        await update.message.reply_text(briefing, parse_mode="HTML")
        log.info("[CMD_MORNING] [OK]")
    except Exception:
        log.error("[CMD_MORNING] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_evening(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_EVENING] [START]")
    try:
        target = engine.get_next_briefing_target()
        briefing = engine.build_evening_briefing(target)
        await update.message.reply_text(briefing, parse_mode="HTML")
        log.info("[CMD_EVENING] [OK]")
    except Exception:
        log.error("[CMD_EVENING] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_TASKS] [START]")
    try:
        today_wd = date.today().weekday()
        text = engine.get_task_list_text(weekend=(today_wd >= 5))
        await update.message.reply_text(text, parse_mode="HTML")
        log.info("[CMD_TASKS] [OK]")
    except Exception:
        log.error("[CMD_TASKS] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_DONE] [START]")
    try:
        args = update.message.text.partition(" ")[2].strip()
        if not args:
            chat_id = update.message.chat_id
            keyboard = _build_done_keyboard(chat_id)
            if keyboard is None:
                await update.message.reply_text("No active tasks found. Use /add to create one.")
                return
            await update.message.reply_text("Select a task to mark as done:", reply_markup=keyboard)
            log.info("[CMD_DONE] [DROPDOWN_SHOWN]")
            return

        task_name = _sanitise(args, MAX_COMMAND_LEN)
        matched = engine.mark_task_done(task_name)
        if matched:
            await update.message.reply_text(f"✅ Marked done: {matched}")
            log.info("[CMD_DONE] [OK]")
        else:
            await update.message.reply_text("No matching task found. Try /done without args to use the dropdown.")
    except Exception:
        log.error("[CMD_DONE] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


def _parse_add_args(raw: str) -> dict:
    """
    Parse /add command arguments into structured fields.
    Extraction order (right-to-left to avoid greedy matching on title):
      1. Work prefix  "w "
      2. Priority     "p0"–"p3" anywhere
      3. Recurrence   "every [freq]" at the end
      4. Date         "on [date]"  at the end (before "every")
      5. Remainder    = task title

    Returns: {is_work, title, due_date, rrule, recurrence_display, priority}
    """
    is_work = False
    if raw.lower().startswith("w ") and len(raw) > 2:
        is_work = True
        raw = raw[2:].strip()

    # Priority: p0-p3 anywhere (case-insensitive)
    priority = "P2"
    p_m = re.search(r"\bp([0-3])\b", raw, re.IGNORECASE)
    if p_m:
        priority = f"P{p_m.group(1)}"
        raw = (raw[:p_m.start()] + raw[p_m.end():]).strip()
        raw = re.sub(r"\s{2,}", " ", raw)

    # Recurrence: "every [freq]" at the end
    rrule, recurrence_display = None, None
    every_m = re.search(r"\s+every\s+(.+)$", raw, re.IGNORECASE)
    if every_m:
        rrule, recurrence_display = engine.parse_task_recurrence(every_m.group(1).strip())
        raw = raw[:every_m.start()].strip()

    # Date: "on [date]" at the end — only strip if the date actually parses,
    # so "on" inside a task title (e.g. "help with X on Y stuff") is preserved.
    due_date = None
    on_m = re.search(r"\s+on\s+(.+)$", raw, re.IGNORECASE)
    if on_m:
        parsed_date = engine.parse_task_date(on_m.group(1).strip())
        if parsed_date is not None:
            due_date = parsed_date
            raw = raw[:on_m.start()].strip()

    return {
        "is_work": is_work,
        "title": raw.strip(),
        "due_date": due_date,
        "rrule": rrule,
        "recurrence_display": recurrence_display,
        "priority": priority,
    }


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_ADD] [START]")
    try:
        raw = update.message.text.partition(" ")[2].strip()
        if not raw:
            await update.message.reply_text(
                "Usage:\n\n"
                "Personal tasks (synced to Google):\n"
                "  /add [task]\n"
                "  /add [task] on [date]\n"
                "  /add [task] on [date] every [freq]\n\n"
                "Work tasks (local, with priority P0–P3):\n"
                "  /add w [task]\n"
                "  /add w [task] p1\n"
                "  /add w [task] p0 on [date]\n\n"
                "Date: tomorrow, friday, 5 may, next week, 2026-05-01\n"
                "Freq: daily, weekly, monthly, every 2 weeks, weekdays"
            )
            return

        parsed = _parse_add_args(_sanitise(raw, MAX_ADD_LEN + 100))
        task_title = _sanitise(parsed["title"], MAX_ADD_LEN)
        if not task_title:
            await update.message.reply_text("Task title cannot be empty.")
            return

        due_date: Optional[date] = parsed["due_date"]
        rrule: Optional[str] = parsed["rrule"]
        rec_display: Optional[str] = parsed["recurrence_display"]

        if parsed["is_work"]:
            # Work task — local only, with priority
            due_str = due_date.isoformat() if due_date else None
            engine.add_task(
                task_title, source="Manual", task_type="💼 Work",
                due_date=due_str, priority=parsed["priority"],
                recurrence=rec_display,
            )
            p = parsed["priority"]
            date_note = f", due {due_date.strftime('%d %b')}" if due_date else ""
            rec_note  = f", repeats {rec_display}" if rec_display else ""
            await update.message.reply_text(
                f"✅ Work task added [{p}]: {task_title}{date_note}{rec_note}"
            )
            log.info(f"[CMD_ADD] [WORK] [{p}] [OK]")

        else:
            # Personal task — store locally and sync to Google
            due_str = due_date.isoformat() if due_date else None
            engine.add_task(
                task_title, source="Manual", task_type="👤 Personal",
                due_date=due_str, recurrence=rec_display,
            )

            target_date = due_date or date.today()
            date_note = f", due {target_date.strftime('%d %b')}" if due_date else " (today)"
            rec_note  = f", repeats {rec_display}" if rec_display else ""

            # Always sync as a Google Task (recurring info stored locally only —
            # Google Tasks API does not support RRULE)
            ok = engine.add_personal_google_task(task_title, due_date=target_date)
            if ok:
                if rrule:
                    sync_note = (
                        " ✅ Synced to Google Tasks (first occurrence only)\n"
                        "⚠️ Google Tasks doesn't support recurring tasks — "
                        "please set up the repeat manually in Google Tasks or Calendar."
                    )
                else:
                    sync_note = " ✅ Synced to Google Tasks"
            else:
                sync_note = " ⚠️ Google sync failed"

            await update.message.reply_text(
                f"✅ Personal task added: {task_title}{date_note}{rec_note}\n{sync_note}"
            )
            log.info(f"[CMD_ADD] [PERSONAL] [RECURRING={bool(rrule)}] [OK]")

    except Exception:
        log.error("[CMD_ADD] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /update              — show dropdown: Done | Today | +1 day per task
    /update old > new    — rename task (personal syncs to Google Tasks)
    /update done [task]  — quick mark-done shortcut
    """
    if not await _guard(update):
        return
    log.info("[CMD_UPDATE] [START]")
    try:
        args = update.message.text.partition(" ")[2].strip()

        # ── No args: show the update dropdown ─────────────────────────────
        if not args:
            chat_id = update.message.chat_id
            keyboard = _build_update_keyboard(chat_id)
            if keyboard is None:
                await update.message.reply_text("No active tasks found. Use /add to create one.")
                return
            await update.message.reply_text(
                "Select a task to update:",
                reply_markup=keyboard,
            )
            log.info("[CMD_UPDATE] [DROPDOWN_SHOWN]")
            return

        sanitised = _sanitise(args, MAX_COMMAND_LEN)

        # ── Rename: "old name > new name" ─────────────────────────────────
        if ">" in sanitised:
            parts = sanitised.split(">", 1)
            old_name = parts[0].strip()
            new_name = parts[1].strip()
            if not old_name or not new_name:
                await update.message.reply_text(
                    "Usage: /update old task name > new task name"
                )
                return
            matched = engine.rename_task(old_name, new_name)
            if matched:
                await update.message.reply_text(
                    f"✏️ Renamed: \"{matched}\" → \"{new_name}\"\n"
                    "(Personal tasks synced to Google Tasks)"
                )
                log.info("[CMD_UPDATE] [RENAME] [OK]")
            else:
                await update.message.reply_text(
                    f"No task matching \"{old_name}\" found. Check /tasks for exact names."
                )
            return

        # ── Priority change: any text containing p0–p3 ────────────────────
        p_m = re.search(r"\bp([0-3])\b", sanitised, re.IGNORECASE)
        if p_m and not sanitised.lower().startswith("done "):
            new_priority = f"P{p_m.group(1).upper()}"
            task_name = (sanitised[:p_m.start()] + sanitised[p_m.end():]).strip()
            task_name = re.sub(r"\s{2,}", " ", task_name).strip()
            if not task_name:
                await update.message.reply_text(
                    "Usage: /update [task name] p0  — e.g. /update publish article p1"
                )
                return
            matched = engine.set_task_priority(task_name, new_priority)
            if matched:
                badge = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}.get(new_priority, "")
                await update.message.reply_text(
                    f"{badge} Priority set to {new_priority}: {matched}"
                )
                log.info(f"[CMD_UPDATE] [PRIORITY] [{new_priority}] [OK]")
            else:
                await update.message.reply_text(
                    f"No work task matching \"{task_name}\" found. Check /tasks for names."
                )
            return

        # ── Quick done shortcut: "done [task name]" ───────────────────────
        if sanitised.lower().startswith("done "):
            task_name = sanitised[5:].strip()
            matched = engine.mark_task_done(task_name)
            if matched:
                await update.message.reply_text(f"✅ Marked done: {matched}")
                log.info("[CMD_UPDATE] [DONE_SHORTCUT] [OK]")
            else:
                await update.message.reply_text(
                    f"No task matching \"{task_name}\" found. Try /done for the dropdown."
                )
            return

        # ── Unrecognised text ─────────────────────────────────────────────
        await update.message.reply_text(
            "📋 /update options:\n\n"
            "• /update — dropdown to mark done or reschedule\n"
            "• /update old name > new name — rename a task\n"
            "• /update [task] p0 — change priority (p0–p3)\n"
            "• /update done [task name] — quick mark done\n"
            "• /append [task] + [text] — append text to a task title"
        )

    except Exception:
        log.error("[CMD_UPDATE] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_append(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/append [task] + [text] — fuzzy-find a task and append text to its title."""
    if not await _guard(update):
        return
    log.info("[CMD_APPEND] [START]")
    try:
        raw = _sanitise(update.message.text.partition(" ")[2].strip())
        if not raw or "+" not in raw:
            await update.message.reply_text(
                "Usage: /append [task name] + [text to add]\n"
                "Example: /append dentist + call to confirm time",
                parse_mode=None,
            )
            return
        parts = raw.split("+", 1)
        task_part = parts[0].strip()
        extra = parts[1].strip()
        if not task_part or not extra:
            await update.message.reply_text(
                "Usage: /append [task name] + [text to add]",
                parse_mode=None,
            )
            return
        new_title = engine.append_to_task(task_part, extra)
        if new_title:
            await update.message.reply_text(f"✏️ Updated: \"{new_title}\"")
            log.info("[CMD_APPEND] [OK]")
        else:
            await update.message.reply_text(
                f"No task matching \"{task_part}\" found. Check /tasks for names.",
                parse_mode=None,
            )
    except Exception:
        log.error("[CMD_APPEND] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_weekend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_WEEKEND] [START]")
    try:
        briefing = engine.build_weekend_briefing()
        await update.message.reply_text(briefing, parse_mode="HTML")
        log.info("[CMD_WEEKEND] [OK]")
    except Exception:
        log.error("[CMD_WEEKEND] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_HELP] [OK]")
    await update.message.reply_text(
        "📋 Available commands:\n\n"
        "/morning — Morning refresh\n"
        "/evening — Evening prep\n"
        "/tasks — Full task list\n"
        "/done — Mark task done (dropdown)\n\n"
        "Adding tasks:\n"
        "  /add [task] — personal, today\n"
        "  /add [task] on [date] — personal, specific date\n"
        "  /add [task] on [date] every [freq] — personal, recurring\n"
        "  /add w [task] p[0-3] — work task with priority\n"
        "  /add w [task] p1 on [date] — work + priority + date\n"
        "  Dates: tomorrow, friday, 5 may, next week\n"
        "  Freq: daily, weekly, monthly, every 2 weeks\n\n"
        "Updating tasks:\n"
        "  /update — dropdown: ✅ Done | 📅 Today | 📅 +1 day\n"
        "  /update old > new — rename a task\n"
        "  /update [task] p0 — change priority (p0–p3)\n"
        "  /update done [task] — quick mark done\n\n"
        "/weekend — Personal schedule only\n\n"
        "Gym:\n"
        "  /days MON WED FRI — Schedule gym for those days next week\n"
        "  /skip — Skip today, cascade remaining splits\n"
        "  /status — This week's scheduled gym sessions\n"
        "  /next — Show next split without changing state\n"
        "  /reschedule [date] [time] — Schedule on a date/time (e.g. /reschedule 5 may 3pm)\n"
        "  /setsplit [date] [Push/Pull/Legs/Rest] — Change a day's split and cascade\n\n"
        "  /append [task] + [text] — Append text to a task title\n\n"
        "/help — This message",
        parse_mode=None,
    )


_DAY_ABBREVS = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3,
    "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6,
}

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_gym_date(text: str) -> Optional[date]:
    """
    Parse a user-supplied date string into a date.
    Supports: YYYY-MM-DD, D Mon, D Month, Mon D.
    Returns None if unparseable.
    """
    t = text.strip().lower()
    today = date.today()

    if t in ("today",):
        return today
    if t in ("tomorrow",):
        return today + timedelta(days=1)

    # Weekday names: "friday", "fri", "monday", etc. → next occurrence
    _WD_NAMES = {
        "mon": 0, "monday": 0, "tue": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2, "thu": 3, "thursday": 3,
        "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }
    if t in _WD_NAMES:
        target_wd = _WD_NAMES[t]
        days_ahead = (target_wd - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    # YYYY-MM-DD
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass

    # "5 may" or "5may" or "may 5"
    import re as _re
    m = _re.match(r"^(\d{1,2})\s*([a-z]+)$", t) or _re.match(r"^([a-z]+)\s*(\d{1,2})$", t)
    if m:
        parts = m.groups()
        try:
            day_part = int(parts[0]) if parts[0].isdigit() else int(parts[1])
            mon_part = parts[1] if parts[0].isdigit() else parts[0]
            month_num = _MONTH_NAMES.get(mon_part[:3])
            if month_num:
                yr = today.year
                d = date(yr, month_num, day_part)
                if d < today:
                    d = date(yr + 1, month_num, day_part)
                return d
        except (ValueError, TypeError):
            pass

    return None


async def cmd_gym_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_GYM_SKIP] [START]")
    try:
        today = date.today()
        await update.message.reply_text("Skipping today and updating this week's sessions…")
        deleted, cascade = gym.cascade_skip(today)
        cal_note = "Today's event removed." if deleted else "No gym event found for today."
        reply = f"Got it — skipping today. {cal_note} 💪"
        if cascade:
            reply += "\n\nUpdated sessions this week:\n" + "\n".join(cascade)
        else:
            reply += "\nNo other sessions this week to update."
        await update.message.reply_text(reply, parse_mode=None)
        log.info("[CMD_GYM_SKIP] [OK]")
    except Exception:
        log.error("[CMD_GYM_SKIP] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_gym_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_GYM_STATUS] [START]")
    try:
        sessions = gym.get_week_sessions()
        current = gym.current_split_name()
        if not sessions:
            await update.message.reply_text(
                f"No gym sessions scheduled this week.\n"
                f"Current split position: {current} Day.\n"
                f"Use /days to schedule.",
                parse_mode=None,
            )
            return
        lines = [f"🏋️ This week's gym sessions:\n"]
        for s in sessions:
            lines.append(f"  {s['weekday']} {s['date'].strftime('%-d %b')}: {s['time_str']} — {s['split']} Day")
        lines.append(f"\nCurrent split: {current} Day")
        await update.message.reply_text("\n".join(lines), parse_mode=None)
        log.info("[CMD_GYM_STATUS] [OK]")
    except Exception:
        log.error("[CMD_GYM_STATUS] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_gym_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_GYM_NEXT] [OK]")
    try:
        current = gym.current_split_name()
        nxt = gym.next_split_name()
        await update.message.reply_text(
            f"Current: {current} Day\nNext up: {nxt} Day",
            parse_mode=None,
        )
    except Exception:
        log.error("[CMD_GYM_NEXT] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


def _parse_gym_time(text: str) -> Optional[time]:
    """Parse '3pm', '14:00', '2:30pm' → time object."""
    t = text.strip().lower()
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', t)
    if m:
        h, mins, period = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if period == 'pm' and h != 12:
            h += 12
        elif period == 'am' and h == 12:
            h = 0
        try:
            return time(h, mins)
        except ValueError:
            pass
    m = re.match(r'^(\d{1,2}):(\d{2})$', t)
    if m:
        try:
            return time(int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def _parse_gym_datetime(raw: str):
    """
    Parse 'date [time]' from a reschedule arg.
    Tries last 1-2 tokens as a time, remainder as date.
    Returns (date | None, time | None).
    """
    tokens = raw.strip().split()
    # Try last 2 tokens as time (e.g. "2:30 pm"), then last 1 token (e.g. "3pm", "14:00")
    for n in (2, 1):
        if len(tokens) <= n:
            continue
        time_str = " ".join(tokens[-n:])
        candidate_time = _parse_gym_time(time_str)
        if candidate_time is not None:
            date_str = " ".join(tokens[:-n])
            candidate_date = _parse_gym_date(date_str)
            if candidate_date is not None:
                log.info(f"[PARSE_GYM_DATETIME] [raw={raw!r}] [date={candidate_date}] [time={candidate_time}]")
                return candidate_date, candidate_time
    # No time found — parse whole input as date
    result_date = _parse_gym_date(raw.strip())
    log.info(f"[PARSE_GYM_DATETIME] [raw={raw!r}] [date={result_date}] [time=None]")
    return result_date, None


async def cmd_gym_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_GYM_RESCHEDULE] [START]")
    try:
        raw = update.message.text.partition(" ")[2].strip()
        if not raw:
            await update.message.reply_text(
                "Schedule a gym session ON a specific date/time.\n"
                "Examples:\n"
                "  /reschedule 2026-05-05\n"
                "  /reschedule 5 may 3pm\n"
                "  /reschedule 2026-05-05 14:00",
                parse_mode=None,
            )
            return
        target, forced_time = _parse_gym_datetime(_sanitise(raw))
        if target is None:
            await update.message.reply_text(
                f"Couldn't parse date from '{raw}'.\nTry: /reschedule 2026-05-05 or /reschedule 5 may 3pm",
                parse_mode=None,
            )
            return
        time_note = f" at {forced_time.strftime('%-I:%M %p')}" if forced_time else ""
        await update.message.reply_text(
            f"Scheduling gym session for {target.strftime('%a %-d %b')}{time_note} and cascading this week…"
        )
        results = gym.cascade_reschedule(target, forced_time)
        if results:
            await update.message.reply_text("\n".join(results), parse_mode=None)
        else:
            await update.message.reply_text("Something went wrong. Check Railway logs.")
        log.info("[CMD_GYM_RESCHEDULE] [OK]")
    except Exception:
        log.error("[CMD_GYM_RESCHEDULE] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_gym_setsplit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_GYM_SETSPLIT] [START]")
    try:
        raw = _sanitise(update.message.text.partition(" ")[2].strip())
        _SPLIT_ALIASES = {
            "push": "Push", "pull": "Pull", "legs": "Legs", "leg": "Legs",
            "rest": "Rest", "off": "Rest",
        }
        # Expect: "[date] [split]" or just "[split]" (defaults to today)
        tokens = raw.lower().split()
        if not tokens:
            await update.message.reply_text(
                "Usage: /setsplit [date] [Push/Pull/Legs/Rest]\n"
                "Examples:\n  /setsplit Push  (changes today)\n  /setsplit 5 may Legs",
                parse_mode=None,
            )
            return

        # Last token is the split name
        split_raw = tokens[-1]
        new_split = _SPLIT_ALIASES.get(split_raw)
        if new_split is None:
            await update.message.reply_text(
                f"Unknown split '{split_raw}'. Use: Push, Pull, Legs, or Rest.",
                parse_mode=None,
            )
            return

        date_part = " ".join(tokens[:-1]).strip()
        target = _parse_gym_date(date_part) if date_part else date.today()
        if target is None:
            await update.message.reply_text(
                f"Couldn't parse date '{date_part}'. Try: /setsplit 5 may Push",
                parse_mode=None,
            )
            return

        await update.message.reply_text(
            f"Setting {target.strftime('%a %-d %b')} to {new_split} Day and cascading…"
        )
        results = gym.setsplit_and_cascade(target, new_split)
        await update.message.reply_text("\n".join(results) if results else "Done.", parse_mode=None)
        log.info(f"[CMD_GYM_SETSPLIT] [{new_split}] [OK]")
    except Exception:
        log.error("[CMD_GYM_SETSPLIT] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_gym_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_GYM_DAYS] [START]")
    try:
        raw = update.message.text.partition(" ")[2].strip().upper()
        if not raw:
            await update.message.reply_text(
                "Usage: /days MON WED FRI\nOptions: MON TUE WED THU FRI SAT SUN",
                parse_mode=None,
            )
            return

        tokens = raw.split()
        weekday_nums = []
        unknown = []
        for tok in tokens:
            if tok in _DAY_ABBREVS:
                wd = _DAY_ABBREVS[tok]
                if wd not in weekday_nums:
                    weekday_nums.append(wd)
            else:
                unknown.append(tok)

        if unknown:
            await update.message.reply_text(
                f"Unrecognised days: {', '.join(unknown)}. Use MON TUE WED THU FRI SAT SUN.",
                parse_mode=None,
            )
            return
        if not weekday_nums:
            await update.message.reply_text("No valid days found.", parse_mode=None)
            return

        weekday_nums.sort()

        today = date.today()
        # Next Monday (always schedule for NEXT week, not current week)
        days_to_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + timedelta(days=days_to_monday)

        results = []
        for wd in weekday_nums:
            target = next_monday + timedelta(days=wd)
            ok, msg = gym.schedule_gym_session(target)
            results.append(msg)

        await update.message.reply_text("\n".join(results), parse_mode=None)
        log.info(f"[CMD_GYM_DAYS] [OK] [{len(weekday_nums)}_DAYS]")
    except Exception:
        log.error("[CMD_GYM_DAYS] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        chat_id = update.message.chat_id
        if not _is_authorised(chat_id):
            log.warning(f"[UNAUTHORISED_ACCESS] [UNKNOWN_CMD] [{datetime.now(timezone.utc).isoformat()}]")
            return
        log.info("[UNKNOWN_COMMAND] [RECEIVED]")
        await update.message.reply_text(UNKNOWN_CMD)


# ── Callback handlers ──────────────────────────────────────────────────────────

async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dismiss / cancel inline keyboard."""
    query = update.callback_query
    await query.answer()
    if not await _guard_callback(update):
        return
    try:
        await query.delete_message()
    except Exception:
        pass


async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /done dropdown selections (dp: personal, dw: work)."""
    query = update.callback_query
    await query.answer()
    if not await _guard_callback(update):
        return

    data = query.data or ""
    chat_id = query.message.chat_id

    cached = _task_dropdown_cache.get(chat_id)
    if not cached:
        await query.edit_message_text("Session expired. Please run /done again.")
        return

    if data.startswith("dp:"):
        idx = int(data[3:])
        tasks = cached.get("personal", [])
        label = "Personal"
    elif data.startswith("dw:"):
        idx = int(data[3:])
        tasks = cached.get("work", [])
        label = "Work"
    else:
        return

    if idx >= len(tasks):
        await query.edit_message_text("Task not found. Please run /done again.")
        return

    task = tasks[idx]
    title = task["title"]

    if task["source"] == "google":
        success = engine.mark_google_task_done_by_id(
            task["task_list_id"], task["task_id"], task["label"], title=title
        )
    else:
        success = engine.mark_local_task_done_by_title(title)

    if success:
        await query.edit_message_text(f"✅ Marked done: {title}")
        log.info(f"[CB_DONE] [{label}] [OK]")
    else:
        await query.edit_message_text(
            f"Could not mark done: {title}. Try /done [task name] instead."
        )
        log.error(f"[CB_DONE] [{label}] [FAIL]")

    _task_dropdown_cache.pop(chat_id, None)


async def cb_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /update dropdown action callbacks.
    Format: ua:<type>:<idx>:<action>
      type   = p (personal) | w (work)
      idx    = task index in cached list
      action = done | today | tom
    """
    query = update.callback_query
    await query.answer()
    if not await _guard_callback(update):
        return

    data = query.data or ""
    chat_id = query.message.chat_id

    # Parse: ua:p:3:today
    parts = data.split(":")  # ['ua', 'p', '3', 'today']
    if len(parts) != 4:
        return
    _, typ, idx_str, action = parts
    idx = int(idx_str)

    cached = _update_dropdown_cache.get(chat_id)
    if not cached:
        await query.edit_message_text("Session expired. Please run /update again.")
        return

    tasks = cached.get("personal" if typ == "p" else "work", [])
    if idx >= len(tasks):
        await query.edit_message_text("Task not found. Please run /update again.")
        return

    task = tasks[idx]
    title = task["title"]
    label = task["label"]  # "Personal" or "Work"
    today = date.today()
    tomorrow = today + timedelta(days=1)

    success = False
    result_msg = ""

    if action == "done":
        if task["source"] == "google":
            success = engine.mark_google_task_done_by_id(
                task["task_list_id"], task["task_id"], label, title=title
            )
        else:
            success = engine.mark_local_task_done_by_title(title)
        result_msg = f"✅ Marked done: {title}" if success else f"Could not mark done: {title}"

    elif action in ("today", "tom"):
        new_date = today if action == "today" else tomorrow
        date_label = "today" if action == "today" else "tomorrow"
        if task["source"] == "google":
            success = engine.reschedule_google_task(
                task["task_list_id"], task["task_id"], label, new_date
            )
        else:
            success = engine.reschedule_local_task_by_title(title, new_date)
        result_msg = (
            f"📅 Rescheduled to {date_label}: {title}"
            if success
            else f"Could not reschedule: {title}"
        )

    await query.edit_message_text(result_msg)
    log.info(f"[CB_UPDATE] [{label}] [{action.upper()}] [{'OK' if success else 'FAIL'}]")
    _update_dropdown_cache.pop(chat_id, None)


# ── Application builder ────────────────────────────────────────────────────────

def build_application() -> Application:
    validate_token()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("evening", cmd_evening))
    app.add_handler(CommandHandler("tasks",   cmd_tasks))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("update",  cmd_update))
    app.add_handler(CommandHandler("append",  cmd_append))
    app.add_handler(CommandHandler("weekend",    cmd_weekend))
    app.add_handler(CommandHandler("help",       cmd_help))

    # Gym scheduler commands
    app.add_handler(CommandHandler("skip",       cmd_gym_skip))
    app.add_handler(CommandHandler("status",     cmd_gym_status))
    app.add_handler(CommandHandler("next",       cmd_gym_next))
    app.add_handler(CommandHandler("reschedule", cmd_gym_reschedule))
    app.add_handler(CommandHandler("days",       cmd_gym_days))
    app.add_handler(CommandHandler("setsplit",   cmd_gym_setsplit))

    # Inline keyboard callbacks — matched by prefix to avoid cross-handler firing
    app.add_handler(CallbackQueryHandler(cb_noop,   pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(cb_done,   pattern=r"^d[pw]:"))
    app.add_handler(CallbackQueryHandler(cb_update, pattern=r"^ua:"))

    # Catch-all: unrecognised commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    log.info("[BOT_BUILD] [OK]")
    return app


async def send_message(text: str) -> None:
    """Helper for scheduler to push proactive briefings."""
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    chat_id = _get_allowed_chat_id()
    if chat_id is None:
        log.error("[SEND_MESSAGE] [NO_CHAT_ID]")
        return
    try:
        for i in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=chat_id,
                text=text[i:i+4000],
                parse_mode="HTML",
            )
        log.info("[SEND_MESSAGE] [OK]")
    except Exception:
        log.error("[SEND_MESSAGE] [FAIL]")

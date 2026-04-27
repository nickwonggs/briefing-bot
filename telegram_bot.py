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
import time
import logging
import logging.handlers
from datetime import datetime, timezone, date, timedelta
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
    now = time.monotonic()
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
        "Commands:\n"
        "/morning — Morning refresh\n"
        "/evening — Evening prep\n"
        "/tasks — Current task list\n"
        "/done — Mark task done (dropdown)\n"
        "/add [task] — Add personal task (synced to Google)\n"
        "/add w [task] — Add work task (local only)\n"
        "/update — Update tasks: reschedule or rename\n"
        "/weekend — Personal schedule only\n"
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

    # Date: "on [date]" at the end
    due_date = None
    on_m = re.search(r"\s+on\s+(.+)$", raw, re.IGNORECASE)
    if on_m:
        due_date = engine.parse_task_date(on_m.group(1).strip())
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
            sync_note = " ✅ Synced to Google Tasks" if ok else " ⚠️ Google sync failed"

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
            "• /update done [task name] — quick mark done"
        )

    except Exception:
        log.error("[CMD_UPDATE] [FAIL]")
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
        "  /update done [task] — quick mark done\n\n"
        "/weekend — Personal schedule only\n"
        "/help — This message",
        parse_mode=None,
    )


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
            task["task_list_id"], task["task_id"], task["label"]
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
                task["task_list_id"], task["task_id"], label
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
    app.add_handler(CommandHandler("weekend", cmd_weekend))
    app.add_handler(CommandHandler("help",    cmd_help))

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

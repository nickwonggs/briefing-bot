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
            label = t["title"][:40] + ("…" if len(t["title"]) > 40 else "")
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
            name = t["title"][:46] + ("…" if len(t["title"]) > 46 else "")
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


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_ADD] [START]")
    try:
        args = update.message.text.partition(" ")[2].strip()
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "  /add [task] — personal task (synced to Google Tasks + Calendar)\n"
                "  /add w [task] — work task (local only)"
            )
            return

        is_work = False
        if args.lower().startswith("w ") and len(args) > 2:
            is_work = True
            args = args[2:].strip()

        task_title = _sanitise(args, MAX_ADD_LEN)
        if not task_title:
            await update.message.reply_text("Task description cannot be empty.")
            return

        if is_work:
            engine.add_task(task_title, source="Manual", task_type="💼 Work")
            await update.message.reply_text(f"✅ Work task added: {task_title}")
            log.info("[CMD_ADD] [WORK] [OK]")
        else:
            engine.add_task(task_title, source="Manual", task_type="👤 Personal")
            synced = engine.add_personal_google_task(task_title)
            sync_note = " (synced to Google Tasks)" if synced else " (local only — Google sync failed)"
            await update.message.reply_text(f"✅ Personal task added: {task_title}{sync_note}")
            log.info(f"[CMD_ADD] [PERSONAL] [GOOGLE_SYNC={'OK' if synced else 'FAIL'}]")
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
        "/done — Mark task done (dropdown)\n"
        "/add [task] — Add personal task (synced to Google)\n"
        "/add w [task] — Add work task (local only)\n\n"
        "/update — Update tasks via dropdown:\n"
        "   ✅ Done | 📅 Reschedule to today | 📅 +1 day\n"
        "/update old name > new name — Rename a task\n"
        "/update done [task] — Quick mark done\n\n"
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

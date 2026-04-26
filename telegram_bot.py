"""
telegram_bot.py — Telegram interface with full security hardening.

Security model:
- Chat ID whitelist enforced on every message
- Rate limiting: 3-second cooldown per Chat ID
- Input sanitisation + length limits
- Replay attack prevention via update_id tracking
- No personal data ever sent to chat
- Unknown commands logged but not echoed
"""

import os
import re
import time
import logging
import logging.handlers
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import briefing_engine as engine

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_PATH = "briefing_bot.log"
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3
)
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s]"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("telegram_bot")

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID_STR = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{35,}$")
_SAFE_TEXT_RE = re.compile(r"[^a-zA-Z0-9 ,.\-_/@#!\?]")

MAX_COMMAND_LEN = 500
MAX_ADD_LEN = 200
RATE_LIMIT_SECONDS = 3
GENERIC_ERROR = "Something went wrong. Please try again later."
UNKNOWN_CMD = "Unknown command. Send /help for the list."


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


# ── In-memory security state ───────────────────────────────────────────────────
_last_command_time: dict[int, float] = {}
_processed_update_ids: set[int] = set()


def _sanitise(text: str, max_len: int = MAX_COMMAND_LEN) -> str:
    """Strip dangerous characters and enforce length limit."""
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
    # Keep set bounded to last 10k IDs
    if len(_processed_update_ids) > 10000:
        oldest = min(_processed_update_ids)
        _processed_update_ids.discard(oldest)
    return False


async def _guard(update: Update) -> bool:
    """
    Returns True if the update should be processed.
    Silent drop + log for all security violations.
    """
    if update.update_id and _is_replay(update.update_id):
        return False

    msg = update.message or update.edited_message
    if not msg:
        return False

    chat_id = msg.chat_id

    if not _is_authorised(chat_id):
        log.warning(f"[UNAUTHORISED_ACCESS] [CHAT_ID_BLOCKED] [{datetime.now(timezone.utc).isoformat()}]")
        return False  # Silent drop — no response

    if _is_rate_limited(chat_id):
        return False  # Silent drop

    text = msg.text or ""
    if len(text) > MAX_COMMAND_LEN:
        await msg.reply_text(GENERIC_ERROR)
        log.info("[INPUT_VALIDATION] [TOO_LONG]")
        return False

    return True


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
        "/done [task] — Mark task done\n"
        "/add [task] — Add new task\n"
        "/update [text] — Log completions and new tasks\n"
        "/weekend — Personal schedule only\n"
        "/help — This list",
        parse_mode=None,
    )


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_MORNING] [START]")
    try:
        briefing = engine.build_morning_briefing()
        await update.message.reply_text(briefing, parse_mode="Markdown")
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
        await update.message.reply_text(briefing, parse_mode="Markdown")
        log.info("[CMD_EVENING] [OK]")
    except Exception:
        log.error("[CMD_EVENING] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_TASKS] [START]")
    try:
        from datetime import date
        today_wd = date.today().weekday()
        text = engine.get_task_list_text(weekend=(today_wd >= 5))
        await update.message.reply_text(text, parse_mode="Markdown")
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
        task_name = _sanitise(args, MAX_COMMAND_LEN)
        if not task_name:
            await update.message.reply_text("Usage: /done [task name]")
            return
        matched = engine.mark_task_done(task_name)
        if matched:
            await update.message.reply_text(f"✅ Marked done: {matched}")
            log.info("[CMD_DONE] [OK]")
        else:
            await update.message.reply_text("No matching task found. Check /tasks for exact names.")
    except Exception:
        log.error("[CMD_DONE] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_ADD] [START]")
    try:
        args = update.message.text.partition(" ")[2].strip()
        task_title = _sanitise(args, MAX_ADD_LEN)
        if not task_title:
            await update.message.reply_text("Usage: /add [task description] (max 200 chars)")
            return
        engine.add_task(task_title, source="Manual")
        await update.message.reply_text(f"✅ Task added: {task_title}")
        log.info("[CMD_ADD] [OK]")
    except Exception:
        log.error("[CMD_ADD] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_UPDATE] [START]")
    try:
        args = update.message.text.partition(" ")[2].strip()
        sanitised = _sanitise(args, MAX_COMMAND_LEN)
        if not sanitised:
            await update.message.reply_text(
                "Usage: /update finished X, new task Y, FYI Z (max 500 chars)"
            )
            return
        reply = engine.process_update(sanitised)
        await update.message.reply_text(reply)
        log.info("[CMD_UPDATE] [OK]")
    except Exception:
        log.error("[CMD_UPDATE] [FAIL]")
        await update.message.reply_text(GENERIC_ERROR)


async def cmd_weekend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    log.info("[CMD_WEEKEND] [START]")
    try:
        briefing = engine.build_weekend_briefing()
        await update.message.reply_text(briefing, parse_mode="Markdown")
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
        "/start — Confirm bot is online\n"
        "/morning — Morning refresh now\n"
        "/evening — Evening prep now\n"
        "/tasks — Full task list\n"
        "/done [task] — Mark task done (fuzzy match)\n"
        "/add [task] — Add new task (max 200 chars)\n"
        "/update [text] — Log completions/new tasks (max 500 chars)\n"
        "/weekend — Personal schedule only\n"
        "/help — This message",
        parse_mode=None,
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for unrecognised commands."""
    if update.message:
        chat_id = update.message.chat_id
        if not _is_authorised(chat_id):
            log.warning(f"[UNAUTHORISED_ACCESS] [UNKNOWN_CMD] [{datetime.now(timezone.utc).isoformat()}]")
            return
        log.info("[UNKNOWN_COMMAND] [RECEIVED]")
        await update.message.reply_text(UNKNOWN_CMD)


# ── Application builder ────────────────────────────────────────────────────────

def build_application() -> Application:
    validate_token()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("evening", cmd_evening))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("weekend", cmd_weekend))
    app.add_handler(CommandHandler("help", cmd_help))

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
        # Telegram message limit is 4096 chars; split if needed
        for i in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=chat_id,
                text=text[i:i+4000],
                parse_mode="Markdown",
            )
        log.info("[SEND_MESSAGE] [OK]")
    except Exception:
        log.error("[SEND_MESSAGE] [FAIL]")

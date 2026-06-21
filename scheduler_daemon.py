"""
scheduler_daemon.py — Always-on process runner.

Responsibilities:
- Runs Telegram bot polling continuously (interactive commands: /done, /add,
  /days, tap-to-complete buttons). This is the ONLY reason Railway runs.
- Fires the daily 03:00 SGT clean shutdown (Railway uptime cap workaround).
- Health check HTTP endpoint on port 8080 (200 OK only)
- Top-level exception handler: log error code, wait 30s, restart
- Log rotation: 10MB per file, 3 rotations max

All scheduled briefings (morning/evening/loyalty/gym) fire from GitHub Actions
(.github/workflows/briefings.yml), not here — so they survive Railway being
asleep. Do NOT re-add briefing schedules below or they'll double-fire.
"""

# stdlib imports first
import logging
import logging.handlers
import os
import signal
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Logging must be configured BEFORE importing project modules ────────────────
# briefing_engine and telegram_bot call logging.getLogger() at module level;
# basicConfig here ensures the root logger has both file + stdout handlers
# before any module-level code in those files runs.
_LOG_PATH = "briefing_bot.log"
_LOG_FMT = "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(logging.Formatter(_LOG_FMT))
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter(_LOG_FMT))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stdout_handler])

# Third-party imports
import schedule
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# Project imports (run after logging is configured)
import briefing_engine as engine
import telegram_bot as tbot

load_dotenv()

log = logging.getLogger("scheduler")

TZ = ZoneInfo("Asia/Singapore")

# ── Health check ───────────────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress default HTTP server logs (they'd expose request details)


def _start_health_server(port: int = 8080) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"[HEALTH_SERVER] [OK] [PORT_{port}]")


# ── Scheduled jobs ─────────────────────────────────────────────────────────────

def _run_daily_shutdown() -> None:
    """Clean exit at 03:00 SGT so Railway (ON_FAILURE policy) won't restart.
    GitHub Actions redeploys the service after the LA peak gate ends (~11:00
    SGT) to wake it for interactive commands — see bot-restart.yml. Uptime
    ~11:00–03:00 SGT ≈ 16 hrs/day ≈ 480 hrs/month, under the 500 hr free-tier
    cap. All briefings run on GitHub Actions, so the bot being down overnight
    only costs interactive commands, never a briefing."""
    log.info("[SCHEDULED_SHUTDOWN] [DAILY] [BACKUP_START]")
    engine.backup_task_state_to_drive()
    log.info("[SCHEDULED_SHUTDOWN] [DAILY] [SENDING_SIGTERM]")
    os.kill(os.getpid(), signal.SIGTERM)


def _setup_schedules() -> None:
    # Times in Singapore time (SGT = UTC+8); schedule lib respects the TZ env var.
    # Only the daily shutdown lives here. All briefings run on GitHub Actions
    # (.github/workflows/briefings.yml) — adding them back here double-fires them.
    schedule.every().day.at("03:00").do(_run_daily_shutdown)
    log.info("[SCHEDULES_REGISTERED] [03:00_SHUTDOWN]")


def _run_schedule_loop() -> None:
    """Run schedule in a background thread."""
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Main loop ──────────────────────────────────────────────────────────────────

def _shutdown(signum, frame):
    log.info("[SHUTDOWN] [SIGNAL_RECEIVED]")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("[DAEMON_START] [OK]")
    engine.restore_task_state_from_drive()

    _start_health_server(port=int(os.getenv("PORT", "8080")))
    _setup_schedules()

    # Schedule loop in background thread
    sched_thread = threading.Thread(target=_run_schedule_loop, daemon=True)
    sched_thread.start()

    # Telegram bot polling in foreground — restart on any unhandled exception
    while True:
        try:
            log.info("[BOT_START] [OK]")
            app = tbot.build_application()
            app.run_polling(drop_pending_updates=True)
        except SystemExit:
            log.info("[DAEMON_STOP] [SYSTEM_EXIT]")
            break
        except Exception as exc:
            log.error(f"[BOT_CRASH] [RESTARTING] [{type(exc).__name__}]")
            time.sleep(30)
            log.info("[BOT_RESTART] [ATTEMPT]")


if __name__ == "__main__":
    main()

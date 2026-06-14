"""
scheduler_daemon.py — Always-on process runner.

Responsibilities:
- Runs Telegram bot polling continuously
- Fires evening briefing at 17:00 SGT daily
- Fires morning briefing at 09:00 SGT daily
- Health check HTTP endpoint on port 8080 (200 OK only)
- Top-level exception handler: log error code, wait 30s, restart
- Log rotation: 10MB per file, 3 rotations max
"""

# stdlib imports first
import asyncio
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
import gym_engine as gym
import loyalty_lobby
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

def _run_evening_briefing() -> None:
    log.info("[SCHEDULED_EVENING] [START]")
    try:
        target = engine.get_next_briefing_target()
        text = engine.build_evening_briefing(target)
        asyncio.run(tbot.send_message(text))
        log.info("[SCHEDULED_EVENING] [OK]")
    except Exception as exc:
        log.error(f"[SCHEDULED_EVENING] [FAIL] [{type(exc).__name__}]")


def _run_morning_briefing() -> None:
    log.info("[SCHEDULED_MORNING] [START]")
    try:
        text = engine.build_morning_briefing()
        asyncio.run(tbot.send_message(text))
        log.info("[SCHEDULED_MORNING] [OK]")
    except Exception as exc:
        log.error(f"[SCHEDULED_MORNING] [FAIL] [{type(exc).__name__}]")


def _run_loyalty_lobby_digest() -> None:
    log.info("[SCHEDULED_LOYALTY_LOBBY] [START]")
    try:
        loyalty_lobby.send_digest()
        log.info("[SCHEDULED_LOYALTY_LOBBY] [OK]")
    except Exception as exc:
        log.error(f"[SCHEDULED_LOYALTY_LOBBY] [FAIL] [{type(exc).__name__}]")


def _run_gym_checkin() -> None:
    log.info("[SCHEDULED_GYM_CHECKIN] [START]")
    try:
        current = gym.current_split_name()
        nxt = gym.next_split_name()
        text = (
            "🏋️ Weekly Gym Check-in!\n\n"
            "Which days are you planning to gym next week?\n"
            "Reply with: /days MON TUE WED THU FRI SAT SUN\n\n"
            f"Current split: {current} Day — next session will be {nxt} Day."
        )
        asyncio.run(tbot.send_message(text))
        log.info("[SCHEDULED_GYM_CHECKIN] [OK]")
    except Exception as exc:
        log.error(f"[SCHEDULED_GYM_CHECKIN] [FAIL] [{type(exc).__name__}]")


def _run_daily_shutdown() -> None:
    """Clean exit at 03:00 SGT so Railway (ON_FAILURE policy) won't restart.
    GitHub Actions redeploys the service at 13:00 SGT (05:00 UTC, off-peak for
    the LA deploy gate). Uptime ~13:00–03:00 SGT ≈ 14 hrs/day ≈ 420 hrs/month,
    under the 500 hr free-tier cap. The 08:00 SGT morning briefing runs on
    GitHub Actions instead, since Railway can't wake before ~13:00 SGT without
    hitting the peak-hour redeploy block."""
    log.info("[SCHEDULED_SHUTDOWN] [DAILY] [BACKUP_START]")
    engine.backup_task_state_to_drive()
    log.info("[SCHEDULED_SHUTDOWN] [DAILY] [SENDING_SIGTERM]")
    os.kill(os.getpid(), signal.SIGTERM)


def _setup_schedules() -> None:
    # Times in Singapore time (SGT = UTC+8)
    # schedule library uses datetime.now() which respects the TZ env var.
    # Morning briefing (08:00 SGT) runs on GitHub Actions — see
    # .github/workflows/briefings.yml — because Railway is asleep then.
    schedule.every().day.at("15:00").do(_run_loyalty_lobby_digest)
    schedule.every().day.at("19:00").do(_run_evening_briefing)
    schedule.every().friday.at("20:00").do(_run_gym_checkin)
    schedule.every().day.at("03:00").do(_run_daily_shutdown)
    log.info("[SCHEDULES_REGISTERED] [15:00_LOYALTY_LOBBY] [19:00_EVENING] [FRI_20:00_GYM_CHECKIN] [03:00_SHUTDOWN]")


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

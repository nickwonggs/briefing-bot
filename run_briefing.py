"""
run_briefing.py — One-shot briefing sender for GitHub Actions cron.

Fires a single briefing then exits. No daemon, no Telegram polling.
Lets scheduled briefings run independently of Railway, so they survive
Railway being asleep or blocked by the peak-hour deploy gate.

Task state is pulled from Google Drive first (Railway is the writer; this
process is a read-only consumer), so the briefing reflects the latest tasks.

Usage: python run_briefing.py <morning|evening|loyalty>
"""

import asyncio
import logging
import sys

# Configure logging before importing project modules (they grab loggers at
# module level — mirrors scheduler_daemon.py).
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

from dotenv import load_dotenv

import briefing_engine as engine
import loyalty_lobby
import telegram_bot as tbot

load_dotenv()

log = logging.getLogger("run_briefing")


def run_morning() -> None:
    engine.restore_task_state_from_drive()
    text = engine.build_morning_briefing()
    asyncio.run(tbot.send_message(text))


def run_evening() -> None:
    engine.restore_task_state_from_drive()
    target = engine.get_next_briefing_target()
    text = engine.build_evening_briefing(target)
    asyncio.run(tbot.send_message(text))


def run_loyalty() -> None:
    loyalty_lobby.send_digest()


_JOBS = {
    "morning": run_morning,
    "evening": run_evening,
    "loyalty": run_loyalty,
}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in _JOBS:
        log.error(f"Usage: python run_briefing.py <{'|'.join(_JOBS)}>")
        sys.exit(2)

    job = sys.argv[1]
    log.info(f"[RUN_BRIEFING] [{job.upper()}] [START]")
    try:
        _JOBS[job]()
        log.info(f"[RUN_BRIEFING] [{job.upper()}] [OK]")
    except Exception as exc:
        log.error(f"[RUN_BRIEFING] [{job.upper()}] [FAIL] [{type(exc).__name__}] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

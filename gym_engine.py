"""
gym_engine.py — Gym scheduler core logic.

Manages the Push/Pull/Legs/Rest rotation, finds free calendar slots via freebusy,
and creates/deletes events on the personal Gym calendar.
"""

import json
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from briefing_engine import _build_personal_credentials

log = logging.getLogger("gym_engine")

TZ = ZoneInfo("Asia/Singapore")

# ── Split rotation ──────────────────────────────────────────────────────────────
SPLIT_ROTATION = ["Push", "Pull", "Legs", "Push", "Pull", "Legs", "Rest"]
_STATE_FILE = os.path.join(os.getenv("TASK_DATA_DIR", "."), "split_state.json")

# ── Slot config ─────────────────────────────────────────────────────────────────
_SLOT_OPEN = time(10, 0)   # earliest allowed start
_SLOT_CLOSE = time(22, 0)  # session must END by this → last start is 8 PM
_PREFERRED = time(14, 0)   # ideal start (2 PM)
_DURATION_MINS = 120

# ── Workout templates ───────────────────────────────────────────────────────────
_PUSH_DESC = """\
💪 Push Day — Chest, Shoulders, Triceps
• Chest Press (Machine) — 2 x 6-8
• Incline Bench Press (Dumbbell) — 2 x 6-8
• Butterfly / Pec Deck — 2 x 6-8
• Overhead Press (Barbell) — 2 x 6-8
• Lateral Raise (Cable) — 2 x 6-8
• Triceps Rope Pushdown — 2 x 6-8

🚶 Cardio Finisher (12-3-30):
Incline treadmill walk — 12% incline, 3.0 mph, 30 minutes

⚡ Notes: Rest 2–3 mins between heavy sets. Stay in 6–8 rep range. If you hit 8 clean reps, add weight next session. Focus on progressive overload."""

_PULL_DESC = """\
💪 Pull Day — Back, Biceps
• Deadlift (Barbell) — 2 x 6-8
• Iso-Lateral Row (Machine) — 2 x 6-8
• Lat Pulldown (Cable) — 2 x 6-8
• Seated Cable Row V-Grip — 2 x 6-8
• Pull Up — 2 x 6-8
• Bicep Curl (Dumbbell) — 2 x 6-8

🚶 Cardio Finisher (12-3-30):
Incline treadmill walk — 12% incline, 3.0 mph, 30 minutes

⚡ Notes: Rest 2–3 mins between heavy sets. Stay in 6–8 rep range. If you hit 8 clean reps, add weight next session. Focus on progressive overload."""

_LEGS_DESC = """\
💪 Legs Day — Quads, Hamstrings, Glutes, Calves
• Squat (Barbell) — 2 x 6-8
• Bulgarian Split Squat — 2 x 6-8
• Romanian Deadlift (Dumbbell) — 2 x 6-8
• Seated Leg Curl (Machine) — 2 x 6-8
• Leg Extension (Machine) — 2 x 6-8
• Hip Thrust (Machine) — 2 x 6-8
• Calf Press (Machine) — 2 x 6-8

🚶 Cardio Finisher (12-3-30):
Incline treadmill walk — 12% incline, 3.0 mph, 30 minutes

⚡ Notes: Rest 2–3 mins between heavy sets. Stay in 6–8 rep range. If you hit 8 clean reps, add weight next session. Focus on progressive overload."""

_DESCRIPTIONS = {"Push": _PUSH_DESC, "Pull": _PULL_DESC, "Legs": _LEGS_DESC}


# ── Split state ─────────────────────────────────────────────────────────────────

def get_split_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"index": 0}


def save_split_state(index: int) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump({"index": index}, f)


def current_split_name() -> str:
    idx = get_split_state()["index"]
    return SPLIT_ROTATION[idx % len(SPLIT_ROTATION)]


def next_split_name() -> str:
    """Return the split that comes after the current one without changing state."""
    idx = get_split_state()["index"]
    return SPLIT_ROTATION[(idx + 1) % len(SPLIT_ROTATION)]


def advance_split() -> str:
    """Increment index and return the NEW current split name."""
    state = get_split_state()
    new_idx = (state["index"] + 1) % len(SPLIT_ROTATION)
    save_split_state(new_idx)
    return SPLIT_ROTATION[new_idx]


# ── Calendar helpers ─────────────────────────────────────────────────────────────

def _cal_service():
    creds = _build_personal_credentials()
    if not creds:
        raise RuntimeError("Personal credentials not available — check GOOGLE_TOKEN_JSON_PERSONAL")
    return build("calendar", "v3", credentials=creds)


def find_gym_calendar_id() -> Optional[str]:
    try:
        svc = _cal_service()
        items = svc.calendarList().list().execute().get("items", [])
        for cal in items:
            if "gym" in cal.get("summary", "").lower():
                return cal["id"]
        log.error("[GYM_ENGINE] [GYM_CAL_NOT_FOUND]")
        return None
    except Exception as exc:
        log.error(f"[GYM_ENGINE] [CAL_LIST_FAIL] [{type(exc).__name__}] {exc}")
        return None


def find_free_slot(target_date: date) -> Optional[datetime]:
    """
    Return the start datetime of the best free 2-hr slot on target_date.
    Boundaries: 10 AM – 10 PM (session must end by 10 PM → last valid start is 8 PM).
    Prefers 2 PM; walks outward in 30-min steps to find the nearest free slot.
    Returns None if no slot is available.
    """
    try:
        svc = _cal_service()

        midnight = datetime.combine(target_date, time(0, 0)).replace(tzinfo=TZ)
        day_start = datetime.combine(target_date, _SLOT_OPEN).replace(tzinfo=TZ)
        day_end = datetime.combine(target_date, _SLOT_CLOSE).replace(tzinfo=TZ)

        cal_items = svc.calendarList().list().execute().get("items", [])
        cal_ids = [{"id": c["id"]} for c in cal_items]
        if not cal_ids:
            return datetime.combine(target_date, _PREFERRED).replace(tzinfo=TZ)

        fb = svc.freebusy().query(body={
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items": cal_ids,
        }).execute()

        # Merge all busy intervals into (start_min, end_min) relative to midnight
        busy: list[tuple[int, int]] = []
        for cal_data in fb.get("calendars", {}).values():
            for period in cal_data.get("busy", []):
                bs = datetime.fromisoformat(period["start"].replace("Z", "+00:00")).astimezone(TZ)
                be = datetime.fromisoformat(period["end"].replace("Z", "+00:00")).astimezone(TZ)
                # Skip all-day events (represented as 00:00–00:00 in freebusy)
                if bs.time() == time(0, 0) and be.time() == time(0, 0):
                    continue
                busy.append((
                    int((bs - midnight).total_seconds() // 60),
                    int((be - midnight).total_seconds() // 60),
                ))

        def is_free(start_min: int) -> bool:
            end_min = start_min + _DURATION_MINS
            return all(end_min <= b[0] or start_min >= b[1] for b in busy)

        open_min = _SLOT_OPEN.hour * 60      # 600
        close_min = (_SLOT_CLOSE.hour * 60) - _DURATION_MINS  # 1200 → last start 8 PM
        preferred_min = _PREFERRED.hour * 60  # 840

        # Sort candidates by distance from 2 PM so nearest slot wins
        candidates = list(range(open_min, close_min + 1, 30))
        candidates.sort(key=lambda m: abs(m - preferred_min))

        for start_min in candidates:
            if is_free(start_min):
                return midnight + timedelta(minutes=start_min)

        return None

    except Exception as exc:
        log.error(f"[GYM_ENGINE] [FREEBUSY_FAIL] [{type(exc).__name__}] {exc}")
        return None


# ── Event creation / deletion ────────────────────────────────────────────────────

def schedule_gym_session(target_date: date) -> tuple[bool, str]:
    """
    Schedule a gym session for target_date using the current split position.
    Advances the split on success or when skipping a rest/no-slot day.
    Returns (success, human-readable message).
    """
    split = current_split_name()

    if split == "Rest":
        next_s = advance_split()
        return False, f"Today is a rest day in the rotation. Split advanced — next session: {next_s} Day."

    slot_start = find_free_slot(target_date)
    if slot_start is None:
        next_s = advance_split()
        return False, (
            f"No free 2-hour slot found on {target_date.strftime('%a %-d %b')}. "
            f"Skipped — next session: {next_s} Day."
        )

    gym_cal_id = find_gym_calendar_id()
    if gym_cal_id is None:
        return False, "Could not find a calendar named 'Gym'. Please create one in Google Calendar."

    slot_end = slot_start + timedelta(hours=2)
    desc = _DESCRIPTIONS.get(split, "")

    try:
        svc = _cal_service()
        svc.events().insert(calendarId=gym_cal_id, body={
            "summary": f"🏋️ Gym — {split} Day",
            "description": desc,
            "start": {"dateTime": slot_start.isoformat(), "timeZone": "Asia/Singapore"},
            "end":   {"dateTime": slot_end.isoformat(),   "timeZone": "Asia/Singapore"},
        }).execute()

        next_s = advance_split()
        time_str = (
            f"{slot_start.strftime('%-I:%M %p')} – {slot_end.strftime('%-I:%M %p')}"
        )
        log.info(f"[GYM_ENGINE] [SCHEDULED] [{split}] [{target_date}]")
        return True, (
            f"✅ {split} Day scheduled on {target_date.strftime('%a %-d %b')} "
            f"at {time_str}. Next in rotation: {next_s} Day."
        )

    except Exception as exc:
        log.error(f"[GYM_ENGINE] [INSERT_FAIL] [{type(exc).__name__}] {exc}")
        return False, "Failed to create the calendar event. Check Railway logs for details."


def delete_gym_event(target_date: date) -> bool:
    """Delete any gym event on target_date. Returns True if at least one event was deleted."""
    gym_cal_id = find_gym_calendar_id()
    if gym_cal_id is None:
        return False
    try:
        svc = _cal_service()
        day_start = datetime.combine(target_date, time(0, 0)).replace(tzinfo=TZ)
        day_end = day_start + timedelta(days=1)
        events = svc.events().list(
            calendarId=gym_cal_id,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
        ).execute().get("items", [])

        deleted = False
        for ev in events:
            if "🏋️ Gym" in ev.get("summary", ""):
                svc.events().delete(calendarId=gym_cal_id, eventId=ev["id"]).execute()
                deleted = True
                log.info(f"[GYM_ENGINE] [EVENT_DELETED] [{target_date}]")
        return deleted
    except Exception as exc:
        log.error(f"[GYM_ENGINE] [DELETE_FAIL] [{type(exc).__name__}] {exc}")
        return False


def _extract_split_from_title(title: str) -> Optional[str]:
    for split in ("Push", "Pull", "Legs", "Rest"):
        if split in title:
            return split
    return None


def _split_idx(split_name: str) -> int:
    """Return the first index of split_name in SPLIT_ROTATION."""
    try:
        return next(i for i, s in enumerate(SPLIT_ROTATION) if s == split_name)
    except StopIteration:
        return 0


def _direct_create_event(
    svc,
    gym_cal_id: str,
    target_date: date,
    split_name: str,
    forced_time: Optional[time] = None,
) -> str:
    """
    Create a single gym event for target_date without touching global split state.
    If forced_time is given, uses that instead of finding a free slot.
    Returns a human-readable result line.
    """
    if split_name == "Rest":
        return f"Rest day on {target_date.strftime('%a %-d %b')} — no session created."

    if forced_time is not None:
        slot_start = datetime.combine(target_date, forced_time).replace(tzinfo=TZ)
    else:
        slot_start = find_free_slot(target_date)
    if slot_start is None:
        return f"⚠️ No free slot on {target_date.strftime('%a %-d %b')} — skipped."

    slot_end = slot_start + timedelta(hours=2)
    try:
        svc.events().insert(calendarId=gym_cal_id, body={
            "summary": f"🏋️ Gym — {split_name} Day",
            "description": _DESCRIPTIONS.get(split_name, ""),
            "start": {"dateTime": slot_start.isoformat(), "timeZone": "Asia/Singapore"},
            "end":   {"dateTime": slot_end.isoformat(),   "timeZone": "Asia/Singapore"},
        }).execute()
        time_str = f"{slot_start.strftime('%-I:%M %p')} – {slot_end.strftime('%-I:%M %p')}"
        log.info(f"[GYM_ENGINE] [CASCADE_CREATED] [{split_name}] [{target_date}]")
        return f"↩️ {split_name} Day on {target_date.strftime('%a %-d %b')} at {time_str}."
    except Exception as exc:
        log.error(f"[GYM_ENGINE] [CASCADE_INSERT_FAIL] [{type(exc).__name__}] {exc}")
        return f"Failed to create event on {target_date.strftime('%a %-d %b')}."


def cascade_skip(skipped_date: date) -> tuple[bool, list[str]]:
    """
    Delete the gym event on skipped_date and reassign splits to all remaining
    gym events this week after it. Reads splits from event titles — does NOT
    touch the global split state.
    Returns (deleted_today, cascade_result_lines).
    """
    try:
        gym_cal_id = find_gym_calendar_id()
        if gym_cal_id is None:
            return False, []

        svc = _cal_service()
        monday = skipped_date - timedelta(days=skipped_date.weekday())
        sunday = monday + timedelta(days=6)

        # ── Step 1: find and delete today's event, record its split ──────────
        today_start = datetime.combine(skipped_date, time(0, 0)).replace(tzinfo=TZ)
        today_end = today_start + timedelta(days=1)
        today_events = svc.events().list(
            calendarId=gym_cal_id,
            timeMin=today_start.isoformat(),
            timeMax=today_end.isoformat(),
            singleEvents=True,
        ).execute().get("items", [])

        today_split: Optional[str] = None
        deleted_today = False
        for ev in today_events:
            if "🏋️ Gym" not in ev.get("summary", ""):
                continue
            today_split = _extract_split_from_title(ev.get("summary", ""))
            svc.events().delete(calendarId=gym_cal_id, eventId=ev["id"]).execute()
            deleted_today = True
            log.info(f"[GYM_ENGINE] [SKIP_DELETED] [{skipped_date}]")

        if today_split is None:
            return deleted_today, []

        # ── Step 2: find remaining gym events this week, delete them ─────────
        future_start = datetime.combine(skipped_date + timedelta(days=1), time(0, 0)).replace(tzinfo=TZ)
        week_end = datetime.combine(sunday, time(23, 59)).replace(tzinfo=TZ)
        if future_start > week_end:
            return deleted_today, []

        future_events = svc.events().list(
            calendarId=gym_cal_id,
            timeMin=future_start.isoformat(),
            timeMax=week_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        future_dates: list[date] = []
        for ev in future_events:
            if "🏋️ Gym" not in ev.get("summary", ""):
                continue
            start_raw = ev["start"].get("dateTime", "")
            if not start_raw:
                continue
            start_dt = datetime.fromisoformat(start_raw).astimezone(TZ)
            future_dates.append(start_dt.date())
            svc.events().delete(calendarId=gym_cal_id, eventId=ev["id"]).execute()

        if not future_dates:
            return deleted_today, []

        # ── Step 3: recreate — today's split carries over to the next session ──
        start_idx = _split_idx(today_split)
        results = []
        for offset, d in enumerate(future_dates):
            split = SPLIT_ROTATION[(start_idx + offset) % len(SPLIT_ROTATION)]
            results.append(_direct_create_event(svc, gym_cal_id, d, split))

        return deleted_today, results

    except Exception as exc:
        log.error(f"[GYM_ENGINE] [CASCADE_SKIP_FAIL] [{type(exc).__name__}] {exc}")
        return False, []


def cascade_reschedule(new_date: date, forced_time: Optional[time] = None) -> list[str]:
    """
    Schedule a session on new_date and cascade all existing gym events from
    new_date onwards so their splits stay in the correct sequence.
    - If existing sessions exist on/after new_date, new_date takes the same
      split as the earliest of those (they shift forward by one).
    - If new_date is after all existing sessions, uses global split state.
    Updates global split state when done.
    Returns list of result lines for every date affected.
    """
    try:
        gym_cal_id = find_gym_calendar_id()
        if gym_cal_id is None:
            return ["Could not find Gym calendar."]

        svc = _cal_service()
        monday = new_date - timedelta(days=new_date.weekday())
        sunday = monday + timedelta(days=6)

        search_start = datetime.combine(new_date, time(0, 0)).replace(tzinfo=TZ)
        week_end = datetime.combine(sunday, time(23, 59)).replace(tzinfo=TZ)

        existing_events = svc.events().list(
            calendarId=gym_cal_id,
            timeMin=search_start.isoformat(),
            timeMax=week_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        # Collect existing gym events (date, split, event_id)
        existing: list[tuple[date, Optional[str], str]] = []
        for ev in existing_events:
            if "🏋️ Gym" not in ev.get("summary", ""):
                continue
            start_raw = ev["start"].get("dateTime", "")
            if not start_raw:
                continue
            start_dt = datetime.fromisoformat(start_raw).astimezone(TZ)
            existing.append((
                start_dt.date(),
                _extract_split_from_title(ev.get("summary", "")),
                ev["id"],
            ))

        # Determine the starting split for new_date
        if existing:
            first_split = existing[0][1] or current_split_name()
        else:
            first_split = current_split_name()

        # Delete all existing events from new_date onwards
        for _, _, ev_id in existing:
            svc.events().delete(calendarId=gym_cal_id, eventId=ev_id).execute()

        # Build sorted date list: new_date + existing dates (excluding new_date if present)
        existing_dates = [d for d, _, _ in existing if d != new_date]
        all_dates = sorted([new_date] + existing_dates)

        # Create events with cascaded splits
        start_idx = _split_idx(first_split)
        results = []
        for offset, d in enumerate(all_dates):
            split = SPLIT_ROTATION[(start_idx + offset) % len(SPLIT_ROTATION)]
            ft = forced_time if (d == new_date and forced_time is not None) else None
            results.append(_direct_create_event(svc, gym_cal_id, d, split, forced_time=ft))

        # Update global split state so next /days call starts at the right position
        save_split_state((start_idx + len(all_dates)) % len(SPLIT_ROTATION))

        return results

    except Exception as exc:
        log.error(f"[GYM_ENGINE] [CASCADE_RESCHEDULE_FAIL] [{type(exc).__name__}] {exc}")
        return [f"Something went wrong during cascade. Check Railway logs."]


def setsplit_and_cascade(target_date: date, new_split_name: str) -> list[str]:
    """
    Change the split for the gym event on target_date to new_split_name,
    then cascade all subsequent gym events this week so their splits stay in sequence.
    Returns list of result messages.
    """
    if new_split_name not in SPLIT_ROTATION:
        return [f"Invalid split '{new_split_name}'. Use: Push, Pull, Legs, or Rest."]
    try:
        gym_cal_id = find_gym_calendar_id()
        if gym_cal_id is None:
            return ["Could not find Gym calendar."]
        svc = _cal_service()
        monday = target_date - timedelta(days=target_date.weekday())
        sunday = monday + timedelta(days=6)

        # Delete target date's existing event
        day_start = datetime.combine(target_date, time(0, 0)).replace(tzinfo=TZ)
        day_end = day_start + timedelta(days=1)
        for ev in svc.events().list(
            calendarId=gym_cal_id,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
        ).execute().get("items", []):
            if "🏋️ Gym" in ev.get("summary", ""):
                svc.events().delete(calendarId=gym_cal_id, eventId=ev["id"]).execute()

        # Create event with the new split on target_date
        today_result = _direct_create_event(svc, gym_cal_id, target_date, new_split_name)

        # Cascade remaining gym events this week after target_date
        future_start = datetime.combine(target_date + timedelta(days=1), time(0, 0)).replace(tzinfo=TZ)
        week_end = datetime.combine(sunday, time(23, 59)).replace(tzinfo=TZ)
        if future_start > week_end:
            save_split_state((_split_idx(new_split_name) + 1) % len(SPLIT_ROTATION))
            return [today_result]

        future_events = svc.events().list(
            calendarId=gym_cal_id,
            timeMin=future_start.isoformat(),
            timeMax=week_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        future_dates: list[date] = []
        for ev in future_events:
            if "🏋️ Gym" not in ev.get("summary", ""):
                continue
            start_raw = ev["start"].get("dateTime", "")
            if not start_raw:
                continue
            future_dates.append(datetime.fromisoformat(start_raw).astimezone(TZ).date())
            svc.events().delete(calendarId=gym_cal_id, eventId=ev["id"]).execute()

        start_idx = _split_idx(new_split_name)
        results = [today_result]
        for offset, d in enumerate(future_dates):
            split = SPLIT_ROTATION[(start_idx + 1 + offset) % len(SPLIT_ROTATION)]
            results.append(_direct_create_event(svc, gym_cal_id, d, split))

        save_split_state((start_idx + 1 + len(future_dates)) % len(SPLIT_ROTATION))
        log.info(f"[GYM_ENGINE] [SETSPLIT] [{new_split_name}] [{target_date}]")
        return results

    except Exception as exc:
        log.error(f"[GYM_ENGINE] [SETSPLIT_FAIL] [{type(exc).__name__}] {exc}")
        return ["Failed to update split. Check Railway logs."]


def get_week_sessions() -> list[dict]:
    """
    Return gym sessions scheduled for the current Mon–Sun week.
    Each entry: {date, weekday, time_str, split}
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    gym_cal_id = find_gym_calendar_id()
    if gym_cal_id is None:
        return []
    try:
        svc = _cal_service()
        week_start = datetime.combine(monday, time(0, 0)).replace(tzinfo=TZ)
        week_end = datetime.combine(sunday, time(23, 59)).replace(tzinfo=TZ)
        events = svc.events().list(
            calendarId=gym_cal_id,
            timeMin=week_start.isoformat(),
            timeMax=week_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        sessions = []
        for ev in events:
            if "🏋️ Gym" not in ev.get("summary", ""):
                continue
            start_raw = ev["start"].get("dateTime", "")
            if not start_raw:
                continue
            start_dt = datetime.fromisoformat(start_raw).astimezone(TZ)
            end_dt = start_dt + timedelta(hours=2)
            title = ev.get("summary", "")
            split = title.replace("🏋️ Gym — ", "").replace(" Day", "")
            sessions.append({
                "date": start_dt.date(),
                "weekday": start_dt.strftime("%a"),
                "time_str": f"{start_dt.strftime('%-I:%M %p')} – {end_dt.strftime('%-I:%M %p')}",
                "split": split,
            })
        return sessions
    except Exception as exc:
        log.error(f"[GYM_ENGINE] [WEEK_SESSIONS_FAIL] [{type(exc).__name__}] {exc}")
        return []

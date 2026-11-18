"""Runs typed in by hand, with no watch file: the date, miles and time (and, if known, the time of day and average
heart rate). Saved like any other run, with source "manual", and tagged Manual wherever runs are listed. A watch file
for the same run that arrives later (within 90 seconds of the typed-in start) replaces it."""
from __future__ import annotations

import datetime as dt
import uuid

from . import analysis as A
from .config import settings
from .fitfile import ParsedActivity

SOURCE = "manual"
DEVICE = "Entered by hand"
DEFAULT_START = dt.time(15, 30)          # practice time, when the athlete doesn't say
MAX_DAYS_BACK = 60
MAX_MILES = 40
MAX_HOURS = 6


def parse_duration(text: str) -> float:
    """'45' (minutes) -> 2700 ; '45:30' -> 2730 ; '1:05:00' -> 3900."""
    s = str(text or "").strip()
    if not s:
        raise ValueError("Enter how long the run took, such as 42:30.")
    try:
        secs = float(s) * 60 if ":" not in s else A.parse_time(s)
    except ValueError:
        raise ValueError(f"Time “{s}” should look like 42:30 (minutes:seconds) or 1:05:00.") from None
    if not 60 <= secs <= MAX_HOURS * 3600:
        raise ValueError("The time should be between 1 minute and 6 hours.")
    return secs


def build(day: str, start: str, miles: str, duration: str, avg_hr: str = "", today: dt.date | None = None) -> ParsedActivity:
    """The run from the form's fields; ValueError with a message for the athlete when something doesn't fit."""
    today = today or A.team_today()
    try:
        date = dt.date.fromisoformat(str(day or "").strip())
    except ValueError:
        raise ValueError("Choose the day of the run.") from None
    if date > today:
        raise ValueError("That day hasn’t happened yet.")
    if date < today - dt.timedelta(days=MAX_DAYS_BACK):
        raise ValueError(f"Runs can be added for the past {MAX_DAYS_BACK} days. Ask your coach about older ones.")
    try:
        when = dt.time.fromisoformat(str(start).strip()) if str(start or "").strip() else DEFAULT_START
    except ValueError:
        raise ValueError("The start time should look like 3:30 PM.") from None
    try:
        dist = float(str(miles or "").strip())
    except ValueError:
        raise ValueError("Enter the miles as a number, such as 5 or 6.2.") from None
    if not 0.1 <= dist <= MAX_MILES:
        raise ValueError(f"Miles should be between 0.1 and {MAX_MILES}.")
    secs = parse_duration(duration)
    pace = secs / dist
    if pace < 180:
        raise ValueError("That’s faster than 3 minutes a mile. Check the miles and the time.")
    if pace > 30 * 60:
        raise ValueError("That’s slower than 30 minutes a mile. Check the miles and the time.")
    hr = None
    if str(avg_hr or "").strip():
        try:
            hr = float(str(avg_hr).strip())
        except ValueError:
            raise ValueError("Average heart rate should be a number, or left empty.") from None
        if not 60 <= hr <= 230:
            raise ValueError("Average heart rate should be between 60 and 230, or left empty.")
    begin = dt.datetime.combine(date, when, tzinfo=settings().tz).astimezone(dt.timezone.utc)
    return ParsedActivity(external_id=f"manual:{uuid.uuid4().hex}", start=begin, duration_s=secs,
                          distance_m=dist * A.METERS_PER_MILE, sport="running", avg_hr=hr, device=DEVICE)

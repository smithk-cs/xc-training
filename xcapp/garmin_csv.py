"""Garmin Connect's activity list export (Activities → Export CSV): one summary line per activity. It has no laps or
heart-rate samples, so runs from it count toward mileage and charts but have no reps; a later .fit file or Garmin sync of
the same run replaces it with the full detail."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import statistics

from .config import settings
from .fitfile import ParsedActivity

MILE, KM = 1609.344, 1000.0
HEADERS = {"Activity Type", "Date", "Distance", "Time"}


class CsvError(ValueError):
    pass


def is_activities_csv(name: str, data: bytes) -> bool:
    return name.lower().endswith(".csv") and all(h.encode() in data[:2000] for h in HEADERS)


def _seconds(text: str) -> float | None:
    """'00:38:17', '38:17' or '1:02:03.4' -> seconds."""
    parts = (text or "").strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if not 2 <= len(nums) <= 3:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total or None


def _number(text: str) -> float | None:
    t = (text or "").replace(",", "").strip().lstrip("'")
    try:
        return float(t)
    except ValueError:
        return None


def parse(data: bytes) -> tuple[list[ParsedActivity], int, str]:
    """Returns (runs, other activities skipped, the distance unit used: 'miles' or 'km')."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or not HEADERS <= set(rows[0]):
        raise CsvError("That CSV isn’t a Garmin Connect activity list (it needs Activity Type, Date, Distance and Time).")
    tz = settings().tz
    raw, skipped = [], 0
    for r in rows:
        kind = (r.get("Activity Type") or "").strip()
        sport = re.sub(r"\s+", "_", kind.lower())
        if "running" not in sport:
            skipped += 1
            continue
        dist, secs = _number(r.get("Distance", "")), _seconds(r.get("Time", ""))
        try:
            start = dt.datetime.strptime((r.get("Date") or "").strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            skipped += 1
            continue
        if not dist or not secs or dist > 100:  # over 100 miles or km is a misread number
            skipped += 1
            continue
        raw.append((start, sport, dist, secs, _number(r.get("Avg HR", "")), _number(r.get("Max HR", "")), (r.get("Title") or "").strip()))
    # Garmin writes distances in the account's units. High school runners' paces fall near 6-10 min per mile, so a
    # typical pace under 6:15 per unit means kilometers.
    paces = [secs / dist for _, _, dist, secs, *_ in raw]
    unit = KM if paces and statistics.median(paces) < 375 else MILE
    runs = [ParsedActivity(external_id=f"gccsv:{start.astimezone(dt.timezone.utc):%Y%m%dT%H%M%S}", start=start.astimezone(dt.timezone.utc),
                           duration_s=secs, distance_m=dist * unit, sport=sport if sport != "running" else "running",
                           avg_hr=hr, max_hr=mx, device="Garmin Connect (summary)")
            for start, sport, dist, secs, hr, mx, _title in raw]
    return runs, skipped, "km" if unit == KM else "miles"

"""A check that synced runs are on the right day and counted once.

- Duplicates: two runs of one athlete that overlap in time (the same run sent twice, e.g. a Garmin summary and a
  CSV import with different IDs and start times a few minutes apart), or that match in distance and duration but
  start a whole number of hours apart (a copy whose time zone was read wrongly). The better copy is kept: the
  richer source (SOURCE_RANK), then the one with laps, then the athlete's effort note, then the older one.
- Clear duplicates are removed automatically (auto_remove: after each Garmin sync and nightly): both copies from
  Garmin, overlapping in time, with matching distance and duration. Anything less certain waits for a coach.
- Wrong day: a run whose stored day isn't the day it started in the team's time zone.
- Odd runs: dated in the future, faster than 4:00 a mile, over 50 km or 5 hours, or started between midnight and
  4 a.m. (often a time-zone slip). These are only shown; the coach decides.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import analysis as A
from .ingest import SOURCE_RANK, rematch_day
from .models import Activity

MILE = 1609.344
OVERLAP_SHARE = 0.5          # overlapping for half the shorter run
SAME_SHARE = 0.02            # distance and duration within 2%
MAX_SHIFT_H = 14
FASTEST_S_PER_MILE = 240     # 4:00 a mile
LONGEST_M, LONGEST_S = 50_000, 5 * 3600
GARMIN_SOURCES = ("garmin_summary", "garmin_details", "garmin_file")
FEEDBACK = ("rpe", "feel", "athlete_note", "feedback_at", "run_type")


def _end(a: Activity) -> dt.datetime:
    return a.start_utc + dt.timedelta(seconds=a.duration_s or 0)


def _close(x: float, y: float, floor: float) -> bool:
    return abs(x - y) <= max(floor, SAME_SHARE * max(x, y))


def same_run(a: Activity, b: Activity) -> str:
    """Why a and b look like one run sent twice ('' if they don't)."""
    shorter = min(a.duration_s or 0, b.duration_s or 0)
    overlap = (min(_end(a), _end(b)) - max(a.start_utc, b.start_utc)).total_seconds()
    if shorter > 0 and overlap >= OVERLAP_SHARE * shorter:
        return "overlapping times"
    if a.start_utc == b.start_utc:
        return "the same start time"
    if (a.distance_m or 0) > 0 and _close(a.distance_m, b.distance_m or 0, 50) and _close(a.duration_s or 0, b.duration_s or 0, 30):
        hours = abs((a.start_utc - b.start_utc).total_seconds()) / 3600
        if 1 <= round(hours) <= MAX_SHIFT_H and abs(hours - round(hours)) <= 2 / 60:
            return f"the same distance and time, {round(hours)} h apart (a time-zone slip)"
    return ""


def _keep_rank(a: Activity) -> tuple:
    return (SOURCE_RANK.get(a.source, 0), len(a.laps) > 0, a.rpe is not None, -a.id)


def _odd(a: Activity, today: dt.date) -> list[str]:
    out = []
    if a.local_date > today:
        out.append("dated in the future")
    running = "run" in (a.sport or "running")
    if running and (a.distance_m or 0) >= MILE and (a.duration_s or 0) / (a.distance_m / MILE) < FASTEST_S_PER_MILE:
        out.append(f"faster than 4:00 a mile ({A.fmt_secs((a.duration_s or 0) / (a.distance_m / MILE))}/mi): a bike ride or a car?")
    if running and ((a.distance_m or 0) > LONGEST_M or (a.duration_s or 0) > LONGEST_S):
        out.append("over 50 km or 5 hours")
    hour = A.local_time(a.start_utc).hour
    if hour < 4:
        out.append(f"started at {A.local_time(a.start_utc):%-I:%M %p}: a time-zone slip?")
    return out


def find_issues(db: Session, start: dt.date, end: dt.date, today: dt.date | None = None) -> dict:
    """Problems with the runs dated start..end (runs a day either side are looked at too, for slips across midnight)."""
    today = today or A.team_today()
    acts = db.scalars(select(Activity).options(selectinload(Activity.athlete), selectinload(Activity.laps))
                      .where(Activity.local_date >= start - dt.timedelta(days=1), Activity.local_date <= end + dt.timedelta(days=1))
                      .order_by(Activity.athlete_id, Activity.start_utc, Activity.id)).all()
    by_athlete: dict[int, list[Activity]] = defaultdict(list)
    for a in acts:
        by_athlete[a.athlete_id].append(a)

    def in_range(a: Activity) -> bool:
        return start <= a.local_date <= end

    dups, dropped = [], set()
    for runs in by_athlete.values():
        for i, a in enumerate(runs):
            for b in runs[i + 1:]:
                if (b.start_utc - a.start_utc).total_seconds() > (MAX_SHIFT_H + 1) * 3600:
                    break
                if a.id in dropped or b.id in dropped or not (in_range(a) or in_range(b)):
                    continue
                why = same_run(a, b)
                if why:
                    keep, drop = (a, b) if _keep_rank(a) >= _keep_rank(b) else (b, a)
                    dropped.add(drop.id)
                    dups.append({"keep": keep, "drop": drop, "why": why})
    wrong_day = [{"act": a, "should_be": A.local_date(a.start_utc)} for a in acts
                 if in_range(a) and a.local_date != A.local_date(a.start_utc)]
    odd = [{"act": a, "why": w} for a in acts if in_range(a) and a.id not in dropped for w in [_odd(a, today)] if w]
    return {"duplicates": dups, "wrong_day": wrong_day, "odd": odd, "checked": sum(1 for a in acts if in_range(a)),
            "start": start, "end": end}


def fix_days(db: Session, ids: list[int] | None = None) -> int:
    """Move runs to the day they started in the team's time zone, and re-match both days' team workouts."""
    q = select(Activity)
    if ids is not None:
        q = q.where(Activity.id.in_(ids))
    days, n = set(), 0
    for a in db.scalars(q).all():
        right = A.local_date(a.start_utc)
        if a.local_date != right:
            days |= {a.local_date, right}
            if not a.workout_locked:
                a.planned_workout_id = None
            a.local_date = right
            n += 1
    db.flush()
    for d in days:
        rematch_day(db, d)
    return n


def remove_duplicate(db: Session, drop: Activity, keep: Activity) -> None:
    """Delete the lesser copy, first moving to the kept copy what the athlete or coach added to it."""
    if keep.rpe is None and drop.rpe is not None:
        for f in FEEDBACK:
            setattr(keep, f, getattr(drop, f))
    elif not keep.run_type and drop.run_type:
        keep.run_type = drop.run_type
    if drop.workout_locked and not keep.workout_locked:
        keep.planned_workout_id, keep.workout_locked = drop.planned_workout_id, True
    if keep.vo2_max is None:
        keep.vo2_max = drop.vo2_max
    day = drop.local_date
    db.delete(drop)
    db.flush()
    rematch_day(db, day)
    if keep.local_date != day:
        rematch_day(db, keep.local_date)


def certain(pair: dict) -> bool:
    """A duplicate safe to remove without asking: both copies came from Garmin, they overlap in time, and the distance
    and duration match (within 2%, or 50 m and 30 s)."""
    k, d = pair["keep"], pair["drop"]
    return (k.source in GARMIN_SOURCES and d.source in GARMIN_SOURCES
            and pair["why"] in ("overlapping times", "the same start time")
            and _close(k.distance_m or 0, d.distance_m or 0, 50) and _close(k.duration_s or 0, d.duration_s or 0, 30))


def auto_remove(db: Session, start: dt.date, end: dt.date) -> list[str]:
    """Remove the certain duplicates among runs dated start..end; returns a line for each, for the sync log."""
    out = []
    for p in find_issues(db, start, end)["duplicates"]:
        if certain(p):
            k, d = p["keep"], p["drop"]
            out.append(f"{k.athlete.name} {d.local_date:%b %-d} ({d.source}, kept {k.source})")
            remove_duplicate(db, d, k)
    return out

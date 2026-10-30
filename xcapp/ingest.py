"""Store a parsed activity: de-duplicate, analyze laps, and match it to the coach's planned workout."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analysis import local_date
from .fitfile import ParsedActivity
from .models import Activity, Athlete, Lap, PlannedWorkout, SyncLog
from .reps import analyze

# Higher number wins when two copies of the same run arrive (e.g. a summary first, the file later).
SOURCE_RANK = {"garmin_csv": 0, "garmin_summary": 1, "garmin_details": 2, "upload": 3, "garmin_file": 3, "demo": 3}
DUPLICATE_WINDOW_S = 90


def log(db: Session, kind: str, message: str, level: str = "info") -> None:
    db.add(SyncLog(kind=kind, message=message[:2000], level=level))


def _naive_utc(t: dt.datetime) -> dt.datetime:
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def find_duplicate(db: Session, athlete_id: int, external_id: str, start: dt.datetime) -> Activity | None:
    same = db.scalar(select(Activity).where(Activity.athlete_id == athlete_id, Activity.external_id == external_id))
    if same:
        return same
    lo = start - dt.timedelta(seconds=DUPLICATE_WINDOW_S)
    hi = start + dt.timedelta(seconds=DUPLICATE_WINDOW_S)
    return db.scalar(select(Activity).where(Activity.athlete_id == athlete_id, Activity.start_utc >= lo, Activity.start_utc <= hi))


def athlete_groups(db: Session, athlete: Athlete) -> tuple[str, str]:
    """The athlete's roster group (Varsity, JV, ...) and ability group (Bronze, Silver, ...)."""
    from .analysis import abilities, team_today
    return athlete.group, abilities(db, team_today()).get(athlete.id, {}).get("group", "")


def _workout_rank(reps: int, distance_m: float | None) -> tuple[int, float]:
    """Which of an athlete's runs that day is the team workout: the most reps, then the longest."""
    return reps, distance_m or 0


def match_planned(db: Session, athlete: Athlete, day: dt.date) -> PlannedWorkout | None:
    groups = athlete_groups(db, athlete)
    for pw in db.scalars(select(PlannedWorkout).where(PlannedWorkout.date == day).order_by(PlannedWorkout.id)).all():
        if pw.applies_to(*groups, gender=athlete.gender):
            return pw
    return None


def ingest(db: Session, athlete: Athlete, parsed: ParsedActivity, source: str) -> tuple[Activity, str]:
    """Returns (activity, outcome) where outcome is 'created', 'upgraded' or 'duplicate'."""
    start = _naive_utc(parsed.start)
    existing = find_duplicate(db, athlete.id, parsed.external_id, start)
    if existing is not None:
        if SOURCE_RANK.get(source, 0) <= SOURCE_RANK.get(existing.source, 0):
            return existing, "duplicate"
        act, outcome = existing, "upgraded"
        act.laps.clear()
        db.flush()
    else:
        act, outcome = Activity(athlete_id=athlete.id), "created"
        db.add(act)

    act.source = source
    act.external_id = parsed.external_id
    act.start_utc = start
    act.local_date = local_date(parsed.start)
    act.duration_s = parsed.duration_s
    act.distance_m = parsed.distance_m
    act.avg_hr = parsed.avg_hr
    act.max_hr = parsed.max_hr
    act.sport = parsed.sport
    act.device = parsed.device
    if parsed.vo2_max is not None:  # an upgrade from a source without it keeps the earlier value
        act.vo2_max = parsed.vo2_max

    analysis = analyze(parsed.laps, parsed.hr_samples, parsed.start) if parsed.laps else []
    has_reps = any(a.is_rep for a in analysis)
    for i, (lap, res) in enumerate(zip(parsed.laps, analysis)):
        act.laps.append(Lap(
            idx=i, start_offset_s=(lap.start - parsed.start).total_seconds(), elapsed_s=lap.elapsed_s,
            timer_s=lap.timer_s, distance_m=lap.distance_m, avg_hr=lap.avg_hr, max_hr=lap.max_hr,
            intensity=lap.intensity, trigger=lap.trigger, is_rep=res.is_rep, rep_num=res.rep_num,
            hr_drop_60s=res.hr_drop_60s,
        ))

    _save_zone_seconds(db, athlete, act, parsed)
    if act.workout_locked:
        db.flush()
        return act, outcome  # a coach or athlete chose this run's workout by hand
    act.planned_workout_id = None
    if parsed.is_running:
        # A run on a day with a workout for the athlete's group is that workout by default, reps or not.
        pw = match_planned(db, athlete, act.local_date)
        if pw is not None:
            # Only one activity per athlete counts as that day's workout: the most reps, then the longest.
            other = db.scalar(select(Activity).where(
                Activity.athlete_id == athlete.id, Activity.planned_workout_id == pw.id, Activity.id != (act.id or -1)))
            mine = _workout_rank(sum(1 for a in analysis if a.is_rep), act.distance_m)
            if other is None or (not other.workout_locked  # a run chosen by hand keeps the workout
                                 and mine > _workout_rank(sum(1 for l in other.laps if l.is_rep), other.distance_m)):
                if other is not None:
                    other.planned_workout_id = None
                act.planned_workout_id = pw.id
    db.flush()
    return act, outcome


def _save_zone_seconds(db: Session, athlete: Athlete, act: Activity, parsed: ParsedActivity) -> None:
    """Time in each pace zone, from the per-second distance when the file has it, else from the laps, at the VDOT the
    athlete has on the day of the run. Kept with the run so points don't move when VDOT changes later."""
    import json
    from .analysis import abilities
    from .points import segment_seconds, zone_seconds
    key = ("abilities", act.local_date)
    if key not in db.info:  # one team calculation per day, not one per run
        db.info[key] = abilities(db, act.local_date)
    v = db.info[key].get(athlete.id, {}).get("vdot")
    if len(parsed.dist_samples) > 1:
        zones = zone_seconds(parsed.dist_samples, v)
    else:
        zones = segment_seconds([(l.timer_s, l.distance_m) for l in parsed.laps] or [(parsed.duration_s, parsed.distance_m)], v)
    act.zone_seconds, act.zone_vdot = json.dumps({z: round(s) for z, s in zones.items()}), v


def rematch_day(db: Session, day: dt.date) -> int:
    """Re-link already-synced activities after a coach adds or edits a planned workout for that day."""
    n = 0
    acts = db.scalars(select(Activity).where(Activity.local_date == day)).all()
    best: dict[int, Activity] = {}
    acts = [a for a in acts if not a.workout_locked]
    for act in acts:
        if "run" in (act.sport or "running"):
            cur = best.get(act.athlete_id)
            rank = _workout_rank(sum(1 for l in act.laps if l.is_rep), act.distance_m)
            if cur is None or rank > _workout_rank(sum(1 for l in cur.laps if l.is_rep), cur.distance_m):
                best[act.athlete_id] = act
    for act in acts:
        pw = match_planned(db, act.athlete, day) if best.get(act.athlete_id) is act else None
        new_id = pw.id if pw else None
        if act.planned_workout_id != new_id:
            act.planned_workout_id = new_id
            n += 1
    db.flush()
    return n

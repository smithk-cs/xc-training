"""Training points (after Daniels): every minute run earns points by pace zone, harder minutes earn more.

- A run's moving seconds are sorted into zones by smoothed pace against the athlete's VDOT pace table (cutoffs halfway
  between neighbouring zones). Walking pace and pauses earn nothing. This is worked out once, when the run arrives,
  with the VDOT the athlete had that day, so old scores don't move when VDOT changes.
- The weekly target comes from the plan: each team workout's parts (distance turned into minutes at the athlete's zone
  pace) times the zone rate, plus the rest of the group's weekly miles goal run at easy pace.
- On a workout day the points earned are capped at that day's planned points, so running an easy day fast earns nothing
  extra. Other days count every running minute as easy. The week is capped at 110% of the target.
- Weekly score: 80% points (earned / target, at most 1) and 20% days (days with at least the minimum minutes / target
  days, at most 1).
"""
from __future__ import annotations

import bisect
import datetime as dt
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .models import Activity, Athlete, MissedDay, PainReport, PlannedWorkout, TrainingGroup

RATES = {"Easy": 0.2, "Moderate": 0.4, "Threshold": 0.6, "Interval": 1.0, "Repetition": 1.5}   # points per minute
ZONES = [n for n, _ in A.PACE_ZONES]           # slowest first
WALK_PACE = 15 * 60       # seconds per mile: anything slower is walking and earns nothing
SMOOTH_S = 30             # pace is smoothed over about this many seconds to tame GPS noise
MAX_GAP_S = 10            # a longer gap between samples is a pause, not running
WEEK_CAP = 1.10           # extra running counts up to 110% of the weekly target
POINTS_SHARE = 0.8        # the rest of the score is days run
DEFAULT_TARGET_DAYS, DEFAULT_MIN_MINUTES = 6, 30
JUMP = 1.10               # flag weekly points more than 10% over the week before


# ---------- zones and one run's points ----------

def zone_paces(v: float) -> list[tuple[str, float]]:
    """Each zone's pace (seconds per mile) at VDOT v, slowest first."""
    return [(n, A.pace_at(v, pct)) for n, pct in A.PACE_ZONES]


def zone_for(pace: float, paces: list[tuple[str, float]]) -> str | None:
    """The zone for a pace (seconds per mile), with cutoffs halfway between neighbouring zones; None when walking."""
    if pace > WALK_PACE:
        return None
    for (name, p), (_, faster) in zip(paces, paces[1:]):
        if pace >= (p + faster) / 2:
            return name
    return paces[-1][0]


def zone_seconds(samples: list[tuple[float, float]], v: float | None) -> dict[str, float]:
    """Moving seconds per zone from (seconds since start, distance in meters) samples. Without a VDOT every running
    second is easy. 'walk' holds walking and standing seconds."""
    pts = sorted((t, d) for t, d in samples if t is not None and d is not None)
    times = [t for t, _ in pts]
    paces = zone_paces(v) if v else None
    out: dict[str, float] = {}
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        span = t1 - t0
        if span <= 0 or span > MAX_GAP_S:
            continue  # paused
        mid = (t0 + t1) / 2
        lo = bisect.bisect_left(times, mid - SMOOTH_S / 2)
        hi = min(bisect.bisect_right(times, mid + SMOOTH_S / 2), len(pts)) - 1
        lo = min(lo, hi)
        (ta, da), (tb, db) = pts[lo], pts[hi]
        speed = (db - da) / (tb - ta) if tb > ta else (d1 - d0) / span
        pace = A.METERS_PER_MILE / speed if speed > 0 else float("inf")
        zone = (zone_for(pace, paces) if paces else ("Easy" if pace <= WALK_PACE else None)) or "walk"
        out[zone] = out.get(zone, 0) + span
    return out


def segment_seconds(segments: list[tuple[float, float]], v: float | None) -> dict[str, float]:
    """The same from coarser (seconds, meters) pieces, such as laps or a whole run, when there are no samples."""
    paces = zone_paces(v) if v else None
    out: dict[str, float] = {}
    for secs, meters in segments:
        if not secs or secs <= 0:
            continue
        pace = secs / meters * A.METERS_PER_MILE if meters and meters > 0 else float("inf")
        zone = (zone_for(pace, paces) if paces else ("Easy" if pace <= WALK_PACE else None)) or "walk"
        out[zone] = out.get(zone, 0) + secs
    return out


def activity_zones(act: Activity, v: float | None) -> dict[str, float]:
    """Seconds per zone for a stored run: as worked out when it arrived, else from its laps, else the whole run."""
    if act.zone_seconds:
        try:
            return json.loads(act.zone_seconds)
        except ValueError:
            pass
    laps = [(l.timer_s or l.elapsed_s, l.distance_m) for l in act.laps]
    return segment_seconds(laps or [(act.duration_s, act.distance_m)], v)


def moving_minutes(zones: dict[str, float]) -> float:
    return sum(s for z, s in zones.items() if z != "walk") / 60


def zone_points(zones: dict[str, float]) -> float:
    return sum(RATES.get(z, 0) * s / 60 for z, s in zones.items())


# ---------- the plan ----------

def part_plan(p: dict, v: float) -> tuple[float, float, float]:
    """(minutes, miles, points) for one workout part at VDOT v."""
    zone = p.get("zone")
    pace = A.pace_at(v, A.WORKOUT_ZONES.get(zone, A.EASY_PCT))
    n = (p.get("sets") or 1) * (p.get("reps") or 1)
    if p.get("mins"):
        minutes = p["mins"] * n
        miles = minutes * 60 / pace
    else:
        miles = (p.get("dist") or 0) * n / A.METERS_PER_MILE
        minutes = miles * pace / 60
    return minutes, miles, minutes * RATES.get(zone, RATES["Easy"])


def workout_plan(pw: PlannedWorkout, v: float) -> tuple[float, float]:
    """(miles, points) a team workout asks of an athlete at VDOT v. Older one-rep workouts count one rep."""
    parts = pw.part_list or [{"sets": 1, "reps": 1, "dist": pw.rep_distance_m,
                              "zone": A.OLD_TYPE_ZONE.get(A.pace_type(pw.pace_factor), "Interval")}]
    plans = [part_plan(p, v) for p in parts]
    return sum(m for _, m, _ in plans), sum(pts for _, _, pts in plans)


def week_score(points: float, target: float, run_days: int, target_days: int) -> float | None:
    """0-100: 80% points earned against the target, 20% days run against the target days."""
    if not target or target <= 0:
        return None
    pts = min(points / target, 1.0)
    days = min(run_days / target_days, 1.0) if target_days else 1.0
    return 100 * (POINTS_SHARE * pts + (1 - POINTS_SHARE) * days)


# ---------- a team's week ----------

def settings_for(db: Session) -> tuple[int, int]:
    """(target run days per week, minimum minutes for a day to count)."""
    try:
        days = int(A.get_setting(db, "points_target_days", str(DEFAULT_TARGET_DAYS)))
        mins = int(A.get_setting(db, "points_min_minutes", str(DEFAULT_MIN_MINUTES)))
    except ValueError:
        days, mins = DEFAULT_TARGET_DAYS, DEFAULT_MIN_MINUTES
    return days, mins


def team_week(db: Session, monday: dt.date, gender: str | None = None, athletes: list[Athlete] | None = None) -> list[dict]:
    """Each athlete's week: target, points, miles, score, days run, and per day the runs, points, heart rate, effort,
    pain, missed-day reason and flags."""
    sunday = monday + dt.timedelta(days=6)
    days = [monday + dt.timedelta(days=i) for i in range(7)]
    target_days, min_minutes = settings_for(db)
    ab = A.abilities(db, min(sunday, A.team_today()))
    groups = {g.name: g for g in db.scalars(select(TrainingGroup)).all()}
    if athletes is None:
        q = select(Athlete).where(A.on_team())
        if gender:
            q = q.where(Athlete.gender == gender)
        athletes = db.scalars(q.order_by(Athlete.last_name, Athlete.first_name)).all()
    ids = [a.id for a in athletes]
    prev_monday = monday - dt.timedelta(days=7)
    acts = db.scalars(select(Activity).where(Activity.athlete_id.in_(ids), Activity.local_date >= prev_monday,
                                             Activity.local_date <= sunday)).all()
    plans = db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= prev_monday, PlannedWorkout.date <= sunday)).all()
    pains = db.scalars(select(PainReport).where(PainReport.athlete_id.in_(ids), PainReport.date >= monday, PainReport.date <= sunday)).all()
    missed = db.scalars(select(MissedDay).where(MissedDay.athlete_id.in_(ids), MissedDay.date >= monday, MissedDay.date <= sunday)).all()
    by = lambda rows, key: {k: [r for r in rows if key(r) == k] for k in {key(r) for r in rows}}  # noqa: E731
    acts_by = by(acts, lambda r: (r.athlete_id, r.local_date))
    pain_by = by(pains, lambda r: (r.athlete_id, r.date))
    missed_by = by(missed, lambda r: (r.athlete_id, r.date))
    out = []
    for a in athletes:
        me = ab.get(a.id, {})
        v, group = me.get("vdot"), me.get("group") or ""
        g = groups.get(group)
        mine = lambda d: [w for w in plans if w.date == d and w.applies_to(a.group, group, gender=a.gender)]  # noqa: E731
        rows = [_athlete_week(a, v, g, days, mine, acts_by, pain_by, missed_by, min_minutes, offset)
                for offset in (0, -7)]
        week, last = rows
        week["score"] = week_score(week["points"], week["target"] or 0, week["run_days"], target_days)
        if last["points"] > 0 and week["points"] > JUMP * last["points"]:
            week["flags"].append(f"Points up {100 * (week['points'] / last['points'] - 1):.0f}% on last week")
        out.append({"a": a, "group": g, "group_name": group, "vdot": v, "target_days": target_days, **week,
                    "last_points": last["points"]})
    return out


def _athlete_week(a, v, g, days, mine, acts_by, pain_by, missed_by, min_minutes, offset) -> dict:
    goal_miles = A.athlete_miles_goal(g, a.gender, a.experience)   # adjusted for experience
    workout_miles = workout_pts = 0.0
    cells, total, miles, run_days, flags = [], 0.0, 0.0, 0, []
    for d0 in days:
        d = d0 + dt.timedelta(days=offset)
        planned = mine(d)
        day_plan = None
        if planned and v:
            plans = [workout_plan(w, v) for w in planned]
            workout_miles += sum(m for m, _ in plans)
            day_plan = sum(p for _, p in plans)
            workout_pts += day_plan
        runs = [x for x in acts_by.get((a.id, d), []) if "run" in (x.sport or "running")]
        zones: dict[str, float] = {}
        for r in runs:
            for z, s in activity_zones(r, v).items():
                zones[z] = zones.get(z, 0) + s
        minutes = moving_minutes(zones)
        if planned and day_plan is not None:
            earned = min(zone_points(zones), day_plan)     # the day cap: running faster than planned earns nothing extra
        else:
            earned = minutes * RATES["Easy"]                # unplanned days count as easy minutes
        day_miles = sum(r.distance_m or 0 for r in runs) / A.METERS_PER_MILE
        hr_s = [(r.avg_hr, r.duration_s) for r in runs if r.avg_hr and r.duration_s]
        hr = sum(h * s for h, s in hr_s) / sum(s for _, s in hr_s) if hr_s else None
        day_flags = []
        if runs and not planned and minutes and sum(s for z, s in zones.items() if z not in ("Easy", "walk")) / 60 > minutes / 2:
            day_flags.append("Easy day run faster than easy pace")
        if any("treadmill" in (r.sport or "") or "indoor" in (r.sport or "") for r in runs):
            day_flags.append("Treadmill: watch pace may be off without a footpod")
        total += earned
        miles += day_miles
        run_days += minutes >= min_minutes
        flags += [f"{d.strftime('%a')}: {f}" for f in day_flags]
        pain = max((p.severity for p in pain_by.get((a.id, d), [])), default=None)
        miss = (missed_by.get((a.id, d)) or [None])[0]
        cells.append({"date": d, "runs": runs, "miles": day_miles, "minutes": minutes, "points": earned, "planned": day_plan,
                      "workouts": planned, "hr": hr, "rpe": max((r.rpe for r in runs if r.rpe is not None), default=None),
                      "pain": pain, "missed": miss, "flags": day_flags})
    target = None
    if v and goal_miles:  # without the group's weekly miles goal the target would be the workouts alone
        easy_pace = A.pace_at(v, A.EASY_PCT)
        target = workout_pts + max(goal_miles - workout_miles, 0) * easy_pace / 60 * RATES["Easy"]
    points = min(total, WEEK_CAP * target) if target else total
    return {"days": cells, "points": points, "target": target, "miles": miles, "goal_miles": goal_miles,
            "run_days": run_days, "flags": flags}

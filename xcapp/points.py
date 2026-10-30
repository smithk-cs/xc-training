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
ON_PACE, NEAR_PACE = 90, 80   # % of the week's goal due so far: green from 90, amber from 80, red below


def pace_status(pct: float | None) -> str:
    """'good', 'warn' or 'bad' for a share of what's due so far; '' when there's nothing to compare with."""
    if pct is None:
        return ""
    return "good" if pct >= ON_PACE else "warn" if pct >= NEAR_PACE else "bad"


RUN_DAYS = 6   # a day's share of the weekly goals: the goal over six running days (as on the athlete's miles chart)


def _pct(done: float, due: float) -> float | None:
    return 100 * done / due if due else None


def score_pace(week: dict, today: dt.date, target_days: int) -> None:
    """Whether the athlete is on track for the week's score: the same 80/20 score, with points and days run measured
    against what's due by today (a seventh of each a day). Once the week is over it is the score itself."""
    week["score_pct"], week["why_score"] = None, ""
    days = week["days"]
    if not days or not week.get("target") or today < days[0]["date"]:
        return
    i = min(7, (today - days[0]["date"]).days + 1)
    due_p, due_d = week["target"] * i / 7, target_days * i / 7
    done_p = sum(d["points"] for d in days[:i])
    run = week["run_days"]   # days run so far (later days have no runs yet)
    pts, dys = min(done_p / due_p, 1.0), min(run / due_d, 1.0) if due_d else 1.0
    week["score_pct"] = 100 * (POINTS_SHARE * pts + (1 - POINTS_SHARE) * dys)
    through = "the whole week" if i == 7 else f"by {days[i - 1]['date']:%a}"
    week["why_score"] = (f"On track for {week['score_pct']:.0f}%: points {done_p:.0f} of {due_p:.0f} due {through} ({100 * pts:.0f}%) "
                         f"× 80% + days run {run} of {due_d:.1f} due ({100 * dys:.0f}%) × 20%")


def keep_pace(week: dict, today: dt.date) -> None:
    """Colours, each with the reason behind it ('why_miles', 'why_points'):
    - each day judged on its own: miles against the day's planned workout miles (else the weekly goal over six
      running days), and points against the day's planned points (else the weekly target over six);
    - the week's totals against the share due by today (a seventh of the goals a day), or the whole goals once past."""
    goal_m, target = week["goal_miles"] or 0, week["target"] or 0
    cum_m = cum_p = 0.0
    week["miles_pct"] = week["points_pct"] = None
    week["why_miles"] = week["why_points"] = ""
    for i, d in enumerate(week["days"], 1):
        cum_m += d["miles"]
        cum_p += d["points"]
        d["miles_pct"] = d["points_pct"] = None
        d["why_miles"] = d["why_points"] = ""
        if d["date"] > today:
            continue
        if d["miles"] or d["points"]:
            plan_m = d.get("plan_miles") or 0
            due_m = plan_m or goal_m / RUN_DAYS
            due_p = d["planned"] if d.get("planned") else target / RUN_DAYS
            d["miles_pct"], d["points_pct"] = _pct(d["miles"], due_m), _pct(d["points"], due_p)
            if d["miles_pct"] is not None:
                d["why_miles"] = (f"{d['miles']:.1f} of {due_m:.1f} mi " + ("planned for the workout" if plan_m else
                                  f"(weekly {goal_m:g} ÷ {RUN_DAYS} days)") + f": {d['miles_pct']:.0f}%")
            if d["points_pct"] is not None:
                d["why_points"] = (f"{d['points']:.0f} of {due_p:.0f} pts " + ("planned for the workout" if d.get("planned") else
                                   f"(weekly {target:.0f} ÷ {RUN_DAYS} days)") + f": {d['points_pct']:.0f}%")
        due_wm, due_wp = goal_m * i / 7, target * i / 7
        week["miles_pct"], week["points_pct"] = _pct(cum_m, due_wm), _pct(cum_p, due_wp)
        through = "the whole week" if i == 7 else f"{i} of 7 days, through {d['date']:%a}"
        if week["miles_pct"] is not None:
            week["why_miles"] = f"{cum_m:.1f} of {due_wm:.1f} mi due ({through}, of {goal_m:g}): {week['miles_pct']:.0f}%"
        if week["points_pct"] is not None:
            week["why_points"] = f"{cum_p:.0f} of {due_wp:.0f} pts due ({through}, of {target:.0f}): {week['points_pct']:.0f}%"


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
    today = A.team_today()
    ab = A.abilities(db, min(sunday, today))
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
        keep_pace(week, today)
        score_pace(week, today, target_days)
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
        day_plan = day_plan_miles = None
        if planned and v:
            plans = [workout_plan(w, v) for w in planned]
            day_plan_miles = sum(m for m, _ in plans)
            workout_miles += day_plan_miles
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
        worst = max(pain_by.get((a.id, d), []), key=lambda p: p.severity, default=None)
        pain, pain_id = (worst.severity, worst.id) if worst else (None, None)
        miss = (missed_by.get((a.id, d)) or [None])[0]
        cells.append({"date": d, "runs": runs, "miles": day_miles, "minutes": minutes, "points": earned, "planned": day_plan, "plan_miles": day_plan_miles,
                      "workouts": planned, "hr": hr, "rpe": max((r.rpe for r in runs if r.rpe is not None), default=None),
                      "pain": pain, "pain_id": pain_id, "missed": miss, "flags": day_flags})
    target = None
    if v and goal_miles:  # without the group's weekly miles goal the target would be the workouts alone
        easy_pace = A.pace_at(v, A.EASY_PCT)
        target = workout_pts + max(goal_miles - workout_miles, 0) * easy_pace / 60 * RATES["Easy"]
    points = min(total, WEEK_CAP * target) if target else total
    return {"days": cells, "points": points, "target": target, "miles": miles, "goal_miles": goal_miles,
            "run_days": run_days, "flags": flags}

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
from .models import Activity, Athlete, MissedDay, PainReport, PlannedWorkout, RaceResult, TrainingGroup

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


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _pct(done: float, due: float) -> float | None:
    return 100 * done / due if due else None


def plan_days(week: dict, target_days: int, rest_day: int) -> None:
    """What each day of the week asks for (d['due_m'], d['due_p'], d['kind']): workout days their workout; the
    rest of the target run days split the miles and points the workouts leave (easy days); the usual rest day, and
    any other day beyond the target run days, nothing. Days are filled from Monday, so with 6 run days and Sunday
    as the rest day, the full goals are due by Saturday."""
    days = week["days"]
    why = week.get("why_target") or {}
    easy = why.get("easy") or {}
    n_workouts = sum(1 for d in days if d.get("planned") is not None)
    easy_days = max(target_days - n_workouts, 0)
    others = [d for d in days if d.get("planned") is None]
    # rest days: the usual rest day first, then the last other days, until only the target run days are left
    order = sorted(others, key=lambda d: (d["date"].weekday() != rest_day, -d["date"].weekday()))
    rest = {id(d) for d in order[:max(len(others) - easy_days, 0)]}
    share_m = easy.get("miles", 0) / easy_days if easy_days else 0.0
    share_p = easy.get("points", 0) / easy_days if easy_days else 0.0
    week["easy_share"] = {"miles": share_m, "points": share_p, "days": easy_days}
    for d in days:
        if d.get("planned") is not None:
            d["kind"], d["due_m"], d["due_p"] = "workout", d.get("plan_miles") or 0.0, d["planned"]
        elif id(d) in rest:
            d["kind"], d["due_m"], d["due_p"] = "rest", 0.0, 0.0
        else:
            d["kind"], d["due_m"], d["due_p"] = "easy", share_m, share_p


def score_pace(week: dict, today: dt.date, target_days: int) -> None:
    """Whether the athlete is on track for the week's score: the same 80/20 score, with points and run days measured
    against what the days so far ask for (plan_days). Once the week is over it is the score itself."""
    week["score_pct"], week["why_score"] = None, ""
    days = week["days"]
    if not days or not week.get("target") or today < days[0]["date"]:
        return
    i = min(7, (today - days[0]["date"]).days + 1)
    so_far = days[:i]
    due_p = sum(d.get("due_p", 0) for d in so_far)
    due_d = sum(1 for d in so_far if d.get("kind") in ("workout", "easy"))
    done_p = sum(d["points"] for d in so_far)
    run = week["run_days"]   # days run so far (later days have no runs yet)
    pts = min(done_p / due_p, 1.0) if due_p else 1.0
    dys = min(run / due_d, 1.0) if due_d else 1.0
    week["score_pct"] = 100 * (POINTS_SHARE * pts + (1 - POINTS_SHARE) * dys)
    through = "the whole week" if i == 7 else f"by {days[i - 1]['date']:%a}"
    week["why_score"] = (f"On track for {week['score_pct']:.0f}%: points {done_p:.0f} of {due_p:.0f} due {through} ({100 * pts:.0f}%) "
                         f"× 80% + days run {run} of {due_d} due ({100 * dys:.0f}%) × 20%")


def keep_pace(week: dict, today: dt.date) -> None:
    """Colours, each with the reason behind it ('why_miles', 'why_points'), from what each day asks for (plan_days):
    - each day on its own: a workout day against its workout, an easy day against its share of the easy miles and
      points (a run on a rest day too);
    - the week's totals against what the days through today ask for, or the whole goals once the week is past.
      Miles: the weekly goal spread evenly over the run days (miles per day), adding up day by day to the goal on
      the last run day; the rest day adds nothing."""
    goal_m, target = week["goal_miles"] or 0, week["target"] or 0
    share = week.get("easy_share") or {"miles": 0, "points": 0, "days": 0}
    run_days = [d for d in week["days"] if d.get("kind", "easy") in ("workout", "easy")]
    per_day = goal_m / len(run_days) if goal_m and run_days else 0.0
    week["miles_per_day"], week["miles_due"] = (per_day or None), None
    cum_m = cum_p = due_wm = due_wp = 0.0
    week["miles_pct"] = week["points_pct"] = None
    week["why_miles"] = week["why_points"] = ""
    for i, d in enumerate(week["days"], 1):
        cum_m += d["miles"]
        cum_p += d["points"]
        due_wm += per_day if d.get("kind", "easy") in ("workout", "easy") else 0.0
        due_wp += d.get("due_p", 0)
        d["miles_pct"] = d["points_pct"] = None
        d["why_miles"] = d["why_points"] = ""
        if d["date"] > today:
            continue
        if d["miles"] or d["points"]:
            kind = d.get("kind", "easy")
            due_m = d.get("due_m", 0) if kind != "rest" else share["miles"]
            due_p = d.get("due_p", 0) if kind != "rest" else share["points"]
            how = ("planned for the workout" if kind == "workout" else
                   f"easy day: the miles the workouts leave ÷ {share['days']} easy days" if kind == "easy" else
                   "a rest day, so compared with an easy day")
            d["miles_pct"], d["points_pct"] = _pct(d["miles"], due_m), _pct(d["points"], due_p)
            if d["miles_pct"] is not None:
                d["why_miles"] = f"{d['miles']:.1f} of {due_m:.1f} mi ({how}): {d['miles_pct']:.0f}%"
            if d["points_pct"] is not None:
                d["why_points"] = f"{d['points']:.0f} of {due_p:.0f} pts ({how}): {d['points_pct']:.0f}%"
        week["miles_pct"], week["points_pct"] = _pct(cum_m, due_wm), _pct(cum_p, due_wp)
        week["miles_due"] = due_wm if goal_m else None
        through = "the whole week" if i == 7 else f"through {d['date']:%a}"
        if week["miles_pct"] is not None:
            week["why_miles"] = (f"{cum_m:.1f} of {due_wm:.1f} mi due {through} ({per_day:.1f} mi a day over {len(run_days)} run days, "
                                 f"{goal_m:g} for the week): {week['miles_pct']:.0f}%")
        if week["points_pct"] is not None:
            week["why_points"] = f"{cum_p:.0f} of {due_wp:.0f} pts due {through} (of {target:.0f}): {week['points_pct']:.0f}%"


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


def workout_minutes_miles_points(pw: PlannedWorkout, v: float, daily_miles: float | None = None) -> tuple[float, float, float]:
    """(minutes, miles, points) a team workout asks of an athlete at VDOT v, with any 'finish the day' easy run sized
    to their daily miles. Older one-rep workouts count one rep."""
    parts = A.resolve_fill(pw.part_list, daily_miles) if pw.part_list else [
        {"sets": 1, "reps": 1, "dist": pw.rep_distance_m, "zone": A.OLD_TYPE_ZONE.get(A.pace_type(pw.pace_factor), "Interval")}]
    plans = [part_plan(p, v) for p in parts]
    return sum(mi for mi, _, _ in plans), sum(m for _, m, _ in plans), sum(pts for _, _, pts in plans)


def workout_plan(pw: PlannedWorkout, v: float, daily_miles: float | None = None) -> tuple[float, float]:
    """(miles, points) a team workout asks of an athlete at VDOT v (see workout_minutes_miles_points)."""
    return workout_minutes_miles_points(pw, v, daily_miles)[1:]


def week_score(points: float, target: float, run_days: int, target_days: int) -> float | None:
    """0-100: 80% points earned against the target, 20% days run against the target days."""
    if not target or target <= 0:
        return None
    pts = min(points / target, 1.0)
    days = min(run_days / target_days, 1.0) if target_days else 1.0
    return 100 * (POINTS_SHARE * pts + (1 - POINTS_SHARE) * days)


# ---------- a team's week ----------

def rest_day(db: Session) -> int:
    """The team's usual rest day (Monday = 0 ... Sunday = 6)."""
    try:
        d = int(A.get_setting(db, "points_rest_day", "6"))
        return d if 0 <= d <= 6 else 6
    except ValueError:
        return 6


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
    rest = rest_day(db)
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
    # Race days always count as a day run, however short the race and warm-up
    race_days = set(db.execute(select(RaceResult.athlete_id, RaceResult.date).where(
        RaceResult.athlete_id.in_(ids), RaceResult.date >= prev_monday, RaceResult.date <= sunday)).all())
    race_days |= {(x.athlete_id, x.local_date) for x in acts if x.run_type == "Race"}
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
        rows = [_athlete_week(a, v, g, days, mine, acts_by, pain_by, missed_by, min_minutes, offset, target_days, race_days)
                for offset in (0, -7)]
        week, last = rows
        week["score"] = week_score(week["points"], week["target"] or 0, week["run_days"], target_days)
        plan_days(week, target_days, rest)
        keep_pace(week, today)
        score_pace(week, today, target_days)
        if last["points"] > 0 and week["points"] > JUMP * last["points"]:
            week["flags"].append(f"Points up {100 * (week['points'] / last['points'] - 1):.0f}% on last week")
        no_score = ("" if week["target"] else "No VDOT yet (needs a race)" if not v
                    else "No team workout posted yet this week")
        out.append({"a": a, "group": g, "group_name": group, "vdot": v, "target_days": target_days, **week,
                    "last_points": last["points"], "no_score": no_score, "min_minutes": min_minutes})
    return out


def _athlete_week(a, v, g, days, mine, acts_by, pain_by, missed_by, min_minutes, offset, target_days: int = DEFAULT_TARGET_DAYS,
                  race_days: set | None = None) -> dict:
    from .ingest import pick_workout
    goal_miles = A.athlete_miles_goal(g, a.gender, a.experience)   # adjusted for experience
    workout_miles = workout_pts = 0.0
    breakdown = []   # how the target is built, for "Show why": (day, workout, miles, points, other workouts not counted)
    cells, total, miles, run_days, flags = [], 0.0, 0.0, 0, []
    for d0 in days:
        d = d0 + dt.timedelta(days=offset)
        planned = mine(d)
        day_plan = day_plan_miles = None
        day_plan_minutes = 0.0
        if planned and v:
            # one team workout a day: the one this athlete's run is matched to
            w = pick_workout(planned)
            day_plan_minutes, day_plan_miles, day_plan = workout_minutes_miles_points(w, v, goal_miles / target_days if goal_miles else None)
            workout_miles += day_plan_miles
            workout_pts += day_plan
            breakdown.append({"date": d, "title": w.title, "miles": day_plan_miles, "points": day_plan,
                              "not_counted": [x.title for x in planned if x is not w]})
        runs = [x for x in acts_by.get((a.id, d), []) if "run" in (x.sport or "running")]
        zones: dict[str, float] = {}
        for r in runs:
            for z, s in activity_zones(r, v).items():
                zones[z] = zones.get(z, 0) + s
        minutes = moving_minutes(zones)
        if planned and day_plan is not None:
            # Points come only from the posted workouts: up to the workout's planned points (running it faster, or
            # adding miles, earns nothing extra).
            earned = min(zone_points(zones), day_plan)
        else:
            earned = 0.0                                    # no posted workout that day: miles and days run, no points
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
        race = (a.id, d) in (race_days or set())
        counts = bool(runs) and (minutes >= min_minutes or race)
        run_days += counts
        flags += [f"{d.strftime('%a')}: {f}" for f in day_flags]
        worst = max(pain_by.get((a.id, d), []), key=lambda p: p.severity, default=None)
        pain, pain_id = (worst.severity, worst.id) if worst else (None, None)
        miss = (missed_by.get((a.id, d)) or [None])[0]
        cells.append({"date": d, "runs": runs, "miles": day_miles, "minutes": minutes, "points": earned, "planned": day_plan, "plan_miles": day_plan_miles,
                      "workouts": planned, "hr": hr, "rpe": max((r.rpe for r in runs if r.rpe is not None), default=None),
                      "pain": pain, "pain_id": pain_id, "missed": miss, "flags": day_flags,
                      "counts": counts, "race": race})
    target = None
    easy = None
    if v and goal_miles:  # the goal's miles the workouts leave: run easy, counted in miles (no points)
        easy_pace = A.pace_at(v, A.EASY_PCT)
        easy_miles = max(goal_miles - workout_miles, 0)
        easy = {"miles": easy_miles, "pace": easy_pace, "points": 0.0}
    if v and workout_pts > 0:  # the target is only the week's posted workouts, so it grows as workouts are added
        target = workout_pts
    points = min(total, WEEK_CAP * target) if target else total
    return {"days": cells, "points": points, "target": target, "miles": miles, "goal_miles": goal_miles,
            "run_days": run_days, "flags": flags,
            "why_target": {"workouts": breakdown, "workout_miles": workout_miles, "workout_points": workout_pts, "easy": easy,
                           "earned": total, "cap": WEEK_CAP * target if target else None}}

"""VDOT suggestions from workouts. Daniels' advice: raise VDOT only when training shows the athlete is ready, and stay at
a VDOT for about four weeks unless a race says otherwise. The app compares each Threshold and Interval session's reps
with the target pace at the athlete's VDOT, and heart rate as a share of their max, since their last VDOT change or race:

- Ready to raise (VDOT + 1): at least 4 weeks at this VDOT; the last 3 sessions all on or faster than target pace; and
  either about 1.5% faster on average, or heart rate down by 2+ points of max across them at the same pace. Not while
  the athlete has reported pain in the last 2 weeks.
- Worth a check (VDOT - 1): 2 of the last 3 sessions 3%+ slower than target, or heart rate up by 3+ points of max while
  paces are only held.

A coach applies or dismisses each one on the VDOT page; nothing changes on its own. A dismissed suggestion comes back
only after a newer session.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import analysis as A
from .models import Activity, Athlete, PainReport, PlannedWorkout, VdotChange

MILE = A.METERS_PER_MILE
ZONES = ("Threshold", "Interval")
HOLD_DAYS = 28        # about four weeks at a VDOT before raising it
LOOK_BACK = 56        # sessions considered: the last 8 weeks, after the last VDOT change or race
FAST, ON_PACE, SLOW = 0.985, 1.005, 1.03     # rep pace ÷ target pace
HR_DOWN, HR_UP = 2.0, 3.0                    # points of max heart rate across the sessions
PAIN_DAYS = 14


def _since(db: Session, a: Athlete, races: dict, today: dt.date) -> dt.date | None:
    """When the athlete's VDOT last changed: a coach's change or the race it comes from, whichever is later."""
    days = [db.scalar(select(func.max(VdotChange.date)).where(VdotChange.athlete_id == a.id))]
    r = races.get(a.id)
    if r:
        days.append(r[2])
    days = [d for d in days if d]
    return max(days) if days else None


def session(db: Session, a: Athlete, act: Activity, v: float) -> dict | None:
    """One Threshold or Interval session: rep pace against the target at VDOT v, and heart rate as a share of max."""
    pw = db.get(PlannedWorkout, act.planned_workout_id)
    main = A.main_part(pw.part_list) if pw else None
    if not main or main["zone"] not in ZONES:
        return None
    reps = [lp for lp in act.laps if lp.is_rep and (lp.distance_m or 0) >= 100 and (lp.timer_s or 0) > 0]
    if len(reps) < 2:
        return None
    dist = sum(lp.distance_m for lp in reps)
    pace = sum(lp.timer_s for lp in reps) / dist * MILE
    target = A.pace_at(v, A.WORKOUT_ZONES[main["zone"]])
    from .workout_view import max_hr
    hr_max = max_hr(db, a, act.local_date)[0]
    hrs = [(lp.avg_hr, lp.distance_m) for lp in reps if lp.avg_hr]
    hr_pct = (100 * sum(h * d for h, d in hrs) / sum(d for _, d in hrs) / hr_max) if hrs and hr_max else None
    return {"date": act.local_date, "title": pw.title, "zone": main["zone"], "pace": pace, "target": target,
            "ratio": pace / target, "hr_pct": hr_pct, "reps": len(reps)}


def _judge(sessions: list[dict], held_days: int | None, pain: bool) -> tuple[str, str] | None:
    """('up' or 'down', why) from the last three sessions (oldest first), or None."""
    last = sessions[-3:]
    if len(last) < 2:
        return None
    ratios = [s["ratio"] for s in last]
    hrs = [s["hr_pct"] for s in last if s["hr_pct"] is not None]
    hr_change = hrs[-1] - hrs[0] if len(hrs) >= 2 else None
    slow = sum(1 for r in ratios if r >= SLOW)
    if slow >= 2:
        return "down", f"{slow} of the last {len(last)} sessions were 3% or more slower than target pace"
    if hr_change is not None and hr_change >= HR_UP and all(r >= 0.995 for r in ratios):
        return "down", f"heart rate up {hr_change:.0f} points of max across the last {len(last)} sessions at the same pace"
    if len(last) < 3 or not all(r <= ON_PACE for r in ratios) or pain or held_days is None or held_days < HOLD_DAYS:
        return None
    avg = sum(ratios) / len(ratios)
    if avg <= FAST:
        return "up", f"the last 3 sessions averaged {100 * (1 - avg):.1f}% faster than target pace"
    if hr_change is not None and hr_change <= -HR_DOWN:
        return "up", f"on target pace in the last 3 sessions with heart rate down {-hr_change:.0f} points of max"
    return None


def suggestions(db: Session, today: dt.date, gender: str | None = None) -> list[dict]:
    """Athletes whose recent workouts suggest a VDOT change: [{"a", "vdot", "to", "kind", "why", "sessions", "key"}]."""
    ab = A.abilities(db, today)
    races = A.latest_races(db, today - dt.timedelta(days=365), today)
    q = select(Athlete).where(A.on_team())
    if gender:
        q = q.where(Athlete.gender == gender)
    out = []
    for a in db.scalars(q.order_by(Athlete.last_name, Athlete.first_name)).all():
        v = ab.get(a.id, {}).get("vdot")
        if not v:
            continue
        since = _since(db, a, races, today)
        start = max(since + dt.timedelta(days=1) if since else today - dt.timedelta(days=LOOK_BACK), today - dt.timedelta(days=LOOK_BACK))
        acts = db.scalars(select(Activity).options(selectinload(Activity.laps))
                          .where(Activity.athlete_id == a.id, Activity.planned_workout_id.is_not(None),
                                 Activity.local_date >= start, Activity.local_date <= today)
                          .order_by(Activity.local_date, Activity.id)).all()
        sessions = [s for s in (session(db, a, x, v) for x in acts) if s]
        if not sessions:
            continue
        pain = db.scalar(select(func.count(PainReport.id)).where(
            PainReport.athlete_id == a.id, PainReport.date > today - dt.timedelta(days=PAIN_DAYS))) > 0
        held = (today - since).days if since else None
        verdict = _judge(sessions, held, pain)
        if not verdict:
            continue
        kind, why = verdict
        to = v + 1 if kind == "up" else v - 1
        if not A.VDOT_MIN <= to <= A.VDOT_MAX:
            continue
        key = f"{kind}:{sessions[-1]['date'].isoformat()}"
        if A.get_setting(db, f"vdot_hint_{a.id}", "") == key:
            continue   # dismissed; comes back after a newer session
        out.append({"a": a, "vdot": v, "to": to, "kind": kind, "why": why, "sessions": sessions[-3:], "key": key,
                    "held": held})
    return out


def dismiss(db: Session, athlete_id: int, key: str) -> None:
    A.set_setting(db, f"vdot_hint_{athlete_id}", key[:40])

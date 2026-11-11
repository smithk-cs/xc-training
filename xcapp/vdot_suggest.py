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
from .models import Activity, AppSetting, Athlete, PainReport, PlannedWorkout, VdotChange

MILE = A.METERS_PER_MILE
ZONES = ("Threshold", "Interval")
HOLD_DAYS = 28        # about four weeks at a VDOT before raising it
LOOK_BACK = 56        # sessions considered: the last 8 weeks, after the last VDOT change or race
FAST, ON_PACE, SLOW = 0.985, 1.005, 1.03     # rep pace ÷ target pace
HR_DOWN, HR_UP = 2.0, 3.0                    # points of max heart rate across the sessions
PAIN_DAYS = 14


def session(act: Activity, pw: PlannedWorkout | None, v: float, hr_max: float | None) -> dict | None:
    """One Threshold or Interval session: rep pace against the target at VDOT v, and heart rate as a share of max."""
    main = A.main_part(pw.part_list) if pw else None
    if not main or main["zone"] not in ZONES:
        return None
    reps = [lp for lp in act.laps if lp.is_rep and (lp.distance_m or 0) >= 100 and (lp.timer_s or 0) > 0]
    if len(reps) < 2:
        return None
    dist = sum(lp.distance_m for lp in reps)
    pace = sum(lp.timer_s for lp in reps) / dist * MILE
    target = A.pace_at(v, A.WORKOUT_ZONES[main["zone"]])
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
    """Athletes whose recent workouts suggest a VDOT change: [{"a", "vdot", "to", "kind", "why", "sessions", "key"}].
    Everything is read in a few queries for the whole team (the page would otherwise wait on one query per run)."""
    ab = A.abilities(db, today)
    races = A.latest_races(db, today - dt.timedelta(days=365), today)
    q = select(Athlete).where(A.on_team())
    if gender:
        q = q.where(Athlete.gender == gender)
    athletes = [a for a in db.scalars(q.order_by(Athlete.last_name, Athlete.first_name)).all() if ab.get(a.id, {}).get("vdot")]
    if not athletes:
        return []
    ids = [a.id for a in athletes]
    changed = dict(db.execute(select(VdotChange.athlete_id, func.max(VdotChange.date))
                              .where(VdotChange.athlete_id.in_(ids)).group_by(VdotChange.athlete_id)).all())
    acts_by: dict[int, list[Activity]] = {}
    for x in db.scalars(select(Activity).options(selectinload(Activity.laps))
                        .where(Activity.athlete_id.in_(ids), Activity.planned_workout_id.is_not(None),
                               Activity.local_date >= today - dt.timedelta(days=LOOK_BACK), Activity.local_date <= today)
                        .order_by(Activity.local_date, Activity.id)).all():
        acts_by.setdefault(x.athlete_id, []).append(x)
    pw_ids = {x.planned_workout_id for xs in acts_by.values() for x in xs}
    pws = {w.id: w for w in db.scalars(select(PlannedWorkout).where(PlannedWorkout.id.in_(pw_ids))).all()} if pw_ids else {}
    pain = set(db.scalars(select(PainReport.athlete_id).where(PainReport.athlete_id.in_(ids),
                                                             PainReport.date > today - dt.timedelta(days=PAIN_DAYS))).all())
    seen_max = dict(db.execute(select(Activity.athlete_id, func.max(Activity.max_hr)).where(
        Activity.athlete_id.in_(ids), Activity.local_date > today - dt.timedelta(days=365), Activity.local_date <= today,
        Activity.max_hr <= 230).group_by(Activity.athlete_id)).all())
    dismissed = dict(db.execute(select(AppSetting.key, AppSetting.value).where(AppSetting.key.like("vdot_hint_%"))).all())
    out = []
    for a in athletes:
        v = ab[a.id]["vdot"]
        days = [d for d in (changed.get(a.id), (races.get(a.id) or (None, None, None))[2]) if d]
        since = max(days) if days else None
        start = since + dt.timedelta(days=1) if since else today - dt.timedelta(days=LOOK_BACK)
        hr_max = float(a.hr_max) if a.hr_max else (float(seen_max[a.id]) if seen_max.get(a.id) else None)
        sessions = [s for s in (session(x, pws.get(x.planned_workout_id), v, hr_max)
                                for x in acts_by.get(a.id, []) if x.local_date >= start) if s]
        if not sessions:
            continue
        held = (today - since).days if since else None
        verdict = _judge(sessions, held, a.id in pain)
        if not verdict:
            continue
        kind, why = verdict
        to = v + 1 if kind == "up" else v - 1
        if not A.VDOT_MIN <= to <= A.VDOT_MAX:
            continue
        key = f"{kind}:{sessions[-1]['date'].isoformat()}"
        if dismissed.get(f"vdot_hint_{a.id}") == key:
            continue   # dismissed; comes back after a newer session
        out.append({"a": a, "vdot": v, "to": to, "kind": kind, "why": why, "sessions": sessions[-3:], "key": key,
                    "held": held})
    return out


def dismiss(db: Session, athlete_id: int, key: str) -> None:
    A.set_setting(db, f"vdot_hint_{athlete_id}", key[:40])

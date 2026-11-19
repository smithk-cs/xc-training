"""Return to running after an injury: a walk-run plan the athletic trainer (or a coach) starts when an athlete is
cleared to begin running again. The athlete sees today's step on their page and, after the session, says how much it
hurt (0 to 10):

- 0-2 (and no limping): the next step opens two days later, so there's a day off between steps.
- 3-4: repeat the same step next time (also two days later).
- 5 or more: the plan pauses and goes back a step; a pain report goes to the trainer and coaches, who resume it.

After the last step the athlete is back to normal easy running; the coaches decide when workouts come back. This is a
common walk-run progression; the trainer can start an athlete at any step and move them back or forward.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Athlete, PainReport, ReturnCheck, ReturnPlan

STEPS = [
    ("Brisk walk", "Walk briskly for 30 minutes."),
    ("Walk 4, jog 1", "Walk 4 minutes, then jog 1 minute easy. Do it 6 times (30 minutes)."),
    ("Walk 3, jog 2", "Walk 3 minutes, then jog 2 minutes easy. Do it 6 times (30 minutes)."),
    ("Walk 2, jog 3", "Walk 2 minutes, then jog 3 minutes easy. Do it 6 times (30 minutes)."),
    ("Walk 1, jog 4", "Walk 1 minute, then jog 4 minutes easy. Do it 6 times (30 minutes)."),
    ("Jog 20 minutes", "Jog 20 minutes easy without stopping, on a soft, flat surface if you can."),
    ("Jog 30 minutes", "Jog 30 minutes easy without stopping."),
    ("Easy 40 with strides", "Run 40 minutes easy, then 4 relaxed strides of about 80 m."),
]
GAP_DAYS = 2            # a day off between steps
OK_MAX = 2              # pain this high or lower: go on
REPEAT_MAX = 4          # ... up to this: repeat the step; above it: pause and see the trainer


def active(db: Session, athlete_id: int) -> ReturnPlan | None:
    return db.scalar(select(ReturnPlan).where(ReturnPlan.athlete_id == athlete_id, ReturnPlan.status.in_(("active", "paused")))
                     .order_by(ReturnPlan.id.desc()))


def start(db: Session, a: Athlete, today: dt.date, step: int = 0, injury_id: int | None = None, by: str = "") -> ReturnPlan:
    """Start (or restart) an athlete's plan at a step (0 = the first)."""
    old = active(db, a.id)
    if old is not None:
        old.status = "stopped"
    p = ReturnPlan(athlete_id=a.id, injury_id=injury_id, step=max(0, min(step, len(STEPS) - 1)), status="active",
                   next_on=today, started_on=today, created_by=by[:80])
    db.add(p)
    db.flush()
    return p


def report(db: Session, p: ReturnPlan, pain: int, today: dt.date, note: str = "") -> str:
    """The athlete's report after today's step; returns what happens next (in words for them)."""
    if p.status != "active" or today < p.next_on:
        raise ValueError("This step isn't open today.")
    if not 0 <= pain <= 10:
        raise ValueError("Pain is from 0 to 10.")
    db.add(ReturnCheck(plan_id=p.id, step=p.step, date=today, pain=pain, note=note[:300]))
    title = STEPS[p.step][0]
    if pain <= OK_MAX:
        if p.step >= len(STEPS) - 1:
            p.status = "done"
            p.finished_on = today
            return "That was the last step. You’re back to easy running: your coaches will say when workouts come back."
        p.step += 1
        p.next_on = today + dt.timedelta(days=GAP_DAYS)
        return f"Nice work. Rest tomorrow; next: {STEPS[p.step][0]} on {p.next_on:%a %b %-d}."
    if pain <= REPEAT_MAX:
        p.next_on = today + dt.timedelta(days=GAP_DAYS)
        return f"Some pain, so repeat {title} on {p.next_on:%a %b %-d} instead of moving on. Ice it and tell the trainer if it lingers."
    p.status = "paused"
    p.step = max(0, p.step - 1)
    db.add(PainReport(athlete_id=p.athlete_id, date=today, body_area=_area(db, p), severity=pain,
                      note=f"Return to run, {title}: {note}".strip(": ")[:300]))
    return "Stop here and see the athletic trainer before running again. Your plan is paused; they’ll restart it."


def _area(db: Session, p: ReturnPlan) -> str:
    from .models import Injury
    inj = db.get(Injury, p.injury_id) if p.injury_id else None
    return inj.body_area if inj else "Other"


def checks(db: Session, p: ReturnPlan) -> list[ReturnCheck]:
    return db.scalars(select(ReturnCheck).where(ReturnCheck.plan_id == p.id).order_by(ReturnCheck.date, ReturnCheck.id)).all()


def view(db: Session, p: ReturnPlan, today: dt.date) -> dict:
    """What the athlete's page and the trainer's list show."""
    title, how = STEPS[p.step]
    done = checks(db, p)
    return {"plan": p, "n": p.step + 1, "of": len(STEPS), "title": title, "how": how, "open": p.status == "active" and today >= p.next_on,
            "reported_today": any(c.date == today and c.step == p.step for c in done), "checks": done, "last": done[-1] if done else None,
            "steps": [{"n": i + 1, "title": t, "state": "done" if i < p.step or p.status == "done" else "now" if i == p.step else "later"}
                      for i, (t, _) in enumerate(STEPS)]}

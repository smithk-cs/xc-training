"""The athlete's morning check-in: hours of sleep, soreness and mood, three taps next to resting heart rate. Coaches
see a flag when an athlete's last few days fall well short of their own usual (or of plain limits), and whether their
resting heart rate is up too; together they often show illness or overtraining before it shows in the running.
Like resting heart rate, check-ins are never sent to the AI."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Athlete, Wellness

SLEEP = [(5, "5 or less"), (6, "6"), (7, "7"), (8, "8"), (9, "9"), (10, "10+")]
SORENESS = [(1, "None"), (2, "A little"), (3, "Some"), (4, "Sore"), (5, "Very sore")]
MOOD = [(1, "Awful"), (2, "Low"), (3, "OK"), (4, "Good"), (5, "Great")]
RECENT = 3          # days looked at for a flag
USUAL = 28          # days before those that make the athlete's usual
SHORT_SLEEP = 6.5   # hours: under this on average over the recent days is short, whatever the usual


def save(db: Session, a: Athlete, day: dt.date, sleep, soreness, mood) -> Wellness:
    """Save (or change) one day's check-in; ValueError for a value off the scale."""
    vals = {}
    for name, v, scale in (("sleep", sleep, SLEEP), ("soreness", soreness, SORENESS), ("mood", mood, MOOD)):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("Tap one answer for sleep, soreness and mood.") from None
        if n not in {k for k, _ in scale}:
            raise ValueError("Tap one answer for sleep, soreness and mood.")
        vals[name] = n
    w = db.scalar(select(Wellness).where(Wellness.athlete_id == a.id, Wellness.date == day))
    if w is None:
        w = Wellness(athlete_id=a.id, date=day)
        db.add(w)
    w.sleep_h, w.soreness, w.mood = vals["sleep"], vals["soreness"], vals["mood"]
    db.flush()
    return w


def for_athlete(db: Session, athlete_id: int, today: dt.date, days: int = 14) -> list[Wellness]:
    return db.scalars(select(Wellness).where(Wellness.athlete_id == athlete_id, Wellness.date > today - dt.timedelta(days=days),
                                             Wellness.date <= today).order_by(Wellness.date.desc())).all()


def label(scale, n) -> str:
    return dict(scale).get(n, "")


def reasons(recent: list[Wellness], usual: list[Wellness]) -> list[str]:
    """Why an athlete's last few days stand out, in words (empty when they don't)."""
    out = []
    if len(recent) < 2:
        return out
    sleep = mean(w.sleep_h for w in recent)
    usual_sleep = mean(w.sleep_h for w in usual) if len(usual) >= 5 else None
    if sleep < SHORT_SLEEP or (usual_sleep is not None and sleep <= usual_sleep - 1.5):
        out.append(f"sleeping {sleep:.1f} h" + (f" (usually {usual_sleep:.1f})" if usual_sleep is not None else ""))
    sore = [w.soreness for w in recent]
    if sum(1 for s in sore if s >= 4) >= 2:
        out.append("sore " + str(sum(1 for s in sore if s >= 4)) + f" of the last {len(sore)} days")
    mood = mean(w.mood for w in recent)
    usual_mood = mean(w.mood for w in usual) if len(usual) >= 5 else None
    if sum(1 for w in recent if w.mood <= 2) >= 2 or (usual_mood is not None and mood <= usual_mood - 1.5):
        out.append(f"mood low ({mood:.1f} of 5" + (f", usually {usual_mood:.1f})" if usual_mood is not None else ")"))
    return out


def flags(db: Session, today: dt.date, rhr_up: dict[int, float] | None = None) -> list[dict]:
    """Athletes whose recent check-ins stand out: [{"athlete", "reasons", "rhr_up", "signals"}], most signals first.
    rhr_up: athlete id -> beats above usual (analysis.resting_hr_flags), counted as one more signal."""
    from .analysis import on_team
    start = today - dt.timedelta(days=RECENT + USUAL)
    rows = db.execute(select(Wellness, Athlete).join(Athlete, Athlete.id == Wellness.athlete_id)
                      .where(on_team(), Wellness.date > start, Wellness.date <= today)).all()
    by, people = defaultdict(list), {}
    for w, a in rows:
        by[a.id].append(w)
        people[a.id] = a
    cut = today - dt.timedelta(days=RECENT)
    out = []
    for aid, ws in by.items():
        why = reasons([w for w in ws if w.date > cut], [w for w in ws if w.date <= cut])
        if not why:
            continue
        up = (rhr_up or {}).get(aid)
        out.append({"athlete": people[aid], "reasons": why, "rhr_up": up, "signals": len(why) + (1 if up else 0)})
    return sorted(out, key=lambda f: (-f["signals"], f["athlete"].last_name))

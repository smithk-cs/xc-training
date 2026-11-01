"""Weekly medals on the athlete page, from each finished week's score (the Week page's 80% points, 20% days):

- gold: a green week (90% or more);
- silver: an amber week (80 to 89%);
- miles: no medal for the score, but the weekly miles goal run in full.

Each finished week is saved (WeekResult) so a visit doesn't work out the whole season; weeks that ended in the last
two weeks are worked out again once a day, for runs that sync late. On Monday the athlete sees a message about last
week's medal until they close it or the day ends.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import points as PT
from .models import Athlete, WeekResult

KINDS = ("gold", "silver", "miles")
ICONS = {"gold": "🥇", "silver": "🥈", "miles": "👟"}
RECHECK_DAYS = 14


def medal_for(score: float | None, miles: float, goal_miles: float | None) -> str:
    status = PT.pace_status(score)
    if status == "good":
        return "gold"
    if status == "warn":
        return "silver"
    if goal_miles and miles >= goal_miles - 0.05:     # a rounding hair short still counts
        return "miles"
    return ""


def _weeks(today: dt.date) -> list[dt.date]:
    """Mondays of this season's finished weeks."""
    start = A.week_monday(A.season_range(today)[0])
    this_monday = A.week_monday(today)
    out, m = [], start
    while m < this_monday:
        out.append(m)
        m += dt.timedelta(days=7)
    return out


def season_weeks(db: Session, a: Athlete, today: dt.date) -> list[WeekResult]:
    """This season's finished weeks for the athlete, worked out where missing or recent."""
    mondays = _weeks(today)
    if not mondays:
        return []
    saved = {r.monday: r for r in db.scalars(select(WeekResult).where(
        WeekResult.athlete_id == a.id, WeekResult.monday >= mondays[0], WeekResult.monday <= mondays[-1])).all()}
    for m in mondays:
        r = saved.get(m)
        recent = m + dt.timedelta(days=6) >= today - dt.timedelta(days=RECHECK_DAYS)
        if r is not None and not (recent and r.computed_on < today):
            continue
        w = PT.team_week(db, m, athletes=[a])[0]
        if r is None:
            r = saved[m] = WeekResult(athlete_id=a.id, monday=m)
            db.add(r)
        r.score, r.miles, r.goal_miles = w["score"], w["miles"], w["goal_miles"]
        r.medal, r.computed_on = medal_for(w["score"], w["miles"], w["goal_miles"]), today
    db.flush()
    return [saved[m] for m in mondays]


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def summary(db: Session, a: Athlete, today: dt.date) -> dict:
    """Medal counts and their explanations for the header, and Monday's message about last week."""
    weeks = season_weeks(db, a, today)
    counts = {k: sum(1 for w in weeks if w.medal == k) for k in KINDS}
    season = A.season_for(today)
    text = {
        "gold": f"Gold: you hit your weekly goal (90% or more) in {counts['gold']} week{_s(counts['gold'])} of the {season} season.",
        "silver": f"Silver: you scored 80 to 89% of your weekly goal in {counts['silver']} week{_s(counts['silver'])} of the {season} season.",
        "miles": f"Miles: you ran your full weekly miles in {counts['miles']} more week{_s(counts['miles'])} of the {season} season.",
    }
    medals = [{"kind": k, "icon": ICONS[k], "count": counts[k], "text": text[k]} for k in KINDS if counts[k]]
    note = None
    last = weeks[-1] if weeks else None
    if today.weekday() == 0 and last is not None and last.monday == today - dt.timedelta(days=7) and last.medal and not last.note_closed:
        n = counts[last.medal]
        nth = f"your {_ordinal(n)} {last.medal if last.medal != 'miles' else 'full-mileage'} week this season"
        if last.medal == "gold":
            msg = f"Gold week! You scored {last.score:.0f}% last week, {nth}. Keep it rolling!"
        elif last.medal == "silver":
            msg = f"Silver week! You scored {last.score:.0f}% last week, {nth}. Reach 90% this week for gold."
        else:
            msg = f"You ran all {last.miles:.1f} of your {last.goal_miles:g} miles last week, {nth}. Nice work!"
        note = {"icon": ICONS[last.medal], "text": msg, "monday": last.monday}
    return {"medals": medals, "note": note}


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def close_note(db: Session, a: Athlete, monday: dt.date) -> None:
    r = db.scalar(select(WeekResult).where(WeekResult.athlete_id == a.id, WeekResult.monday == monday))
    if r is not None:
        r.note_closed = True

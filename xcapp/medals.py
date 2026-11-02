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
from . import seasons as S
from .models import Athlete, WeekResult

KINDS = ("gold", "silver", "miles")
ICONS = {"gold": "🥇", "silver": "🥈", "miles": "👟"}
RECHECK_DAYS = 14
RULES_CHANGED = dt.date(2026, 11, 2)   # weeks saved before this are worked out again (race days count as days run; running beyond a workout earns easy points)


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
    """Mondays of the finished weeks in this part of the school year (see seasons), and in last week's part when the
    year has just moved on. A week belongs to the part its Monday falls in."""
    this_monday = A.week_monday(today)
    start = A.week_monday(min(S.period_for(today)["start"], S.period_for(this_monday - dt.timedelta(days=7))["start"]))
    out, m = [], start
    while m < this_monday:
        out.append(m)
        m += dt.timedelta(days=7)
    return out


def ensure_weeks(db: Session, athletes: list[Athlete], mondays: list[dt.date], today: dt.date) -> dict[tuple[int, dt.date], WeekResult]:
    """Saved finished weeks for these athletes, worked out where missing, saved before points were kept, or recent
    (once a day). One team calculation per week for all the athletes that need it."""
    if not athletes or not mondays:
        return {}
    ids = [a.id for a in athletes]
    saved = {(r.athlete_id, r.monday): r for r in db.scalars(select(WeekResult).where(
        WeekResult.athlete_id.in_(ids), WeekResult.monday >= min(mondays), WeekResult.monday <= max(mondays))).all()}
    for m in mondays:
        recent = m + dt.timedelta(days=6) >= today - dt.timedelta(days=RECHECK_DAYS)
        need = [a for a in athletes if (r := saved.get((a.id, m))) is None or r.points is None or r.run_days is None
                or r.computed_on < RULES_CHANGED <= today
                or (recent and r.computed_on < today)]
        if not need:
            continue
        for w in PT.team_week(db, m, athletes=need):
            key = (w["a"].id, m)
            r = saved.get(key)
            if r is None:
                r = saved[key] = WeekResult(athlete_id=w["a"].id, monday=m)
                db.add(r)
            r.score, r.miles, r.goal_miles, r.points, r.target = w["score"], w["miles"], w["goal_miles"], w["points"], w["target"]
            r.run_days = w["run_days"]
            r.medal, r.computed_on = medal_for(w["score"], w["miles"], w["goal_miles"]), today
    db.flush()
    return saved


def season_weeks(db: Session, a: Athlete, today: dt.date) -> list[WeekResult]:
    """This season's finished weeks for the athlete."""
    mondays = _weeks(today)
    saved = ensure_weeks(db, [a], mondays, today)
    return [saved[(a.id, m)] for m in mondays]


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def summary(db: Session, a: Athlete, today: dt.date) -> dict:
    """Medal counts and their explanations for the header, and Monday's message about last week."""
    every = season_weeks(db, a, today)
    cur = S.period_for(today)

    def within(p):
        return [w for w in every if S.period_for(w.monday)["label"] == p["label"]]

    weeks = within(cur)
    counts = {k: sum(1 for w in weeks if w.medal == k) for k in KINDS}
    season = cur["label"]
    text = {
        "gold": f"Gold: you hit your weekly goal (90% or more) in {counts['gold']} week{_s(counts['gold'])} of {season}.",
        "silver": f"Silver: you scored 80 to 89% of your weekly goal in {counts['silver']} week{_s(counts['silver'])} of {season}.",
        "miles": f"Miles: you ran your full weekly miles in {counts['miles']} more week{_s(counts['miles'])} of {season}.",
    }
    medals = [{"kind": k, "icon": ICONS[k], "count": counts[k], "text": text[k]} for k in KINDS if counts[k]]
    note = None
    last = every[-1] if every else None
    if today.weekday() == 0 and last is not None and last.monday == today - dt.timedelta(days=7) and last.medal and not last.note_closed:
        lp = S.period_for(last.monday)
        n = sum(1 for w in within(lp) if w.medal == last.medal)
        nth = f"your {_ordinal(n)} {last.medal if last.medal != 'miles' else 'full-mileage'} week of {lp['label']}"
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

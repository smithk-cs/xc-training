"""Mileage challenges, such as summer base building: between two dates, each athlete's total miles, days run and
longest streak of days in a row, ranked on a leaderboard; their progress toward an optional goal; and, when the
challenge is linked to a squad competition, each squad's average miles. Athletes see the board on their page, with
teammates' names only when those teammates allow it (Athlete.show_name)."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analysis as A
from .fitfile import RUNNING_SPORTS
from .models import Activity, Athlete, MileageChallenge, Squad, SquadMember

METERS_PER_MILE = 1609.344


def is_run(sport: str) -> bool:
    return sport in RUNNING_SPORTS or (sport or "").endswith("running")


def streaks(days: set[dt.date], end: dt.date) -> tuple[int, int]:
    """(longest run of days in a row, the run of days ending on `end` or the day before)."""
    best = cur = 0
    prev = None
    for d in sorted(days):
        cur = cur + 1 if prev is not None and (d - prev).days == 1 else 1
        best = max(best, cur)
        prev = d
    now = 0
    d = end if end in days else end - dt.timedelta(days=1)
    while d in days:
        now += 1
        d -= dt.timedelta(days=1)
    return best, now


def board(db: Session, c: MileageChallenge, today: dt.date) -> dict:
    """The leaderboard: {"rows": [{"athlete", "miles", "days", "best", "now", "pct", "rank"}], "squads": [...],
    "total", "days_left", "until"}."""
    until = min(c.end_date, today)
    q = select(Athlete).where(A.on_team())
    if c.team in ("Boys", "Girls"):
        q = q.where(Athlete.gender == c.team)
    people = db.scalars(q).all()
    ids = [a.id for a in people]
    miles, days = defaultdict(float), defaultdict(set)
    for aid, d, m, sport in db.execute(select(Activity.athlete_id, Activity.local_date, Activity.distance_m, Activity.sport).where(
            Activity.athlete_id.in_(ids or [-1]), Activity.local_date >= c.start_date, Activity.local_date <= until)).all():
        if is_run(sport):
            miles[aid] += (m or 0) / METERS_PER_MILE
            days[aid].add(d)
    rows = []
    for a in people:
        best, now = streaks(days[a.id], until)
        rows.append({"athlete": a, "miles": round(miles[a.id], 1), "days": len(days[a.id]), "best": best, "now": now,
                     "pct": round(100 * miles[a.id] / c.goal_miles) if c.goal_miles else None})
    rows.sort(key=lambda r: (-r["miles"], -r["days"], r["athlete"].last_name))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    squads = []
    if c.squads_id:
        by = {aid: sid for sid, aid in db.execute(select(SquadMember.squad_id, SquadMember.athlete_id).where(
            SquadMember.competition_id == c.squads_id)).all()}
        for s in db.scalars(select(Squad).where(Squad.competition_id == c.squads_id).order_by(Squad.position)).all():
            mine = [r for r in rows if by.get(r["athlete"].id) == s.id]
            if mine:
                squads.append({"squad": s, "members": len(mine), "miles": round(sum(r["miles"] for r in mine), 1),
                               "avg": round(sum(r["miles"] for r in mine) / len(mine), 1)})
        squads.sort(key=lambda x: -x["avg"])
    return {"rows": rows, "squads": squads, "total": round(sum(r["miles"] for r in rows), 1), "until": until,
            "days_left": max(0, (c.end_date - today).days), "started": today >= c.start_date}


def current(db: Session, today: dt.date, gender: str = "") -> MileageChallenge | None:
    """The challenge running now (or the latest one that ended in the last week) for an athlete of that team."""
    q = select(MileageChallenge).where(MileageChallenge.start_date <= today, MileageChallenge.end_date >= today - dt.timedelta(days=7))
    found = [c for c in db.scalars(q.order_by(MileageChallenge.start_date.desc())).all() if c.team in ("All", gender) or not gender]
    return found[0] if found else None


def shown_name(a: Athlete, viewer_id: int) -> str:
    """How a teammate appears on an athlete's board: their name if they allow it, else "Teammate"."""
    return a.name if a.id == viewer_id or a.show_name else "Teammate"

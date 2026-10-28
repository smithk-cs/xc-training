"""Class years: an athlete's grade follows from their graduation year each school year (July 1 to June 30), and after
that school year ends they become alumni: off the roster, with every record kept as team history."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ingest import log
from .models import Athlete, RaceResult


def school_year_end(d: dt.date) -> int:
    """The calendar year a school year ends in: Oct 2026 is in the 2026-27 school year, so 2027."""
    return d.year + 1 if d.month >= 7 else d.year


def grade_for(grad_year: int, on: dt.date) -> int:
    return 12 - (grad_year - school_year_end(on))


def class_of(grade: int, on: dt.date) -> int:
    """Graduation year of someone in `grade` on date `on`."""
    return school_year_end(on) + 12 - grade


def latest_races(db: Session) -> dict[int, dt.date]:
    return dict(db.execute(select(RaceResult.athlete_id, func.max(RaceResult.date)).group_by(RaceResult.athlete_id)).all())


def to_confirm(db: Session, today: dt.date) -> list[dict]:
    """Athletes with no class year whose grade may be from an older race (their latest race was in an earlier school
    year), with a suggested class year from that race. Ones whose grade is clearly current get filled in by refresh()."""
    last = latest_races(db)
    out = []
    for a in db.scalars(select(Athlete).where(Athlete.grad_year.is_(None), Athlete.alumni.is_(False))
                        .order_by(Athlete.last_name, Athlete.first_name)).all():
        when = last.get(a.id)
        if when and school_year_end(when) < school_year_end(today):
            out.append({"athlete": a, "latest": when, "suggested": class_of(a.grade, when)})
    return out


def refresh(db: Session, today: dt.date, disconnect=None) -> dict:
    """Fill in clear class years, move grades up, and graduate classes whose school year has ended. `disconnect`, when
    given, is called with each graduate's Garmin connection (the nightly job ends them; page loads just mark alumni)."""
    pending = {x["athlete"].id for x in to_confirm(db, today)}
    out = {"filled": 0, "graduated": 0, "grades": 0}
    for a in db.scalars(select(Athlete).where(Athlete.alumni.is_(False))).all():
        if a.grad_year is None:
            if a.id in pending or not a.active:
                continue
            a.grad_year = class_of(a.grade, today)
            out["filled"] += 1
        if school_year_end(today) > a.grad_year:
            a.alumni, a.active, a.left_on = True, False, None
            if a.garmin is not None and a.garmin.status != "revoked" and disconnect:
                disconnect(a.garmin)
            log(db, "roster", f"{a.name} graduated (class of {a.grad_year}) and moved to alumni.")
            out["graduated"] += 1
            continue
        g = grade_for(a.grad_year, today)
        if a.grade != g and 6 <= g <= 12:
            a.grade = g
            out["grades"] += 1
    if disconnect:  # alumni marked during the day whose Garmin is still connected
        for a in db.scalars(select(Athlete).where(Athlete.alumni.is_(True))).all():
            if a.garmin is not None and a.garmin.status not in ("revoked",):
                disconnect(a.garmin)
    db.flush()
    return out

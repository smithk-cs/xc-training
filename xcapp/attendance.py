"""Practice attendance, tapped in by a coach at practice: present, late, excused or absent for each athlete. The list
shows what athletes already told the coaches (a missed-day report), and the season summary gives each athlete's
attendance rate (late counts as there; excused days don't count against them)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .models import Athlete, Attendance, MissedDay

STATUSES = [("present", "Here"), ("late", "Late"), ("excused", "Excused"), ("absent", "Absent")]
THERE = {"present", "late"}


def roster(db: Session, team: str = "All") -> list[Athlete]:
    q = select(Athlete).where(A.on_team())
    if team in ("Boys", "Girls"):
        q = q.where(Athlete.gender == team)
    return db.scalars(q.order_by(Athlete.last_name, Athlete.first_name)).all()


def day(db: Session, d: dt.date, ids: list[int]) -> tuple[dict[int, Attendance], dict[int, MissedDay]]:
    """That day's marks and the athletes' own missed-day reports, by athlete id."""
    marks = {x.athlete_id: x for x in db.scalars(select(Attendance).where(Attendance.date == d, Attendance.athlete_id.in_(ids or [-1]))).all()}
    told = {x.athlete_id: x for x in db.scalars(select(MissedDay).where(MissedDay.date == d, MissedDay.athlete_id.in_(ids or [-1]))).all()}
    return marks, told


def save(db: Session, d: dt.date, choices: dict[int, str], by: str) -> int:
    """Save the marks for a day ({athlete id: status}; "" clears one). Returns how many changed."""
    marks, _ = day(db, d, list(choices))
    n = 0
    for aid, status in choices.items():
        cur = marks.get(aid)
        if status == "":
            if cur is not None:
                db.delete(cur)
                n += 1
            continue
        if status not in dict(STATUSES):
            continue
        if cur is None:
            db.add(Attendance(athlete_id=aid, date=d, status=status, by=by[:80]))
            n += 1
        elif cur.status != status:
            cur.status, cur.by = status, by[:80]
            n += 1
    db.flush()
    return n


def summary(db: Session, start: dt.date, end: dt.date, ids: list[int]) -> dict[int, dict]:
    """Each athlete's marks between two dates: {"there", "late", "excused", "absent", "days", "rate" (% of the days
    that count, excused left out), "missed": dates absent}."""
    out = {aid: {"there": 0, "late": 0, "excused": 0, "absent": 0, "days": 0, "rate": None, "missed": []} for aid in ids}
    for m in db.scalars(select(Attendance).where(Attendance.date >= start, Attendance.date <= end,
                                                 Attendance.athlete_id.in_(ids or [-1]))).all():
        s = out[m.athlete_id]
        s["days"] += 1
        if m.status in THERE:
            s["there"] += 1
            s["late"] += m.status == "late"
        elif m.status == "excused":
            s["excused"] += 1
        else:
            s["absent"] += 1
            s["missed"].append(m.date)
    for s in out.values():
        counted = s["there"] + s["absent"]
        s["rate"] = round(100 * s["there"] / counted) if counted else None
    return out


def marked_days(db: Session, start: dt.date, end: dt.date) -> list[dt.date]:
    return sorted({d for (d,) in db.execute(select(Attendance.date).where(Attendance.date >= start, Attendance.date <= end).distinct()).all()})

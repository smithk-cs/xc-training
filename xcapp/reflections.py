"""Post-race reflections: after each race the athlete says how happy they are with it (1-5), what went well, what
they'd change and their focus for the next race. Coaches read a meet's reflections together; each athlete's build up
over the season on their page."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Athlete, RaceReflection, RaceResult

HAPPY = [(1, "Not at all"), (2, "Not really"), (3, "OK"), (4, "Happy"), (5, "Very happy")]
ASK_DAYS = 14       # races this recent get a nudge on the athlete's page


def save(db: Session, a: Athlete, r: RaceResult, happy, went_well: str, change: str, next_focus: str) -> RaceReflection:
    if r.athlete_id != a.id:
        raise ValueError("That isn’t your race.")
    try:
        h = int(happy)
    except (TypeError, ValueError):
        raise ValueError("Tap how happy you are with the race.") from None
    if h not in dict(HAPPY):
        raise ValueError("Tap how happy you are with the race.")
    went_well, change, next_focus = (str(x or "").strip() for x in (went_well, change, next_focus))
    if not (went_well or change or next_focus):
        raise ValueError("Write at least one answer.")
    ref = db.scalar(select(RaceReflection).where(RaceReflection.race_result_id == r.id))
    if ref is None:
        ref = RaceReflection(race_result_id=r.id, athlete_id=a.id, happy=h)
        db.add(ref)
    ref.happy, ref.went_well, ref.change, ref.next_focus = h, went_well[:500], change[:500], next_focus[:300]
    db.flush()
    return ref


def by_race(db: Session, athlete_id: int) -> dict[int, RaceReflection]:
    return {x.race_result_id: x for x in db.scalars(select(RaceReflection).options(selectinload(RaceReflection.race))
                                                     .where(RaceReflection.athlete_id == athlete_id)).all()}


def to_ask(races: list[RaceResult], done: dict[int, RaceReflection], today: dt.date) -> RaceResult | None:
    """The most recent race in the last two weeks without a reflection, for a nudge on the athlete's page."""
    recent = [r for r in races if r.id not in done and today - dt.timedelta(days=ASK_DAYS) <= r.date <= today]
    return max(recent, key=lambda r: (r.date, r.id), default=None)


def for_meet(db: Session, day: dt.date, meet: str) -> tuple[list[RaceResult], dict[int, RaceReflection]]:
    """A meet's results (on the team) and the reflections on them."""
    races = db.scalars(select(RaceResult).options(selectinload(RaceResult.athlete)).where(RaceResult.date == day, RaceResult.meet == meet)
                       .order_by(RaceResult.race, RaceResult.time_s)).all()
    refs = {x.race_result_id: x for x in db.scalars(select(RaceReflection).where(
        RaceReflection.race_result_id.in_([r.id for r in races] or [-1]))).all()}
    return races, refs


def counts(db: Session) -> dict[tuple, int]:
    """(date, meet) -> how many reflections, for the Races page."""
    out: dict[tuple, int] = {}
    for d, m in db.execute(select(RaceResult.date, RaceResult.meet).join(RaceReflection, RaceReflection.race_result_id == RaceResult.id)).all():
        out[(d, m)] = out.get((d, m), 0) + 1
    return out

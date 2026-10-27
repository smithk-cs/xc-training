"""Combining two roster entries for the same athlete (for example 'Will Pell' from the roster and 'William Pell' from
a meet's results): everything moves to the one kept, overlaps are dropped, and the other name is remembered so future
uploads match it."""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .ingest import DUPLICATE_WINDOW_S
from .models import (Activity, Athlete, AthletePhoto, DeletionRequest, DivisionAthlete, GarminConnection, Goal, Injury, Insight, MissedDay,
                     OAuthState, PainReport, RaceResult, RestingHeartRate, Result)


def other_names(a: Athlete) -> list[str]:
    return [n.strip() for n in re.split(r"[;\n]", a.other_names or "") if n.strip()]


def add_other_names(a: Athlete, names: list[str]) -> None:
    have = {n.lower() for n in other_names(a)} | {a.name.lower()}
    keep = other_names(a)
    for n in names:
        n = re.sub(r"\s+", " ", n).strip()
        if n and n.lower() not in have:
            keep.append(n)
            have.add(n.lower())
    a.other_names = "; ".join(keep)[:500]


def likely_duplicates(athletes: list[Athlete]) -> list[tuple[Athlete, Athlete]]:
    """Pairs with the same last name whose first names start alike (Nick / Nicholas, Rafa / Rafael)."""
    out = []
    for i, a in enumerate(athletes):
        for b in athletes[i + 1:]:
            fa, fb = a.first_name.lower(), b.first_name.lower()
            if a.last_name.lower() == b.last_name.lower() and fa != fb and (fa.startswith(fb[:3]) or fb.startswith(fa[:3])):
                out.append((a, b))
    return out


def merge(db: Session, keep: Athlete, other: Athlete) -> dict[str, int]:
    """Move all of `other`'s data to `keep` and delete `other`. Returns how many records moved and how many overlapping
    ones were dropped. The caller ends `other`'s Garmin connection first if both are connected."""
    if keep.id == other.id:
        raise ValueError("Pick a different athlete.")
    k, o = keep.id, other.id
    moved = dropped = 0

    def drop(ids):
        nonlocal dropped
        dropped += len(ids)
        return ids

    # Runs: the same run uploaded under both names stays once (on the kept athlete).
    mine = db.execute(select(Activity.external_id, Activity.start_utc).where(Activity.athlete_id == k)).all()
    ext = {e for e, _ in mine}
    win = dt.timedelta(seconds=DUPLICATE_WINDOW_S)
    dup_runs = [a.id for a in db.scalars(select(Activity).where(Activity.athlete_id == o)).all()
                if a.external_id in ext or any(abs(a.start_utc - s) <= win for _, s in mine)]
    if dup_runs:
        db.execute(delete(Activity).where(Activity.id.in_(drop(dup_runs))))
    # One per day / per event and season: the kept athlete's wins.
    for model, cols in ((RestingHeartRate, ("date",)), (MissedDay, ("date",)), (Goal, ("event", "season")), (DivisionAthlete, ("division_id",))):
        have = {tuple(getattr(r, c) for c in cols) for r in db.scalars(select(model).where(model.athlete_id == k)).all()}
        clash = [r.id for r in db.scalars(select(model).where(model.athlete_id == o)).all() if tuple(getattr(r, c) for c in cols) in have]
        if clash:
            db.execute(delete(model).where(model.id.in_(drop(clash))))
    # The same meet result imported under both names.
    have = {(r.date, r.meet, r.race) for r in db.scalars(select(RaceResult).where(RaceResult.athlete_id == k)).all()}
    clash = [r for r in db.scalars(select(RaceResult).where(RaceResult.athlete_id == o)).all() if (r.date, r.meet, r.race) in have]
    for r in clash:
        if r.result_id:
            db.execute(delete(Result).where(Result.id == r.result_id))
    if clash:
        db.execute(delete(RaceResult).where(RaceResult.id.in_(drop([r.id for r in clash]))))
    have = {(r.date, r.event, round(r.time_s, 1)) for r in db.scalars(select(Result).where(Result.athlete_id == k)).all()}
    clash = [r.id for r in db.scalars(select(Result).where(Result.athlete_id == o)).all() if (r.date, r.event, round(r.time_s, 1)) in have]
    if clash:
        db.execute(update(RaceResult).where(RaceResult.result_id.in_(clash)).values(result_id=None))
        db.execute(delete(Result).where(Result.id.in_(drop(clash))))
    # One Garmin connection and one photo each: the kept athlete's, else the other's.
    for model in (GarminConnection, AthletePhoto):
        if db.scalar(select(model.id).where(model.athlete_id == k)) is not None:
            n = db.execute(delete(model).where(model.athlete_id == o)).rowcount
            dropped += n or 0
    db.execute(delete(OAuthState).where(OAuthState.athlete_id == o))
    for model in (Activity, Result, RaceResult, Goal, Injury, PainReport, RestingHeartRate, MissedDay, GarminConnection,
                  AthletePhoto, DeletionRequest, Insight, DivisionAthlete):
        moved += db.execute(update(model).where(model.athlete_id == o).values(athlete_id=k)).rowcount or 0

    # Fill in what the kept athlete doesn't have yet.
    for field in ("hr_max", "hr_rest", "vdot_override", "ai_consent_at", "ai_consent_note", "weekly_plan", "vdot_mark", "ability_override"):
        if not getattr(keep, field) and getattr(other, field):
            setattr(keep, field, getattr(other, field))
    add_other_names(keep, [other.name] + other_names(other))
    db.flush()
    db.expire_all()
    db.delete(db.get(Athlete, o))
    db.flush()
    return {"moved": moved, "dropped": dropped}

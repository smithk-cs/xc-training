"""CSV imports for the roster, benchmark results (e.g. exported from XC Stats or Athletic.net) and goals.

Every row is checked; bad rows are reported by line number and nothing from a file is saved unless
the whole file is valid, so a coach never ends up with half an import.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analysis import EVENTS, normalize_event, parse_time, season_for
from .models import Athlete, Goal, Result
from .security import new_link_token

GROUPS = ("Varsity", "JV", "Development")
GENDERS = {"boys": "Boys", "boy": "Boys", "m": "Boys", "male": "Boys", "girls": "Girls", "girl": "Girls", "f": "Girls", "female": "Girls"}


class ImportErrors(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def _rows(text: str) -> list[tuple[int, dict]]:
    text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportErrors(["The file has no header row."])
    reader.fieldnames = [re.sub(r"\s+", "_", f.strip().lower()) for f in reader.fieldnames]
    return [(i, {k: (v or "").strip() for k, v in r.items() if k}) for i, r in enumerate(reader, start=2)]


def parse_date(text: str) -> dt.date:
    s = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Date '{text}' should look like 2026-10-17 or 10/17/2026")


def _name_index(db: Session) -> dict[str, list[Athlete]]:
    idx: dict[str, list[Athlete]] = {}
    for a in db.scalars(select(Athlete)).all():
        idx.setdefault(a.name.lower(), []).append(a)
    return idx


def _find(idx, name: str) -> Athlete:
    key = re.sub(r"\s+", " ", name.strip().lower())
    m = idx.get(key, [])
    if not m and "," in key:  # "Last, First"
        last, first = [x.strip() for x in key.split(",", 1)]
        m = idx.get(f"{first} {last}", [])
    if not m:
        raise ValueError(f"No athlete named '{name}' on the roster")
    if len(m) > 1:
        raise ValueError(f"More than one athlete is named '{name}'")
    return m[0]


def _int_or_none(v: str, lo: int, hi: int, label: str):
    if not v:
        return None
    n = int(v)
    if not lo <= n <= hi:
        raise ValueError(f"{label} {n} is outside {lo}-{hi}")
    return n


def import_roster(db: Session, text: str) -> int:
    """Columns: name (or first_name + last_name), gender, grade, group, hr_max, hr_rest. Updates matching names."""
    errors, todo = [], []
    idx = _name_index(db)
    for line, r in _rows(text):
        try:
            if r.get("first_name") or r.get("last_name"):
                first, last = r.get("first_name", ""), r.get("last_name", "")
            else:
                parts = r.get("name", "").split(None, 1)
                first, last = (parts + [""])[:2]
            if not first:
                raise ValueError("name is missing")
            gender = GENDERS.get(r.get("gender", "").lower())
            if not gender:
                raise ValueError("gender should be Boys or Girls")
            grade = _int_or_none(r.get("grade", ""), 6, 12, "grade")
            if grade is None:
                raise ValueError("grade is missing")
            group = next((g for g in GROUPS if g.lower() == r.get("group", "JV").lower()), None)
            if not group:
                raise ValueError("group should be Varsity, JV or Development")
            todo.append((first, last, gender, grade, group,
                         _int_or_none(r.get("hr_max", ""), 150, 230, "hr_max"), _int_or_none(r.get("hr_rest", ""), 30, 100, "hr_rest")))
        except ValueError as exc:
            errors.append(f"Line {line}: {exc}")
    if errors:
        raise ImportErrors(errors)
    for first, last, gender, grade, group, hrmax, hrrest in todo:
        existing = idx.get(f"{first} {last}".strip().lower())
        a = existing[0] if existing else Athlete(first_name=first, last_name=last, link_token=new_link_token())
        a.gender, a.grade, a.group, a.hr_max, a.hr_rest, a.active = gender, grade, group, hrmax, hrrest, True
        if not existing:
            db.add(a)
            idx[a.name.lower()] = [a]
    db.flush()
    return len(todo)


def import_results(db: Session, text: str) -> int:
    """Columns: athlete, event, time, date, and optional course, kind (race | time trial | track)."""
    errors, todo = [], []
    idx = _name_index(db)
    for line, r in _rows(text):
        try:
            a = _find(idx, r.get("athlete") or r.get("name") or "")
            event, factor = normalize_event(r.get("event", ""))
            t = parse_time(r.get("time", "")) * factor
            d = parse_date(r.get("date", ""))
            kind = (r.get("kind") or "race").lower()
            if kind not in ("race", "time trial", "track"):
                raise ValueError("kind should be race, time trial or track")
            note = "" if factor == 1.0 else f"converted from {r.get('event')}"
            todo.append(Result(athlete_id=a.id, event=event, time_s=round(t, 2), date=d, season=season_for(d),
                               course=r.get("course", "")[:120], kind=kind, note=note))
        except ValueError as exc:
            errors.append(f"Line {line}: {exc}")
    if errors:
        raise ImportErrors(errors)
    for res in todo:
        dup = db.scalar(select(Result).where(Result.athlete_id == res.athlete_id, Result.event == res.event,
                                             Result.date == res.date, Result.time_s == res.time_s))
        if dup is None:
            db.add(res)
    db.flush()
    return len(todo)


def import_goals(db: Session, text: str, default_season: str) -> int:
    """Columns: athlete, event, goal, and optional season (defaults to the current XC season)."""
    errors, todo = [], []
    idx = _name_index(db)
    for line, r in _rows(text):
        try:
            a = _find(idx, r.get("athlete") or r.get("name") or "")
            event, factor = normalize_event(r.get("event", ""))
            if event not in EVENTS:
                raise ValueError("unknown event")
            season = r.get("season") or default_season
            if not re.fullmatch(r"\d{4} (XC|Track)", season):
                raise ValueError("season should look like 2026 XC")
            todo.append((a.id, event, season, round(parse_time(r.get("goal") or r.get("time") or "") * factor, 2)))
        except ValueError as exc:
            errors.append(f"Line {line}: {exc}")
    if errors:
        raise ImportErrors(errors)
    for aid, event, season, g in todo:
        set_goal(db, aid, event, season, g)
    db.flush()
    return len(todo)


def set_goal(db: Session, athlete_id: int, event: str, season: str, goal_s: float) -> Goal:
    g = db.scalar(select(Goal).where(Goal.athlete_id == athlete_id, Goal.event == event, Goal.season == season))
    if g is None:
        g = Goal(athlete_id=athlete_id, event=event, season=season, goal_s=goal_s)
        db.add(g)
    else:
        g.goal_s = goal_s
    return g

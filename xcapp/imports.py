"""CSV imports for the roster, benchmark results (e.g. exported from XC Stats or Athletic.net), race results and goals.

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
from .models import Athlete, Goal, RaceResult, Result
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


CLASS_YEARS = {"fr": 9, "freshman": 9, "so": 10, "soph": 10, "sophomore": 10, "jr": 11, "junior": 11, "sr": 12, "senior": 12}


def _split_name(name: str) -> tuple[str, str]:
    """'Last, First' or 'First Last'."""
    if "," in name:
        last, first = (x.strip() for x in name.split(",", 1))
        return first, last
    return tuple((name.split(None, 1) + [""])[:2])


def _grade(text: str) -> int | None:
    year = CLASS_YEARS.get(text.strip().lower().rstrip("."))
    return year if year is not None else _int_or_none(text, 6, 12, "grade")


def import_roster(db: Session, text: str) -> int:
    """Columns: name ('First Last' or 'Last, First'; or first_name + last_name), gender, grade (9-12 or Fr/So/Jr/Sr;
    also read from gr or yr), group, hr_max, hr_rest. Adds new athletes; for ones already on the roster, changes only the
    columns the file fills in, so a names-and-grades list leaves groups and heart rates alone. Blank rows are skipped."""
    errors, todo = [], []
    idx = _name_index(db)
    for line, r in _rows(text):
        if not any(r.values()):
            continue
        try:
            if r.get("first_name") or r.get("last_name"):
                first, last = r.get("first_name", ""), r.get("last_name", "")
            else:
                first, last = _split_name(r.get("name", ""))
            if not first:
                raise ValueError("name is missing")
            existing = idx.get(f"{first} {last}".strip().lower())
            gender = GENDERS.get(r.get("gender", "").lower())
            if r.get("gender") and not gender:
                raise ValueError("gender should be Boys or Girls")
            if not gender and not existing:
                raise ValueError(f"add a gender (Boys or Girls) for new athlete '{first} {last}'")
            grade = _grade(r.get("grade") or r.get("gr") or r.get("yr") or "")
            if grade is None and not existing:
                raise ValueError("grade is missing")
            group = next((g for g in GROUPS if g.lower() == r.get("group", "").lower()), None)
            if r.get("group") and not group:
                raise ValueError("group should be Varsity, JV or Development")
            todo.append((existing[0] if existing else None, first, last, gender, grade, group,
                         _int_or_none(r.get("hr_max", ""), 150, 230, "hr_max"), _int_or_none(r.get("hr_rest", ""), 30, 100, "hr_rest")))
        except ValueError as exc:
            errors.append(f"Line {line}: {exc}")
    if errors:
        raise ImportErrors(errors)
    for a, first, last, gender, grade, group, hrmax, hrrest in todo:
        if a is None:
            a = Athlete(first_name=first, last_name=last, link_token=new_link_token(), group=group or "JV")
            db.add(a)
            idx[f"{first} {last}".strip().lower()] = [a]
        for field, value in (("gender", gender), ("grade", grade), ("group", group), ("hr_max", hrmax), ("hr_rest", hrrest)):
            if value is not None:
                setattr(a, field, value)
        a.active = True
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


# Benchmark events by distance in meters: a race within 0.5% of one also counts toward VDOT and running groups.
BENCHMARK_METERS = {"1 Mile": 1609.344, "1.5 Mile": 2414.016, "2 Mile": 3218.688, "5K": 5000.0}
_UNITS = {"m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0, "k": 1000.0, "km": 1000.0,
          "mi": 1609.344, "mile": 1609.344, "miles": 1609.344}


def parse_distance(text: str) -> float:
    """'5,000 Meters' -> 5000 ; '4K' -> 4000 ; '3 Miles' -> 4828.0 ; '2 mi' -> 3218.7"""
    s = re.sub(r"[,\s]", "", str(text).lower())
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]*)", s)
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        raise ValueError(f"Distance '{text}' should look like 5,000 Meters, 4K or 3 Miles")
    value = float(m.group(1)) * _UNITS.get(m.group(2) or "m", 1.0)
    if not 400 <= value <= 50000:
        raise ValueError(f"Distance '{text}' is not a race distance")
    return round(value, 1)


def benchmark_event(meters: float) -> str | None:
    return next((ev for ev, d in BENCHMARK_METERS.items() if abs(meters - d) <= d * 0.005), None)


def _from_race(race: str, choices: dict[str, str]) -> str | None:
    words = re.findall(r"[a-z]+", race.lower())
    return next((v for k, v in choices.items() if k in words), None)


def import_races(db: Session, text: str, add_athletes: bool = False) -> tuple[int, int]:
    """Columns: meet, date, race (or division), distance, athlete (or name), time; optional course, place, and, for
    athletes not on the roster when add_athletes is on, grade (or yr), gender and group (read from the race if missing).
    Returns (results imported, athletes added)."""
    errors, todo, new = [], [], {}
    idx = _name_index(db)
    for line, r in _rows(text):
        try:
            meet = (r.get("meet") or "").strip()
            race = (r.get("race") or r.get("division") or "").strip()
            name = (r.get("athlete") or r.get("name") or "").strip()
            if not meet or not name:
                raise ValueError("meet and athlete are required")
            d = parse_date(r.get("date", ""))
            label = (r.get("distance") or "").strip()
            meters = parse_distance(label)
            t = parse_time(r.get("time", ""))
            place = _int_or_none(r.get("place", ""), 1, 5000, "place")
            key = re.sub(r"\s+", " ", name.lower())
            try:
                a = _find(idx, name)
            except ValueError:
                if not add_athletes or key in new:
                    if key not in new:
                        raise
                    a = new[key]
                else:
                    parts = name.split(",", 1)
                    first, last = ((parts[1].strip(), parts[0].strip()) if len(parts) == 2 else (name.split(None, 1) + [""])[:2])
                    gender = GENDERS.get((r.get("gender") or "").lower()) or _from_race(race, {
                        "boys": "Boys", "boy": "Boys", "men": "Boys", "mens": "Boys", "girls": "Girls", "girl": "Girls",
                        "women": "Girls", "womens": "Girls"})
                    if not gender:
                        raise ValueError(f"add a gender (Boys or Girls) for new athlete '{name}'")
                    grade = _int_or_none(r.get("grade") or r.get("yr") or "", 6, 12, "grade")
                    if grade is None:
                        raise ValueError(f"add a grade for new athlete '{name}'")
                    group = next((g for g in GROUPS if g.lower() == (r.get("group") or "").lower()), None) or _from_race(
                        race, {"varsity": "Varsity", "jv": "JV", "frosh": "Development", "freshman": "Development",
                               "sophomore": "Development", "novice": "Development"}) or "Development"
                    a = Athlete(first_name=first, last_name=last, gender=gender, grade=grade, group=group,
                                link_token=new_link_token())
                    new[key] = a
            todo.append((a, meet, d, (r.get("course") or "").strip()[:160], race[:120], meters, label[:40] or f"{meters:g} m", t, place))
        except ValueError as exc:
            errors.append(f"Line {line}: {exc}")
    if errors:
        raise ImportErrors(errors)
    for a in new.values():
        db.add(a)
    db.flush()
    n = 0
    for a, meet, d, course, race, meters, label, t, place in todo:
        if db.scalar(select(RaceResult.id).where(RaceResult.athlete_id == a.id, RaceResult.date == d, RaceResult.meet == meet,
                                                 RaceResult.race == race, RaceResult.time_s == t)):
            continue
        rr = RaceResult(athlete_id=a.id, meet=meet[:160], date=d, course=course, race=race, distance_m=meters,
                        distance_label=label, time_s=t, place=place)
        event = benchmark_event(meters)
        if event:
            res = db.scalar(select(Result).where(Result.athlete_id == a.id, Result.event == event, Result.date == d,
                                                 Result.time_s == round(t, 2)))
            if res is None:
                res = Result(athlete_id=a.id, event=event, time_s=round(t, 2), date=d, season=season_for(d),
                             course=(course or meet)[:120], kind="race", note=f"{meet} ({race})"[:200] if race else meet[:200])
                db.add(res)
                db.flush()
            rr.result_id = res.id
        db.add(rr)
        n += 1
    db.flush()
    return n, len(new)


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

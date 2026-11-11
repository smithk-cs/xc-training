"""Season awards: each class's top 5 scorers (MVP) and top 5 most improved, from the season's race results.

- Classes: Freshmen (9th grade), Sophomores (10th) and Varsity (11th and 12th), for the Boys or the Girls, by the grade
  each athlete was in that season.
- Who counts: athletes who ran at least 80% of the meets their class raced in that season (a meet where at least one
  athlete of that class, same team, ran). Freshmen don't run sectionals, so they aren't held to meets they couldn't enter.
- MVP: lowest average finishing place (in cross country a runner scores the points of their place, so lower is better),
  over the races with a place in the results. Ties go to more races, then the faster average adjusted time.
- Most improved: average mile pace, from the time adjusted for the course and weather, at one meet against another (the
  coach picks them; by default each class's first and last meet of the season). Athletes who ran both meets are ranked
  by how many seconds a mile faster they got. Pace depends on the distance too, so the two meets are best at the same
  distance (each athlete's distances are shown).
"""
from __future__ import annotations

import datetime as dt
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import classyear as CY
from . import seasons as SE
from .courses import adjusted_times
from .models import Athlete, RaceResult

CLASSES = (("freshmen", "Freshmen", (9,)), ("sophomores", "Sophomores", (10,)), ("varsity", "Varsity", (11, 12)))
MIN_SHARE = 0.8
TOP = 5


def grade_in(a: Athlete, season_start: dt.date, today: dt.date) -> int | None:
    """The grade the athlete was in during the season."""
    if a.grad_year:
        return CY.grade_for(a.grad_year, season_start)
    if a.grade:
        return a.grade - (CY.school_year_end(today) - CY.school_year_end(season_start))
    return None


def xc_seasons(db: Session, today: dt.date) -> list[dict]:
    """Cross country seasons with races, newest first."""
    first = db.scalar(select(RaceResult.date).order_by(RaceResult.date).limit(1))
    if not first:
        return [SE.period(SE.school_year_of(today), "xc")]
    out = []
    for y in range(SE.school_year_of(today), SE.school_year_of(first) - 1, -1):
        p = SE.period(y, "xc")
        if p["start"] <= today:
            out.append(p)
    return out


MILE = A.METERS_PER_MILE
Meet = tuple  # (date, meet name)


def meet_key(m: Meet) -> str:
    return f"{m[0].isoformat()}|{m[1]}"


def parse_meet(text: str) -> Meet | None:
    try:
        d, name = str(text or "").split("|", 1)
        return (dt.date.fromisoformat(d), name)
    except ValueError:
        return None


def season_meets(db: Session, season: dict, team: str, today: dt.date) -> list[Meet]:
    """The team's meets in the season, in order: (date, meet)."""
    return [tuple(x) for x in db.execute(
        select(RaceResult.date, RaceResult.meet).join(Athlete, Athlete.id == RaceResult.athlete_id)
        .where(Athlete.gender == team, RaceResult.date >= season["start"], RaceResult.date <= min(season["end"], today))
        .group_by(RaceResult.date, RaceResult.meet).order_by(RaceResult.date, RaceResult.meet)).all()]


def awards(db: Session, season: dict, team: str, today: dt.date, span: tuple[Meet | None, Meet | None] = (None, None)) -> dict:
    """{class key: {"label", "meets", "need", "eligible", "mvp": [...], "improved": [...], "span": (from, to),
    "missed_span": n, "short": [...]}}. span: the two meets most improved compares (None: the class's first or last)."""
    start, end = season["start"], min(season["end"], today)
    rows = db.execute(select(RaceResult, Athlete).join(Athlete, Athlete.id == RaceResult.athlete_id)
                      .where(Athlete.gender == team, RaceResult.date >= start, RaceResult.date <= end)
                      .order_by(RaceResult.date, RaceResult.id)).all()
    adj = adjusted_times(db, [r for r, _ in rows])
    by_athlete: dict[int, dict] = {}
    for r, a in rows:
        g = grade_in(a, start, today)
        e = by_athlete.setdefault(a.id, {"a": a, "grade": g, "races": []})
        t = adj.get(r.id) or r.time_s
        e["races"].append({"r": r, "meet": (r.date, r.meet), "adj": t,
                           "pace": t / (r.distance_m / MILE) if r.distance_m and t else None})
    out = {}
    for key, label, grades in CLASSES:
        people = [e for e in by_athlete.values() if e["grade"] in grades]
        meets = sorted({x["meet"] for e in people for x in e["races"]})
        need = -(-len(meets) * MIN_SHARE // 1) if meets else 0          # 80%, rounded up
        need = int(need)
        eligible, short = [], []
        for e in people:
            ran = len({x["meet"] for x in e["races"]})
            e = {**e, "ran": ran}
            (eligible if ran >= need else short).append(e)
        mvp = []
        for e in eligible:
            placed = [x for x in e["races"] if x["r"].place]
            if not placed:
                continue
            mvp.append({"a": e["a"], "grade": e["grade"], "ran": e["ran"], "avg_place": statistics.fmean(x["r"].place for x in placed),
                        "placed": len(placed), "best": min(x["r"].place for x in placed),
                        "avg_adj": statistics.fmean(x["adj"] for x in placed), "races": e["races"]})
        mvp.sort(key=lambda m: (m["avg_place"], -m["placed"], m["avg_adj"]))
        # Most improved: adjusted mile pace at one meet against another
        pick = lambda chosen, default: (chosen if chosen in meets else None) if chosen else default
        m_from, m_to = pick(span[0], meets[0] if meets else None), pick(span[1], meets[-1] if meets else None)
        improved, missed_span = [], 0
        if m_from and m_to and m_from < m_to:
            for e in eligible:
                pace_at = lambda m: min((x for x in e["races"] if x["meet"] == m and x["pace"]), key=lambda x: x["pace"], default=None)
                f, t = pace_at(m_from), pace_at(m_to)
                if not f or not t:
                    missed_span += 1
                    continue
                gain = f["pace"] - t["pace"]
                if gain <= 0:
                    continue
                improved.append({"a": e["a"], "grade": e["grade"], "ran": e["ran"], "from": f, "to": t, "gain": gain,
                                 "pct": 100 * gain / f["pace"], "same_distance": abs(f["r"].distance_m - t["r"].distance_m) < 50})
        improved.sort(key=lambda m: -m["gain"])     # most seconds a mile faster
        out[key] = {"label": label, "meets": len(meets), "need": need, "eligible": len(eligible),
                    "mvp": mvp[:TOP], "improved": improved[:TOP], "span": (m_from, m_to), "missed_span": missed_span,
                    "all_meets": meets,
                    "short": sorted(short, key=lambda e: (-e["ran"], e["a"].last_name))}
    return out

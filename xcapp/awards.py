"""Season awards: each class's top 5 scorers (MVP) and top 5 most improved, from the season's race results.

- The window: the coach picks a first and a last meet (by default each class's first and last of the season). Those
  meets and every race between them count, for both awards and for who qualifies.
- Classes: Freshmen (9th grade), Sophomores (10th) and Varsity (11th and 12th), for the Boys or the Girls, by the grade
  each athlete was in that season.
- Who counts: athletes who ran at least 80% (the coach can change it) of the meets their class raced in the window (a meet where at least one
  athlete of that class, same team, ran). Freshmen don't run sectionals, so they aren't held to meets they couldn't enter.
- MVP: in each race, the share of the field the athlete beat (finishers behind them ÷ everyone who finished, all teams;
  the finisher count is entered with the race), times the race level's weight (Varsity 1.00, JV 0.90, Frosh/Soph 0.80 by
  default; the coach can change them), so a freshman racing up to varsity gets credit for the stronger field. The MVP
  score is the average over every race in the window with a place and a finisher count; ties go to the faster average
  adjusted time. Without any finisher counts in the window, it falls back to the lowest average place.
- Most improved: every race in the window as an adjusted mile pace (the time adjusted for the course and weather, as a
  5K: a 5K's own pace; another distance's 5K equivalent by VDOT, so a 2-mile race and a 5K compare fairly), and a
  best-fit line through them over the dates. The improvement is how much faster the line is at the athlete's last race
  than at their first, as a share of the first pace (seconds a mile shown too): the trend over all the races, not one
  good or bad day at either end, and fair to faster runners, for whom each second is harder to find.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import classyear as CY
from . import seasons as SE
from .courses import adjusted_times
from .models import Athlete, RaceResult, RaceTeamResult

CLASSES = (("freshmen", "Freshmen", (9,)), ("sophomores", "Sophomores", (10,)), ("varsity", "Varsity", (11, 12)))
MIN_SHARE = 0.8          # the default share of the class's meets an athlete must run; the coach can change it
SHARE_KEY = "awards_min_share"
TOP = 5
LEVELS = ("Varsity", "JV", "Frosh/Soph", "Other")
DEFAULT_WEIGHTS = {"Varsity": 1.0, "JV": 0.9, "Frosh/Soph": 0.8, "Other": 1.0}
WEIGHTS_KEY = "mvp_level_weights"


def level_of(race: str) -> str:
    """The race's level from its division name: 'Boys Varsity Large School' → Varsity, 'Frosh/Soph' → Frosh/Soph."""
    t = (race or "").lower()
    if re.search(r"\bjv\b|junior varsity", t):
        return "JV"
    if re.search(r"frosh|fresh|soph|\bf/s\b|\bfs\b|novice|\b9th|\b10th", t):
        return "Frosh/Soph"
    if "varsity" in t:
        return "Varsity"
    return "Other"


def weights(db: Session) -> dict[str, float]:
    try:
        saved = json.loads(A.get_setting(db, WEIGHTS_KEY, "{}") or "{}")
    except ValueError:
        saved = {}
    return {k: float(saved.get(k, v)) for k, v in DEFAULT_WEIGHTS.items()}


def save_weights(db: Session, values: dict[str, float]) -> None:
    A.set_setting(db, WEIGHTS_KEY, json.dumps(values, separators=(",", ":")))


def min_share(db: Session) -> float:
    """The share of the class's meets in the window an athlete must run to count (0.5-1.0; 80% by default)."""
    try:
        v = float(A.get_setting(db, SHARE_KEY, str(MIN_SHARE)))
    except ValueError:
        return MIN_SHARE
    return v if 0.5 <= v <= 1.0 else MIN_SHARE


def field_share(place: int | None, finishers: int | None) -> float | None:
    """Share of the field beaten, 0-100: 5th of 200 beat 97.5%."""
    if not place or not finishers or place > finishers:
        return None
    return 100 * (finishers - place) / finishers


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


def trend(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares line through (day, pace) points: (slope a day, intercept), or None with fewer than 2 days."""
    if len({x for x, _ in points}) < 2:
        return None
    mx, my = statistics.fmean(x for x, _ in points), statistics.fmean(y for _, y in points)
    slope = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return slope, my - slope * mx


def awards(db: Session, season: dict, team: str, today: dt.date, span: tuple[Meet | None, Meet | None] = (None, None)) -> dict:
    """{class key: {"label", "meets", "need", "eligible", "mvp": [...], "improved": [...], "span": (from, to), "short": [...]}}.
    span: the first and last meet of the window (None: the class's first or last of the season)."""
    start, end = season["start"], min(season["end"], today)
    rows = db.execute(select(RaceResult, Athlete).join(Athlete, Athlete.id == RaceResult.athlete_id)
                      .where(Athlete.gender == team, RaceResult.date >= start, RaceResult.date <= end)
                      .order_by(RaceResult.date, RaceResult.id)).all()
    adj = adjusted_times(db, [r for r, _ in rows])
    fields = {(t.meet, t.date, t.race): t.finishers for t in db.scalars(select(RaceTeamResult).where(
        RaceTeamResult.date >= start, RaceTeamResult.date <= end, RaceTeamResult.finishers.is_not(None))).all()}
    wts = weights(db)
    share_needed = min_share(db)
    by_athlete: dict[int, dict] = {}
    for r, a in rows:
        g = grade_in(a, start, today)
        e = by_athlete.setdefault(a.id, {"a": a, "grade": g, "races": []})
        t = adj.get(r.id) or r.time_s
        pace = pace5k = None
        if r.distance_m and t:
            pace = t / (r.distance_m / MILE)
            pace5k = pace if abs(r.distance_m - 5000) < 50 else A.equivalent_time(5000, A.vdot(r.distance_m, t)) / (5000 / MILE)
        lvl, fin = level_of(r.race), fields.get((r.meet, r.date, r.race))
        share = field_share(r.place, fin)
        e["races"].append({"r": r, "meet": (r.date, r.meet), "adj": t, "pace": pace, "pace5k": pace5k, "level": lvl,
                           "finishers": fin, "share": share, "score": share * wts[lvl] if share is not None else None})
    out = {}
    for key, label, grades in CLASSES:
        people = [e for e in by_athlete.values() if e["grade"] in grades]
        season_meets_c = sorted({x["meet"] for e in people for x in e["races"]})
        pick = lambda chosen, default: (chosen if chosen in season_meets_c else None) if chosen else default
        m_from = pick(span[0], season_meets_c[0] if season_meets_c else None)
        m_to = pick(span[1], season_meets_c[-1] if season_meets_c else None)
        entry = {"label": label, "meets": 0, "need": 0, "eligible": 0, "mvp": [], "improved": [], "span": (m_from, m_to),
                 "by_field": False, "missing": [],
                 "short": [], "raced": bool(season_meets_c), "ok_span": bool(m_from and m_to and m_from <= m_to)}
        out[key] = entry
        if not entry["ok_span"]:
            continue
        # Every race in the window
        people = [{**e, "races": [x for x in e["races"] if m_from <= x["meet"] <= m_to]} for e in people]
        people = [e for e in people if e["races"]]
        meets = sorted({x["meet"] for e in people for x in e["races"]})
        need = int(-(-len(meets) * share_needed // 1)) if meets else 0      # the coach's share (80% at first), rounded up
        eligible, short = [], []
        for e in people:
            e["ran"] = len({x["meet"] for x in e["races"]})
            (eligible if e["ran"] >= need else short).append(e)
        # MVP: share of the field beaten × the race level's weight, averaged; by average place without finisher counts
        races_in = {(x["r"].meet, x["r"].date, x["r"].race): x for e in eligible for x in e["races"] if x["r"].place}
        missing = sorted({(k[1], k[0], k[2]) for k, x in races_in.items() if x["finishers"] is None})
        by_field = len(missing) < len(races_in)
        mvp = []
        for e in eligible:
            placed = [x for x in e["races"] if x["r"].place]
            scored = [x for x in placed if x["score"] is not None]
            if not placed or (by_field and not scored):
                continue
            best = max(scored, key=lambda x: x["score"]) if scored else min(placed, key=lambda x: x["r"].place)
            mvp.append({"a": e["a"], "grade": e["grade"], "ran": e["ran"], "avg_place": statistics.fmean(x["r"].place for x in placed),
                        "placed": len(placed), "best": min(x["r"].place for x in placed), "best_race": best,
                        "score": statistics.fmean(x["score"] for x in scored) if scored else None, "scored": len(scored),
                        "avg_adj": statistics.fmean(x["adj"] for x in placed), "races": e["races"],
                        "levels": sorted({x["level"] for x in placed}, key=LEVELS.index)})
        if by_field:
            mvp.sort(key=lambda m: (-m["score"], m["avg_adj"]))
        else:
            mvp.sort(key=lambda m: (m["avg_place"], -m["placed"], m["avg_adj"]))
        # Most improved: the trend of adjusted mile pace through every race in the window
        d0, d1 = m_from[0], m_to[0]
        improved = []
        for e in eligible:
            rs = [x for x in e["races"] if x["pace5k"]]
            line = trend([((x["r"].date - d0).days, x["pace5k"]) for x in rs])
            if not line:
                continue
            slope, icept = line
            first, last = rs[0]["r"].date, rs[-1]["r"].date
            p_from, p_to = icept + slope * (first - d0).days, icept + slope * (last - d0).days
            gain = p_from - p_to
            if gain <= 0:
                continue
            improved.append({"a": e["a"], "grade": e["grade"], "ran": e["ran"], "n": len(rs), "gain": gain,
                             "pct": 100 * gain / p_from, "p_from": p_from, "p_to": p_to, "races": rs,
                             "mixed": len({round(x["r"].distance_m, -2) for x in rs}) > 1})
        improved.sort(key=lambda m: -m["pct"])      # the biggest share faster along the trend: fair to fast and slower runners
        entry.update(meets=len(meets), need=need, eligible=len(eligible), mvp=mvp[:TOP], improved=improved[:TOP],
                     by_field=by_field, missing=missing,
                     short=sorted(short, key=lambda e: (-e["ran"], e["a"].last_name)))
    return out

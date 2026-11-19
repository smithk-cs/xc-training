"""Race-day pacing plans: a predicted time for an athlete on a meet's course, and mile-by-mile target splits.

- The time: the athlete's current VDOT gives the time for the race distance on a flat track at 55°F; the course
  (courses.course_seconds: hills, surface, turns) and the forecast heat, humidity and altitude (courses.conditions_pct)
  slow it, the reverse of how race results are adjusted to a track, so predictions and adjusted times agree.
- The splits: the course's elevation profile (from its flyover) spreads that time over the miles, slower uphill and
  a little faster downhill, adding up to the same total. Without a profile, even splits.
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import courses as CS
from .models import Athlete, CourseFlyover, MeetDivision, MeetEntry

UP = 0.033      # each 1% of uphill grade costs about 3.3% more time over that stretch
DOWN = 0.018    # ... and each 1% downhill gives back a bit over half of that
PLAN_DAYS = 21  # meets this close get a plan on the athlete's page


def race_meters(meet: MeetEntry, division: MeetDivision | None) -> tuple[float | None, str]:
    """The race distance in meters and its label: the athlete's race, else the course's distance."""
    if division is not None and division.distance in CS.DISTANCES:
        return CS.DISTANCES[division.distance], division.distance
    c = meet.course
    if c is not None and c.distance_m:
        return c.distance_m, CS.distance_label(c.distance_m)
    return None, ""


def profile(db: Session, meet: MeetEntry, label: str) -> tuple[list, list[tuple[float, str]]]:
    """The course's elevation profile for that distance ([[mile, feet], ...]) and its notes ([(mile, text)]), from
    its flyover; empty when it has none."""
    from .weather import course_for
    c = course_for(db, meet)
    if c is None:
        return [], []
    fly = next(iter(CS.race_flyovers(c, label)), None) if label else None
    if fly is None:
        fly = db.scalar(select(CourseFlyover).where(CourseFlyover.course_id == c.id).order_by(CourseFlyover.id))
    if fly is None:
        return [], []
    try:
        E = json.loads(fly.data or "{}").get("E") or []
    except ValueError:
        E = []
    notes = []
    for line in (fly.notes or "").splitlines():
        head, _, text = line.strip().partition(" ")
        try:
            notes.append((float(head), text.strip()))
        except ValueError:
            continue
    return E, notes


def split_costs(E: list, miles: float) -> list[tuple[float, float, float, float]]:
    """For each mile (the last one partial): (start mile, end mile, effort cost, climb in feet). The cost is the
    distance weighted for its grade, so the splits share the total time by it."""
    edges = [0.0]
    while edges[-1] + 1 < miles - 0.05:
        edges.append(edges[-1] + 1)
    edges.append(miles)
    good = len(E) >= 2 and E[-1][0] > 0
    scale = miles / E[-1][0] if good else 1.0           # the profile's length stretched to the race distance
    out = []
    for lo, hi in zip(edges, edges[1:]):
        if not good:
            out.append((lo, hi, hi - lo, 0.0))
            continue
        cost = climb = 0.0
        for (m0, f0), (m1, f1) in zip(E, E[1:]):
            a, b = max(lo, m0 * scale), min(hi, m1 * scale)
            if b <= a:
                continue
            seg_m = (m1 - m0) * scale
            rise = (f1 - f0) * (b - a) / seg_m if seg_m else 0.0
            grade = rise / ((b - a) * 5280) * 100 if b > a else 0.0     # percent
            cost += (b - a) * (1 + (UP * grade if grade > 0 else DOWN * grade))
            climb += max(0.0, rise)
        out.append((lo, hi, max(cost, 0.5 * (hi - lo)), climb))
    return out


def plan(db: Session, a: Athlete, meet: MeetEntry, today: dt.date, vdot: float | None = None,
         division: MeetDivision | None = None) -> dict | None:
    """The athlete's pacing plan for a meet, or None without a VDOT or a distance:
    {"vdot", "label", "miles", "total", "pace", "track", "explain", "splits": [{"mile", "to", "time", "cum", "pace",
    "climb", "notes"}], "even": no profile, "weather"}."""
    if division is None:
        division = next((d for d in meet.divisions if any(e.athlete_id == a.id for e in d.entries)), None)
    if vdot is None:
        vdot = A.abilities(db, today).get(a.id, {}).get("vdot")
    meters, label = race_meters(meet, division)
    if not vdot or not meters:
        return None
    from .weather import course_for
    course = course_for(db, meet)
    f = meet.forecast
    cond = SimpleNamespace(temp_f=f.temp_f, dew_point_f=f.dew_point_f, footing="") if f is not None and f.temp_f is not None else None
    miles = meters / CS.METERS_PER_MILE
    track = A.equivalent_time(meters, vdot)
    pace = (track / miles + CS.course_seconds(course)) * (1 + CS.conditions_pct(cond, course) / 100)
    total = pace * miles
    E, notes = profile(db, meet, label)
    costs = split_costs(E, miles)
    weight = sum(c for _, _, c, _ in costs) or 1.0
    splits, cum = [], 0.0
    for i, (lo, hi, cost, climb) in enumerate(costs, 1):
        t = total * cost / weight
        cum += t
        splits.append({"mile": i, "from": lo, "to": hi, "time": t, "cum": cum, "pace": t / (hi - lo), "climb": round(climb),
                       "notes": [text for m, text in notes if lo <= m < hi or (i == len(costs) and m >= hi)]})
    return {"vdot": vdot, "label": label or CS.distance_label(meters), "miles": miles, "total": total, "pace": pace,
            "track": track, "explain": CS.explain(course, cond), "splits": splits, "even": not E, "course": course,
            "weather": f if cond else None, "division": division}


def tips(p: dict) -> list[str]:
    """A few plain race tips from the plan: the first mile, and the hardest climb."""
    out = []
    first = p["splits"][0]
    out.append(f"Start controlled: through the first mile in about {A.fmt_secs(first['time'])}, not faster. "
               "Most races are lost by going out too fast.")
    hill = max(p["splits"], key=lambda s: s["climb"] / max(0.1, s["to"] - s["from"]))
    if hill["climb"] >= 25:
        out.append(f"Mile {hill['mile']} climbs about {hill['climb']} ft: run it by effort, not pace, and get back on "
                   "pace at the top.")
    out.append("Last mile: if you feel good with half a mile to go, that's the time to move up.")
    return out

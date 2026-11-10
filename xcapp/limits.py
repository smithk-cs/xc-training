"""Daniels' limits on how much of one session can be quality running, from each athlete's weekly miles goal
(Daniels' Running Formula):
- Threshold (T): up to 10% of the week's miles in one session;
- Interval (I): up to 8% of the week's miles, and never more than 10 km;
- Repetition (R): up to 5% of the week's miles, and never more than 8 km;
- Moderate (M, marathon pace): up to 20% of the week's miles, and never more than 18 miles;
- a long run (a workout that's all easy running): up to 25% of the week's miles, and never more than 150 minutes.
check() lists, for each posted workout, the athletes it asks too much of, grouped by training group and weekly goal,
with how much would fit (such as "at most 4 × 1K").
"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .models import Athlete, PlannedWorkout, TrainingGroup

MILE = A.METERS_PER_MILE
# zone: (share of the weekly miles goal, cap in miles or None)
LIMITS = {"Threshold": (0.10, None), "Interval": (0.08, 10000 / MILE), "Repetition": (0.05, 8000 / MILE),
          "Moderate": (0.20, 18.0)}
LONG_SHARE, LONG_MAX_MIN = 0.25, 150
ORDER = ("Repetition", "Interval", "Threshold", "Moderate")


def zone_miles(parts: list[dict], v: float | None) -> dict[str, float]:
    """Miles run at each pace zone in a workout: distances as given, timed parts at the athlete's pace for that zone
    (or a mile every 7 minutes without a VDOT). The easy run that finishes the day isn't counted."""
    out: dict[str, float] = {}
    for p in parts:
        if p.get("fill"):
            continue
        n = (p.get("sets") or 1) * (p.get("reps") or 1)
        if p.get("mins"):
            pct = A.WORKOUT_ZONES.get(p["zone"])
            pace = A.pace_at(v, pct) if v and pct else A.FILL_MIN_PER_MILE * 60
            each = p["mins"] * 60 / pace
        else:
            each = (p.get("dist") or 0) / MILE
        out[p["zone"]] = out.get(p["zone"], 0.0) + each * n
    return out


def _fit(parts: list[dict], zone: str, limit_mi: float) -> str:
    """How much of the zone's running fits the limit, in the workout's own terms: 'at most 4 × 1K' for one part of
    reps at a distance, otherwise 'about 2.3 mi'."""
    ps = [p for p in parts if p.get("zone") == zone and not p.get("fill")]
    if len(ps) == 1 and not ps[0].get("mins") and (ps[0].get("sets") or 1) == 1 and (ps[0].get("reps") or 1) > 1:
        p = ps[0]
        n = max(1, math.floor(limit_mi * MILE / p["dist"] + 1e-9))
        return "at most " + A.part_label({**p, "reps": n})
    return f"about {limit_mi:.1f} mi"


def check_one(pw: PlannedWorkout, people: list[dict]) -> list[dict]:
    """The limits a workout goes over, each with the athletes affected, grouped by group and weekly goal:
    [{"zone", "rule", "groups": [{"group", "goal", "limit", "has", "fit", "names"}], "count"}]."""
    parts = pw.part_list
    if not parts:
        return []
    fast = {p["zone"] for p in parts if not p.get("fill")}
    is_long = fast and fast <= {"Easy"}
    found: dict[str, dict] = {}
    for x in people:
        a, v, g, goal = x["a"], x["vdot"], x["group"], x["goal"]
        if not goal or not pw.applies_to(a.group, g, gender=a.gender):
            continue
        miles = zone_miles(parts, v)
        checks = []
        for zone in ORDER:
            has = miles.get(zone, 0)
            share, cap = LIMITS[zone]
            limit = min(goal * share, cap) if cap else goal * share
            if has > limit + 0.05:
                rule = f"{int(share * 100)}% of the week's miles" + (f", at most {cap:.1f} mi" if cap else "")
                checks.append((zone, rule, limit, has, _fit(parts, zone, limit)))
        if is_long:
            easy_pace = A.pace_at(v, A.WORKOUT_ZONES["Easy"]) if v else A.FILL_MIN_PER_MILE * 60
            has = miles.get("Easy", 0)
            limit = min(goal * LONG_SHARE, LONG_MAX_MIN * 60 / easy_pace)
            if has > limit + 0.05:
                checks.append(("Long run", f"{int(LONG_SHARE * 100)}% of the week's miles, at most {LONG_MAX_MIN} minutes",
                               limit, has, f"about {limit:.1f} mi"))
        for zone, rule, limit, has, fit in checks:
            z = found.setdefault(zone, {"zone": zone, "rule": rule, "groups": {}, "count": 0})
            key = (g or "No group", round(goal, 1))
            row = z["groups"].setdefault(key, {"group": key[0], "goal": key[1], "limit": limit, "has": has, "fit": fit, "names": []})
            row["names"].append(a.name)
            z["count"] += 1
    out = []
    for z in found.values():
        z["groups"] = sorted(z["groups"].values(), key=lambda r: -r["goal"])
        out.append(z)
    return out


def people(db: Session, on) -> list[dict]:
    """Every athlete on the team with their VDOT, group and weekly miles goal (as the Week page counts it)."""
    ab = A.abilities(db, on)
    groups = {g.name: g for g in db.scalars(select(TrainingGroup)).all()}
    out = []
    for a in db.scalars(select(Athlete).where(A.on_team()).order_by(Athlete.last_name, Athlete.first_name)).all():
        me = ab.get(a.id, {})
        g = me.get("group") or ""
        out.append({"a": a, "vdot": me.get("vdot"), "group": g,
                    "goal": A.athlete_miles_goal(groups.get(g), a.gender, a.experience)})
    return out


def check(db: Session, workouts: list[PlannedWorkout]) -> dict[int, list[dict]]:
    """workout id -> the limits it goes over (only workouts that go over any)."""
    by_day: dict = {}
    out = {}
    for w in workouts:
        if w.date not in by_day:
            by_day[w.date] = people(db, w.date)
        over = check_one(w, by_day[w.date])
        if over:
            out[w.id] = over
    return out


def summary(over: list[dict]) -> str:
    """One line for a message after saving: 'Over Daniels' Interval limit for 5 athletes; Threshold limit for 2'."""
    return "Over Daniels' " + "; ".join(f"{z['zone']} limit for {z['count']} athlete{'s' if z['count'] != 1 else ''}" for z in over)

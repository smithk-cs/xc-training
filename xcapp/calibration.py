"""How hard each race and course really was, measured against the runners' own track benchmarks.

Method (docs/plans/conditions-and-courses.md, section 7):
- Each athlete's benchmark is their best VDOT (Daniels & Gilbert's equations) from the track meets a coach ticks, such
  as a 3200. From that VDOT the same equations predict their time at any race distance on a flat track at 55°F or
  cooler, at sea level: the baseline the app's adjusted times aim at.
- For each cross country result: difficulty % = race time ÷ predicted track time − 1. The app's own estimate is
  race time ÷ adjusted time − 1 (course + heat, humidity, altitude, footing). The gap between them is what the app's
  course and weather settings don't yet explain.
- Single results are noisy: athletes race faster or slower than their benchmark on any day, and fitness changes
  between the benchmark and the race. So everything is summarized by the median (robust to off days), results more
  than OUTLIER_PCT % off are set aside, the middle-50% spread is shown, and a course needs MIN_RESULTS results before
  a new setting is suggested. Suggestions are blended with the current setting: w = n / (n + BLEND_K).
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import courses as CS
from .models import Athlete, Course, MeetConditions, RaceResult, Result

OUTLIER_PCT = 10.0     # a result this far from the athlete's benchmark is set aside (off day, wrong distance or course)
MIN_RESULTS = 5        # results needed on a course before a new course setting is suggested
BLEND_K = 10           # suggestion = current + n/(n+K) × (measured − current)
TRACK_WORDS = ("track", "3200", "1600", "800", "3000", "1500", "mile run")
SETTING_KEY = "calibration_benchmarks"


def predicted_time(v: float, meters: float) -> float:
    """The time at `meters` that gives VDOT v (Daniels & Gilbert), found by bisection."""
    lo, hi = 30.0, 7200.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if A.vdot(meters, mid) > v:   # too fast for this VDOT: more time
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bench_key(r: Result) -> str:
    return f"{r.date.isoformat()}|{(r.note or r.course or r.event)}"


def bench_choices(db: Session) -> list[dict]:
    """Track benchmark meets to choose from: mile to 2-mile results, grouped by date and meet."""
    rows = db.scalars(select(Result).where(Result.event != "5K").order_by(Result.date.desc())).all()
    out: dict[str, dict] = {}
    for r in rows:
        k = bench_key(r)
        x = out.setdefault(k, {"key": k, "date": r.date, "label": (r.note or r.course or r.event), "event": r.event, "n": 0,
                               "track": r.kind == "track" or any(w in f"{r.note} {r.course}".lower() for w in TRACK_WORDS)})
        x["n"] += 1
    return list(out.values())


def chosen(db: Session) -> list[str]:
    raw = A.get_setting(db, SETTING_KEY, "")
    if raw:
        try:
            return list(json.loads(raw))
        except ValueError:
            pass
    return [c["key"] for c in bench_choices(db) if c["track"]]


def benchmarks(db: Session, keys: list[str]) -> dict[int, dict]:
    """athlete id -> {vdot, label, date, time, event}: their best from the chosen track meets."""
    keep = set(keys)
    out: dict[int, dict] = {}
    for r in db.scalars(select(Result).where(Result.event != "5K")).all():
        if bench_key(r) not in keep or r.event not in A.EVENTS:
            continue
        v = A.vdot(A.EVENTS[r.event], r.time_s)
        if r.athlete_id not in out or v > out[r.athlete_id]["vdot"]:
            out[r.athlete_id] = {"vdot": v, "label": r.note or r.course, "date": r.date, "time": r.time_s, "event": r.event}
    return out


def _quartiles(xs: list[float]) -> tuple[float, float]:
    if len(xs) < 4:
        return (min(xs), max(xs)) if xs else (math.nan, math.nan)
    q = statistics.quantiles(xs, n=4)
    return q[0], q[2]


@dataclass
class Report:
    rows: list[dict]      # every result compared
    races: list[dict]     # per race (meet, date, division)
    courses: list[dict]   # per course, with a suggested setting
    benches: dict[int, dict]


def report(db: Session, season: str, keys: list[str]) -> Report:
    benches = benchmarks(db, keys)
    from .weather import result_course
    found: dict[tuple, Course | None] = {}
    conds = {(c.meet, c.date): c for c in db.scalars(select(MeetConditions)).all()}
    athletes = {a.id: a for a in db.scalars(select(Athlete)).all()}
    rows = []
    for r in db.scalars(select(RaceResult).order_by(RaceResult.date, RaceResult.meet)).all():
        b = benches.get(r.athlete_id)
        a = athletes.get(r.athlete_id)
        if b is None or a is None or a.is_test or A.season_for(r.date) != season or not r.distance_m:
            continue
        cond = conds.get((r.meet, r.date))
        key = (r.meet, r.date, r.course)
        if key not in found:
            found[key] = result_course(db, r.meet, r.date, r.course, cond)
        course = found[key]
        pred = predicted_time(b["vdot"], r.distance_m)
        miles = r.distance_m / A.METERS_PER_MILE
        adjusted = CS.adjusted_time(r.time_s, r.distance_m, course, cond)
        cond_pct = CS.conditions_pct(cond, course)
        footing = CS.FOOTING.get(cond.footing if cond else "", 0)
        observed = 100 * (r.time_s / pred - 1)                     # how much slower than track fitness, all causes
        model = 100 * (r.time_s / adjusted - 1) if adjusted else None   # what the app's settings account for
        rows.append({
            "r": r, "athlete": a, "bench": b, "pred": pred, "adjusted": adjusted, "observed": observed, "model": model,
            "gap": observed - model if model is not None else None,
            # the course seconds per mile that would make the adjusted time equal the predicted track time
            "implied_s": (r.time_s / miles) / (1 + cond_pct / 100) - footing - pred / miles,
            "course": course, "cond_pct": cond_pct, "days": (r.date - b["date"]).days,
            "outlier": abs(observed - (model or 0)) > OUTLIER_PCT if model is not None else abs(observed) > 3 * OUTLIER_PCT,
        })
    races: dict[tuple, list] = {}
    for x in rows:
        races.setdefault((x["r"].date, x["r"].meet, x["r"].race, x["r"].distance_label), []).append(x)
    race_list = []
    for (d, meet, race, dist), xs in sorted(races.items(), key=lambda kv: kv[0][0], reverse=True):
        keep = [x for x in xs if not x["outlier"]] or xs
        obs = [x["observed"] for x in keep]
        models = [x["model"] for x in keep if x["model"] is not None]
        q1, q3 = _quartiles(obs)
        race_list.append({"date": d, "meet": meet, "race": race, "distance": dist, "n": len(keep), "set_aside": len(xs) - len(keep),
                          "observed": statistics.median(obs), "model": statistics.median(models) if models else None,
                          "q1": q1, "q3": q3, "course": keep[0]["course"], "cond_pct": keep[0]["cond_pct"]})
    course_list = []
    by_course: dict[int, list] = {}
    for x in rows:
        if x["course"] is not None and not x["outlier"]:
            by_course.setdefault(x["course"].id, []).append(x)
    for cid, xs in by_course.items():
        c = xs[0]["course"]
        implied = statistics.median(x["implied_s"] for x in xs)
        current = CS.course_seconds(c)
        n = len(xs)
        w = n / (n + BLEND_K)
        q1, q3 = _quartiles([x["implied_s"] for x in xs])
        course_list.append({"course": c, "n": n, "races": len({(x["r"].date, x["r"].meet) for x in xs}), "implied": implied,
                            "current": current, "suggested": round(current + w * (implied - current)) if n >= MIN_RESULTS else None,
                            "weight": w, "q1": q1, "q3": q3,
                            "gap": statistics.median(x["gap"] for x in xs if x["gap"] is not None) if any(x["gap"] is not None for x in xs) else None})
    course_list.sort(key=lambda c: -c["n"])
    return Report(rows=rows, races=race_list, courses=course_list, benches=benches)

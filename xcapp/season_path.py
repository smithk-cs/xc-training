"""The season path: each week of a season, the plan (phase, share of peak miles) against what happened, so the coach
can see the team staying on course from Phase I to IV, in mileage, points, intensity and form work.

For each week:
- Mileage and points: the team's average share of the weekly miles goal and of the points target, and the weekly
  score, from each athlete's saved finished week (medals.ensure_weeks; the Week page's numbers).
- Intensity: the share of the team's running time at threshold pace or faster (T, I, R; strides count), from each
  run's minutes per pace zone, against the phase's guide range. Daniels keeps most running easy; the quality share
  grows from Phase I to III and eases in IV. The ranges are guides for high school distance runners, not rules.
- Quality posted: the paces of the team workouts posted that week, against the paces the phase calls for (for
  example, interval work in Phase I is flagged).
- Form: each phase starts with filming (a fresh baseline in Phase I, a re-film after); Phase III adds a fatigue check
  (filmed tired at the end of an interval session). Each phase block shows how many athletes on the team were filmed.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import form_plan as FP
from . import season_plan as SP
from .models import Activity, Athlete, FormAnalysis, PlannedWorkout, WeekResult

QUALITY = ("Threshold", "Interval", "Repetition")
# Share of running time at T pace or faster: (low, high) guide for each phase
INTENSITY = {1: (0, 5), 2: (5, 12), 3: (10, 20), 4: (8, 16)}
# Paces of the posted quality sessions each phase calls for (strides are fine in every phase)
PACES = {1: set(), 2: {"Repetition", "Threshold"}, 3: {"Interval", "Threshold", "Repetition"}, 4: {"Threshold", "Repetition", "Interval"}}
MAIN = {1: None, 2: "Repetition", 3: "Interval", 4: "Threshold"}
FORM = {
    1: ("Film every athlete fresh (the baseline)", "Drills (A-skips, B-skips, high knees) before every run; strides twice a week; hip and core strength. Work on each athlete's top focus from their analysis."),
    2: ("Re-film fresh at the same pace", "Repetition work is the time to change mechanics: fast, relaxed and short. One cue per athlete, from their focus."),
    3: ("Fatigue check: film tired at the end of an interval session", "Form that breaks down when tired needs strength and threshold work, and the cue in the last reps, not new drills."),
    4: ("Re-film fresh before the championships (optional)", "Hold what's there: no big changes. Cues in race warm-ups and on strides."),
}


def _pct(a: float, b: float) -> float | None:
    return 100 * a / b if b else None


def path(db: Session, season: dict, today: dt.date) -> dict:
    """{"weeks": [{monday, row, plan, done, miles_pct, points_pct, score, quality_pct, guide, posted, off, main_ok,
    state}], "blocks": [{phase, start, end, weeks, filmed, tired, team, todo, how}], "team": n}."""
    from .medals import ensure_weeks
    weeks = SP.weeks(db, season)
    if not weeks:
        return {"weeks": [], "blocks": [], "team": 0}
    first, last = weeks[0]["monday"], weeks[-1]["monday"] + dt.timedelta(days=6)
    team = db.scalars(select(Athlete).where(A.on_team())).all()
    ids = [a.id for a in team]
    finished = [w["monday"] for w in weeks if w["monday"] + dt.timedelta(days=6) < today]
    saved = ensure_weeks(db, team, finished, today) if finished and team else {}
    by_week: dict[dt.date, list[WeekResult]] = {}
    for (aid, m), r in saved.items():
        if aid in ids:
            by_week.setdefault(m, []).append(r)
    # Time per pace zone, team-wide, per week
    zone_time: dict[dt.date, dict[str, float]] = {}
    for d, z in db.execute(select(Activity.local_date, Activity.zone_seconds).where(
            Activity.athlete_id.in_(ids), Activity.local_date >= first, Activity.local_date <= min(last, today),
            Activity.zone_seconds != "")).all() if ids else []:
        try:
            secs = json.loads(z or "{}")
        except ValueError:
            continue
        acc = zone_time.setdefault(A.week_monday(d), {})
        for k, v in secs.items():
            if k != "walk":
                acc[k] = acc.get(k, 0) + float(v or 0)
    posted: dict[dt.date, list[PlannedWorkout]] = {}
    for w in db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= first, PlannedWorkout.date <= last)).all():
        posted.setdefault(A.week_monday(w.date), []).append(w)
    out = []
    for w in weeks:
        m, row = w["monday"], w["row"]
        rs = [r for r in by_week.get(m, []) if r.goal_miles or r.target]
        miles = [100 * r.miles / r.goal_miles for r in rs if r.goal_miles]
        points = [100 * (r.points or 0) / r.target for r in rs if r.target]
        scores = [r.score for r in rs if r.score is not None]
        zt = zone_time.get(m, {})
        total = sum(zt.values())
        quality_pct = _pct(sum(zt.get(z, 0) for z in QUALITY), total) if total >= 3600 else None
        paces: dict[str, int] = {}
        for pw in posted.get(m, []):
            for p in pw.part_list:
                if p.get("zone") in QUALITY and not p.get("strides"):
                    paces[p["zone"]] = paces.get(p["zone"], 0) + 1
        phase = row.phase if row else None
        guide = INTENSITY.get(phase)
        off = sorted(z for z in paces if phase and z not in PACES[phase])
        main_ok = None if not phase or phase == 1 else (MAIN[phase] in paces if paces else False)
        future = m > today
        done = m + dt.timedelta(days=6) < today
        state = ""
        if phase and not future and quality_pct is not None and guide:
            state = "high" if quality_pct > guide[1] + 2 else "low" if phase > 1 and quality_pct < guide[0] - 2 else "ok"
        out.append({"monday": m, "row": row, "phase": phase, "future": future, "done": done, "now": m <= today <= m + dt.timedelta(days=6),
                    "miles_pct": statistics.fmean(miles) if miles and done else None,
                    "points_pct": statistics.fmean(points) if points and done else None,
                    "score": statistics.fmean(scores) if scores and done else None, "athletes": len(rs),
                    "quality_pct": quality_pct, "guide": guide, "intensity": state,
                    "posted": paces, "off": off, "main_ok": main_ok})
    # Phase blocks: consecutive weeks of one phase, with the form checkpoint and how many were filmed
    blocks = []
    for w in out:
        if w["phase"] is None:
            continue
        if blocks and blocks[-1]["phase"] == w["phase"] and blocks[-1]["end"] + dt.timedelta(days=1) == w["monday"]:
            blocks[-1]["end"] = w["monday"] + dt.timedelta(days=6)
            blocks[-1]["weeks"] += 1
        else:
            blocks.append({"phase": w["phase"], "start": w["monday"], "end": w["monday"] + dt.timedelta(days=6), "weeks": 1})
    fas = db.scalars(select(FormAnalysis).where(FormAnalysis.athlete_id.in_(ids))).all() if ids else []
    for b in blocks:
        inside = [f for f in fas if b["start"] <= FP.day_of(f) <= b["end"]]
        b["filmed"] = len({f.athlete_id for f in inside if (f.condition or "fresh") == "fresh"})
        b["tired"] = len({f.athlete_id for f in inside if f.condition == "tired"})
        b["team"] = len(ids)
        b["todo"], b["how"] = FORM[b["phase"]]
        b["started"] = b["start"] <= today
    return {"weeks": out, "blocks": blocks, "team": len(ids), "intensity": INTENSITY, "main": MAIN}


def year(db: Session, school_year: int) -> list[dict]:
    """The school year's seasons with each week's phase, for the year at a glance: [{"season", "weeks": [phase|None]}]."""
    from . import seasons as SE
    out = []
    for p in SE.year_periods(school_year):
        ws = SP.weeks(db, p)
        out.append({"season": p, "weeks": [(w["monday"], w["row"].phase if w["row"] else None) for w in ws]})
    return out

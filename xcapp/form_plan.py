"""Running-form analyses put to work in training (see form.py for the measures):

- Focus and drills: each athlete's latest analysis (the latest filmed fresh, if there is one) gives up to three things
  to work on, each with a cue and a drill. The Workouts page groups the team by focus, for drill stations.
- Progress: each athlete's analyses over time, and how far each measure has moved toward its range since the first.
- Re-film: Daniels' phases run about six weeks, so an athlete is due to be filmed again six weeks after their latest
  fresh clip (and anyone never filmed is listed too).
- Fatigue check: a clip filmed tired (at the end of a workout) against one filmed fresh within three weeks of it. A
  measure out of range both times is technique (drills and cues); in range fresh but out of range tired is fatigue
  (strength, threshold work and holding form late in workouts).
"""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import form as F
from .models import Athlete, FormAnalysis

REFILM_DAYS = 42      # about one Daniels phase
PAIR_DAYS = 21        # a tired clip is compared with a fresh one filmed within this many days
CONDITIONS = {"fresh": "Fresh", "tired": "Tired (end of a workout)"}
SPEC = {c[0]: c for c in F.CHECKS}


def metrics(fa: FormAnalysis) -> dict:
    try:
        return json.loads(fa.data or "{}").get("metrics") or {}
    except ValueError:
        return {}


def off_by(key: str, value) -> float | None:
    """How far a measure is outside its range (0 inside it), in its own units."""
    if value is None or key not in SPEC:
        return None
    lo, hi = SPEC[key][3]
    return 0.0 if lo <= value <= hi else (lo - value if value < lo else value - hi)


def day_of(fa: FormAnalysis) -> dt.date:
    return A.local_time(fa.created_at).date() if fa.created_at else A.team_today()


def _all(db: Session, athlete_ids=None) -> list[FormAnalysis]:
    q = select(FormAnalysis).order_by(FormAnalysis.created_at, FormAnalysis.id)
    if athlete_ids is not None:
        q = q.where(FormAnalysis.athlete_id.in_(list(athlete_ids)))
    return list(db.scalars(q).all())


def latest(db: Session, athlete_ids=None) -> dict[int, FormAnalysis]:
    """Each athlete's latest analysis filmed fresh, or their latest of any kind if none was."""
    out: dict[int, FormAnalysis] = {}
    for fa in _all(db, athlete_ids):
        cur = out.get(fa.athlete_id)
        if cur is None or (fa.condition or "fresh") == "fresh" or (cur.condition or "fresh") != "fresh":
            out[fa.athlete_id] = fa
    return out


def focus(fa: FormAnalysis | None) -> list[dict]:
    """Up to three things to work on, furthest out of range first: {key, state, title, summary, why, cue, drill}."""
    return F.report(metrics(fa), fa.pace_s)["focus"] if fa else []


def focus_groups(db: Session) -> list[dict]:
    """The team grouped by what to work on (from each athlete's latest analysis), most athletes first:
    [{"key", "state", "summary", "cue", "drill", "people": [Athlete]}]."""
    team = {a.id: a for a in db.scalars(select(Athlete).where(A.on_team())).all()}
    groups: dict[tuple, dict] = {}
    for aid, fa in latest(db, list(team)).items():
        for c in focus(fa):
            g = groups.setdefault((c["key"], c["state"]), {"key": c["key"], "state": c["state"], "summary": c["summary"],
                                                          "cue": c["cue"], "drill": c["drill"], "title": c["title"], "people": []})
            g["people"].append(team[aid])
    for g in groups.values():
        g["people"].sort(key=lambda a: (a.last_name, a.first_name))
    return sorted(groups.values(), key=lambda g: (-len(g["people"]), g["title"]))


def history(db: Session, athlete_id: int) -> dict:
    """{"rows": oldest first [{"fa", "day", "condition", "score", "cells": {key: {"value", "state"}}}],
    "moves": {key: "better" | "worse" | ""} from the first fresh analysis to the latest fresh one}."""
    rows = []
    for fa in _all(db, [athlete_id]):
        m = metrics(fa)
        rows.append({"fa": fa, "day": day_of(fa), "condition": fa.condition or "fresh", "score": fa.score,
                     "cells": {k: {"value": m.get(k), "state": F.rate(k, m.get(k))[0]} for k in SPEC}})
    fresh = [r for r in rows if r["condition"] == "fresh"]
    moves = {}
    if len(fresh) >= 2:
        first, last = fresh[0]["cells"], fresh[-1]["cells"]
        for k in SPEC:
            a, b = off_by(k, first[k]["value"]), off_by(k, last[k]["value"])
            moves[k] = "" if a is None or b is None or abs(a - b) < 1e-9 else ("better" if b < a else "worse")
    return {"rows": rows, "moves": moves, "checks": [(c[0], F.SHORT.get(c[0], c[1]), c[2]) for c in F.CHECKS]}


def due(db: Session, today: dt.date, days: int = REFILM_DAYS) -> dict:
    """Athletes on the team due to be filmed: {"stale": [(Athlete, last day)], oldest first, "never": [Athlete]}."""
    team = db.scalars(select(Athlete).where(A.on_team()).order_by(Athlete.last_name, Athlete.first_name)).all()
    last: dict[int, dt.date] = {}
    for fa in _all(db, [a.id for a in team]):
        if (fa.condition or "fresh") == "fresh":
            last[fa.athlete_id] = day_of(fa)
    stale = sorted([(a, last[a.id]) for a in team if a.id in last and (today - last[a.id]).days >= days], key=lambda x: x[1])
    return {"stale": stale, "never": [a for a in team if a.id not in last], "days": days}


def refilm_on(db: Session, athlete_id: int) -> dt.date | None:
    """When the athlete is next due to be filmed (six weeks after the latest fresh clip), or None if never filmed."""
    days = [day_of(fa) for fa in _all(db, [athlete_id]) if (fa.condition or "fresh") == "fresh"]
    return max(days) + dt.timedelta(days=REFILM_DAYS) if days else None


VERDICTS = {
    "technique": ("Technique", "Out of range fresh and tired: work on it with the drill and cue, at easy pace first."),
    "fatigue": ("Fatigue", "In range fresh, out of range tired: form holds until fatigue. Strength, threshold work, and the "
                           "cue in the last reps of workouts."),
    "ok": ("Holds", "In range fresh and tired."),
    "tired_only": ("Better tired", "Out of range fresh but in range tired, often a pace difference; film both at similar paces."),
}


def fatigue(db: Session, athlete_id: int, fa: FormAnalysis | None = None) -> dict | None:
    """A tired clip against the nearest fresh clip within three weeks: {"fresh", "tired", "rows": [{key, title, unit,
    fresh, tired, verdict, label, advice}], "counts"}. fa: the analysis being viewed (either of the pair); otherwise the
    athlete's latest tired clip."""
    rows = _all(db, [athlete_id])
    tired_all = [x for x in rows if x.condition == "tired"]
    fresh_all = [x for x in rows if (x.condition or "fresh") == "fresh"]
    if not tired_all or not fresh_all:
        return None
    near = lambda x, pool: min(pool, key=lambda y: abs((day_of(y) - day_of(x)).days), default=None)
    if fa is not None and fa.condition == "tired":
        tired = fa
    elif fa is not None:
        tired = near(fa, tired_all)
    else:
        tired = tired_all[-1]
    fresh = fa if fa is not None and fa.condition != "tired" else near(tired, fresh_all)
    if fresh is None or abs((day_of(fresh) - day_of(tired)).days) > PAIR_DAYS:
        return None
    mf, mt = metrics(fresh), metrics(tired)
    out = []
    for key, title, unit, *_ in F.CHECKS:
        a, b = mf.get(key), mt.get(key)
        if a is None or b is None:
            continue
        sa, sb = F.rate(key, a)[0], F.rate(key, b)[0]
        verdict = ("technique" if sa != "good" and sb != "good" else "fatigue" if sa == "good" and sb != "good"
                   else "tired_only" if sa != "good" else "ok")
        out.append({"key": key, "title": title, "unit": unit, "fresh": a, "tired": b, "fresh_state": sa, "tired_state": sb,
                    "verdict": verdict, "label": VERDICTS[verdict][0], "advice": VERDICTS[verdict][1]})
    order = {"fatigue": 0, "technique": 1, "tired_only": 2, "ok": 3}
    out.sort(key=lambda r: order[r["verdict"]])
    counts = {v: sum(1 for r in out if r["verdict"] == v) for v in order}
    return {"fresh": fresh, "tired": tired, "rows": out, "counts": counts}

"""Dashboard datasets: the same columns the dashboard page reads, built from the database."""
from __future__ import annotations

import csv
import datetime as dt
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .models import AiRun

COLUMNS = {
    "roster": ["athlete_id", "athlete", "gender", "grade", "group", "hr_max", "hr_rest", "ability_group", "vdot"],
    "benchmarks": ["athlete_id", "athlete", "gender", "group", "season", "event", "time_sec", "date", "course", "kind"],
    "goals": ["athlete_id", "event", "goal_sec", "season"],
    "weekly_load": ["athlete_id", "athlete", "group", "week_start", "miles", "acwr", "injured", "srpe"],
    "injuries": ["injury_id", "athlete_id", "athlete", "onset_date", "injury", "body_area", "days_missed", "status", "return_date"],
    "pain_reports": ["report_id", "athlete_id", "athlete", "date", "body_area", "severity", "reason"],
    "segments": ["workout_id", "date", "workout", "athlete_id", "athlete", "rep", "rep_m", "target_pace_sec_mi",
                 "pace_sec_mi", "avg_hr", "max_hr", "hr_drop_60s", "device"],
    "ai_insights": ["insight_id", "priority", "scope", "athlete_id", "category", "headline", "insight", "action",
                    "generated_at", "model"],
}


def insight_rows(db: Session) -> list[dict]:
    run = db.scalar(select(AiRun).where(AiRun.kind == "briefing", AiRun.status == "ok").order_by(AiRun.id.desc()))
    if run is None:
        return []
    gen = run.created_at.replace(tzinfo=dt.timezone.utc).isoformat()
    out = []
    for ins in run.insights:
        out.append({
            "insight_id": f"I{ins.position}", "priority": ins.priority, "scope": ins.scope,
            "athlete_id": f"A{ins.athlete_id:02d}" if ins.athlete_id else "", "category": ins.category,
            "headline": ins.headline, "insight": ins.insight, "action": ins.action, "generated_at": gen, "model": run.model,
        })
    return out


def build(db: Session, name: str, as_of: dt.date | None = None) -> list[dict]:
    as_of = as_of or A.team_today()
    if name == "roster":
        return A.roster_rows(db)
    if name == "benchmarks":
        return A.benchmark_rows(db)
    if name == "goals":
        return A.goal_rows(db)
    if name == "weekly_load":
        return A.weekly_load_rows(db, as_of)
    if name == "injuries":
        return A.injury_rows(db, as_of)
    if name == "pain_reports":
        return A.pain_rows(db, as_of)
    if name == "segments":
        return A.segment_rows(db)
    if name == "ai_insights":
        return insight_rows(db)
    raise KeyError(name)


def to_csv(name: str, rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS[name], extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS[name]})
    return buf.getvalue()


def snapshot(db: Session, as_of: dt.date | None = None) -> dict[str, list[dict]]:
    """Everything the dashboard and the AI briefing need, computed once."""
    as_of = as_of or A.team_today()
    data = {n: build(db, n, as_of) for n in COLUMNS if n != "ai_insights"}
    data["summaries"] = A.workout_summaries(data["segments"])
    data["watchlist"] = A.watchlist(data["weekly_load"], data["injuries"], data["summaries"], data["roster"], data["pain_reports"])
    data["progress"], data["seasons"] = A.progress(data["benchmarks"], data["goals"], data["roster"])
    data["kpis"] = A.kpis(data["injuries"], data["weekly_load"], data["progress"], data["watchlist"], data["roster"])
    data["risk_bands"] = A.risk_bands(data["weekly_load"], data["injuries"])
    data["injury_peaks"] = A.injury_peaks(data["injuries"], data["weekly_load"])
    data["recent_runs"] = recent_runs(db, as_of)
    from .models import Athlete as _Ath
    data["ai_consented"] = {a.code for a in db.scalars(select(_Ath).where(_Ath.active.is_(True), _Ath.ai_consent_at.is_not(None))).all()}
    data["missed_days"] = missed_days(db, as_of)
    data["as_of"] = as_of
    return data


def missed_days(db: Session, as_of: dt.date, days: int = 14) -> list[dict]:
    """Days athletes reported not running. The athlete's written description is left out on purpose."""
    from .models import Athlete, MissedDay
    q = (select(MissedDay, Athlete).join(Athlete, Athlete.id == MissedDay.athlete_id)
         .where(Athlete.active.is_(True), MissedDay.date > as_of - dt.timedelta(days=days), MissedDay.date <= as_of)
         .order_by(MissedDay.date))
    return [{"athlete_id": a.code, "date": m.date.isoformat(), "reason": m.reason, "area": m.body_area or None,
             "side": m.side or None, "pain": m.pain, "started": m.started or None, "seen_by": m.seen_by or None,
             "more_days": m.more_days or None} for m, a in db.execute(q).all()]


def recent_runs(db: Session, as_of: dt.date, days: int = 7) -> list[dict]:
    from .models import Activity, Athlete
    q = (select(Activity, Athlete).join(Athlete, Athlete.id == Activity.athlete_id)
         .where(Athlete.active.is_(True), Activity.local_date > as_of - dt.timedelta(days=days), Activity.local_date <= as_of)
         .order_by(Activity.local_date))
    out = []
    for act, a in db.execute(q).all():
        if "run" not in (act.sport or "running"):
            continue
        out.append({"athlete_id": a.code, "date": act.local_date.isoformat(), "miles": round((act.distance_m or 0) / A.METERS_PER_MILE, 1),
                    "rpe": act.rpe, "feel": act.feel, "workout": act.planned.title if act.planned else ""})
    return out

"""Coaching analytics.

The row builders (``*_rows``) read the database and return plain dicts with the same columns the
dashboard reads. The pure functions below them (risk_bands, watchlist, progress, kpis, ...) mirror the
dashboard's own calculations exactly, so the AI briefing and the dashboard always agree; the test
suite checks that they match number for number.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .models import Activity, AppSetting, Athlete, Goal, Injury, MissedDay, PainReport, PlannedWorkout, Result, TrainingGroup

METERS_PER_MILE = 1609.344
# Coaching flags. The dashboard page uses the same three numbers; change both together.
LOAD_RATIO_FLAG = 1.3   # this week's miles vs the previous 4-week average
SLOW_RECOVERY_BPM = 25  # heart-rate drop in the first 60 s of rest between reps
PAIN_FLAG = 4           # athlete-reported pain, 0-10
EVENTS = {"1 Mile": 1609.344, "1.5 Mile": 2414.016, "2 Mile": 3218.688, "5K": 5000.0}
# name -> (benchmark event, multiplier applied to the time)
EVENT_ALIASES = {
    "1 mile": ("1 Mile", 1.0), "mile": ("1 Mile", 1.0), "1mi": ("1 Mile", 1.0), "1 mi": ("1 Mile", 1.0),
    "1.5 mile": ("1.5 Mile", 1.0), "1.5 mi": ("1.5 Mile", 1.0), "1.5mi": ("1.5 Mile", 1.0),
    "2 mile": ("2 Mile", 1.0), "2 mi": ("2 Mile", 1.0), "2mi": ("2 Mile", 1.0), "two mile": ("2 Mile", 1.0),
    "5k": ("5K", 1.0), "5000m": ("5K", 1.0), "5000": ("5K", 1.0), "3.1 mile": ("5K", 1.0),
    # Track 1600 m / 3200 m converted to mile / 2-mile equivalents at the same average pace.
    "1600m": ("1 Mile", 1609.344 / 1600), "1600": ("1 Mile", 1609.344 / 1600),
    "3200m": ("2 Mile", 3218.688 / 3200), "3200": ("2 Mile", 3218.688 / 3200),
}


# ---------- small helpers ----------

def num(x):
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def normalize_event(name: str) -> tuple[str, float]:
    key = re.sub(r"\s+", " ", str(name).strip().lower())
    if key in EVENT_ALIASES:
        return EVENT_ALIASES[key]
    for ev in EVENTS:
        if key == ev.lower():
            return ev, 1.0
    raise ValueError(f"Unknown event '{name}'. Use 1 Mile, 1.5 Mile, 2 Mile, 5K, 1600m or 3200m.")


def parse_time(text: str) -> float:
    """'15:42.3' -> 942.3 ; '4:31' -> 271 ; '1:02:03' -> 3723 ; '942.3' -> 942.3"""
    s = str(text).strip()
    if not s:
        raise ValueError("Empty time.")
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", s):
        raise ValueError(f"Time '{text}' should look like 16:42.3")
    parts = s.split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if len(parts) > 1 and any(float(p) >= 60 for p in parts[1:]):
        raise ValueError(f"Time '{text}' has 60 or more seconds or minutes in a field.")
    if total <= 0:
        raise ValueError("Time must be positive.")
    return round(total, 2)


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return ""
    t = math.floor(seconds * 10 + 0.5) / 10
    m = int(t // 60)
    return f"{m}:{t - m * 60:04.1f}"


def season_for(d: dt.date) -> str:
    """Jul-Nov = cross country of that year; Dec-Jun = track of the spring year."""
    if 7 <= d.month <= 11:
        return f"{d.year} XC"
    return f"{d.year + 1 if d.month == 12 else d.year} Track"


def race_history(races) -> dict:
    """An athlete's races (newest first) by season, with PR / season-best marks, and groups of similar races to compare:
    same course and distance first (the fair comparison in cross country), then every race at one distance.
    Distances within 1% count as the same."""
    def dist_key(r):
        return round(r.distance_m / 50)  # 50 m buckets: "5,000 Meters" and "5K" match, 4K and 5K don't
    best: dict[int, float] = {}
    season_best: dict[tuple[str, int], float] = {}
    for r in races:
        k, s = dist_key(r), season_for(r.date)
        best[k] = min(best.get(k, r.time_s), r.time_s)
        season_best[(s, k)] = min(season_best.get((s, k), r.time_s), r.time_s)
    seasons: dict[str, list[dict]] = {}
    for r in races:
        s, k = season_for(r.date), dist_key(r)
        seasons.setdefault(s, []).append({"r": r, "pr": r.time_s == best[k], "sb": r.time_s == season_best[(s, k)]})

    def compare(rows, label):
        rows = sorted(rows, key=lambda r: (r.date, r.id or 0))
        top = min(r.time_s for r in rows)
        out, prev = [], None
        for r in rows:
            out.append({"r": r, "vs_prev": None if prev is None else round(r.time_s - prev, 2), "vs_best": round(r.time_s - top, 2),
                        "pr": r.time_s == top})
            prev = r.time_s
        return {"label": label, "rows": out}

    by_course: dict[tuple, list] = {}
    by_distance: dict[int, list] = {}
    for r in races:
        place = (r.course or r.meet or "").strip()
        by_course.setdefault((place.lower(), dist_key(r)), []).append(r)
        by_distance.setdefault(dist_key(r), []).append(r)
    groups = []
    for (_, _k), rows in sorted(by_course.items(), key=lambda kv: -max(r.date for r in kv[1]).toordinal()):
        if len(rows) >= 2:
            newest = max(rows, key=lambda r: r.date)  # labels as written most recently
            groups.append(compare(rows, f"{(newest.course or newest.meet).strip()} · {newest.distance_label} ({len(rows)} races)"))
    for _k, rows in sorted(by_distance.items(), key=lambda kv: -len(kv[1])):
        if len(rows) >= 2:
            groups.append(compare(rows, f"Every {max(rows, key=lambda r: r.date).distance_label} race ({len(rows)})"))
    return {"seasons": list(seasons.items()), "compare": groups}


def fmt_gap(seconds: float | None) -> str:
    """Change in time for people: '-39.0 s', '+1:05.2', 'same'."""
    if seconds is None:
        return ""
    if abs(seconds) < 0.05:
        return "same"
    sign = "−" if seconds < 0 else "+"
    s = abs(seconds)
    return f"{sign}{int(s // 60)}:{s % 60:04.1f}" if s >= 60 else f"{sign}{s:.1f} s"


def team_today() -> dt.date:
    if settings().as_of_date:
        return dt.date.fromisoformat(settings().as_of_date)
    return dt.datetime.now(settings().tz).date()


def week_starts(as_of: dt.date, n: int) -> list[dt.date]:
    """Start dates of n rolling 7-day weeks, oldest first; the last week ends on as_of."""
    return [as_of - dt.timedelta(days=6 + 7 * k) for k in reversed(range(n))]


def load_ratios(miles: list[float]) -> list[float | None]:
    """This week's miles divided by the average of the previous four weeks."""
    out: list[float | None] = []
    for i, m in enumerate(miles):
        if i < 4:
            out.append(None)
            continue
        chronic = sum(miles[i - 4:i]) / 4
        out.append(m / chronic if chronic > 0 else None)
    return out


def local_date(start_utc: dt.datetime) -> dt.date:
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=dt.timezone.utc)
    return start_utc.astimezone(settings().tz).date()


# ---------- VDOT (estimated VO2max) and ability groups ----------

RUN_TYPES = ("Easy", "Long", "Workout", "Race", "Recovery", "Other")
MISSED_REASONS = ("Injury", "Sick", "Planned rest day", "School or family", "Other")
SIDES = ("Left", "Right", "Both")
STARTED = ("Today", "This week", "Over a week ago", "An old injury is back")
SEEN_BY = ("Not yet", "Athletic trainer", "Doctor or physical therapist")
MORE_DAYS = ("Just today", "A few days", "Not sure yet")
PRACTICE_DAYS = (0, 1, 2, 3, 4, 5)  # Monday to Saturday; a day with no run on these asks the athlete why


def missed_label(m: MissedDay) -> str:
    if m.reason == "Injury":
        where = ", ".join(x for x in (m.body_area, (m.side or "").lower()) if x)
        return "Injury" + (f": {where.lower()}" if where else "")
    return m.reason


def unlogged_days(db: Session, athlete: Athlete, as_of: dt.date, days: int = 14) -> list[dt.date]:
    """Practice days in the last two weeks (not today) with no run and no report, after the athlete's first run."""
    first = db.scalar(select(func.min(Activity.local_date)).where(Activity.athlete_id == athlete.id))
    if first is None:
        return []
    ran = {d for (d,) in db.execute(select(Activity.local_date).where(
        Activity.athlete_id == athlete.id, Activity.local_date >= as_of - dt.timedelta(days=days))).all()}
    told = {d for (d,) in db.execute(select(MissedDay.date).where(MissedDay.athlete_id == athlete.id)).all()}
    out = []
    for k in range(1, days + 1):
        d = as_of - dt.timedelta(days=k)
        if d >= first and d.weekday() in PRACTICE_DAYS and d not in ran and d not in told:
            out.append(d)
    return out


def vdot(distance_m: float, time_s: float) -> float:
    """Jack Daniels and Jimmy Gilbert's VDOT: VO2max estimated from one race or time trial."""
    t = time_s / 60.0
    v = distance_m / t  # meters per minute
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct


def get_setting(db: Session, key: str, default: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def equivalent_time(distance_m: float, v: float) -> float:
    """Race time (seconds) over a distance that the VDOT formula gives for a VDOT."""
    lo, hi = 30.0, 6000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if vdot(distance_m, mid) > v:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _secs(t: str) -> float:
    m, s = str(t).strip().split(":")
    return int(m) * 60 + float(s)


def vdot_targets(db: Session) -> dict[int, tuple[str, str]]:
    """The team's target 800 and 1600 times by whole-number VDOT (edited on the Groups page)."""
    import json
    raw = get_setting(db, "vdot_targets", "")
    try:
        return {int(k): (v[0], v[1]) for k, v in json.loads(raw).items()} if raw else {}
    except (ValueError, TypeError, IndexError):
        return {}


def team_vdot(v: float, targets: dict[int, tuple[str, str]]) -> int:
    """Whole-number VDOT on the team's own scale: the highest row of the team's table whose 1600 time the result's
    1600 equivalent reaches. Outside the table, or with no table, the formula's VDOT rounded."""
    rounded = int(math.floor(v + 0.5))
    if not targets:
        return rounded
    lo, hi = min(targets), max(targets)
    t16 = equivalent_time(1600.0, v)
    reached = [n for n, (_, t) in targets.items() if t16 <= _secs(t) + 0.5]
    if not reached:
        return min(rounded, lo - 1)
    best = max(reached)
    return max(best, rounded) if best == hi else best


def group_for(v: float | None, gender: str, groups: list[TrainingGroup]) -> str:
    if v is None:
        return ""
    key = (lambda g: g.min_vdot_girls) if gender == "Girls" else (lambda g: g.min_vdot_boys)
    best = ""
    for g in sorted(groups, key=key):
        if v >= key(g):
            best = g.name
    return best


def abilities(db: Session, as_of: dt.date) -> dict[int, dict]:
    """athlete id -> {group, vdot, source, override} from the highest VDOT in the lookback window."""
    lookback = int(get_setting(db, "group_lookback_days", "365"))
    groups = db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()
    names = {g.name for g in groups}
    colors = {g.name: g.color or "" for g in groups}
    targets = vdot_targets(db)
    best: dict[int, tuple[float, Result]] = {}
    since = as_of - dt.timedelta(days=lookback)
    for r in db.scalars(select(Result).where(Result.date >= since, Result.date <= as_of)).all():
        if r.event not in EVENTS or not r.time_s:
            continue
        v = vdot(EVENTS[r.event], r.time_s)
        if r.athlete_id not in best or v > best[r.athlete_id][0]:
            best[r.athlete_id] = (v, r)
    out = {}
    for a in db.scalars(select(Athlete).where(Athlete.active.is_(True))).all():
        v, src = best.get(a.id, (None, None))
        tv = team_vdot(v, targets) if v is not None else None
        auto = group_for(tv, a.gender, groups)
        pinned = a.ability_override if a.ability_override in names else ""
        grp = pinned or auto
        tgt = targets.get(tv) if tv is not None else None
        out[a.id] = {
            "group": grp, "auto_group": auto, "override": pinned, "vdot": tv, "color": colors.get(grp, ""),
            "target_800": tgt[0] if tgt else "", "target_1600": tgt[1] if tgt else "",
            "source": f"{src.event} {fmt_time(src.time_s)} on {src.date:%b %-d, %Y}" if src is not None else "",
        }
    return out


def latest_vo2(db: Session, athlete_id: int) -> Activity | None:
    """The most recent run that carried the watch's VO2 max estimate. Shown for interest only: VDOT and running
    groups come from race results."""
    return db.scalar(select(Activity).where(Activity.athlete_id == athlete_id, Activity.vo2_max.is_not(None))
                     .order_by(Activity.start_utc.desc()).limit(1))


def run_type_of(act: Activity) -> str:
    """The run type the athlete or coach chose, else a suggestion from the data."""
    if act.run_type:
        return act.run_type
    return "Workout" if act.planned_workout_id or any(l.is_rep for l in act.laps) else "Easy"


# ---------- row builders (database -> dashboard columns) ----------

def roster_rows(db: Session, as_of: dt.date | None = None) -> list[dict]:
    rows = db.scalars(select(Athlete).where(Athlete.active.is_(True)).order_by(Athlete.id)).all()
    ab = abilities(db, as_of or team_today())
    return [{
        "athlete_id": a.code, "athlete": a.name, "gender": a.gender, "grade": a.grade, "group": a.group,
        "hr_max": a.hr_max if a.hr_max is not None else "", "hr_rest": a.hr_rest if a.hr_rest is not None else "",
        "ability_group": ab.get(a.id, {}).get("group", ""), "vdot": ab.get(a.id, {}).get("vdot") or "",
    } for a in rows]


def benchmark_rows(db: Session) -> list[dict]:
    q = select(Result, Athlete).join(Athlete, Athlete.id == Result.athlete_id).where(Athlete.active.is_(True)).order_by(Result.date, Result.id)
    return [{
        "athlete_id": a.code, "athlete": a.name, "gender": a.gender, "group": a.group, "season": r.season,
        "event": r.event, "time_sec": round(r.time_s, 1), "date": r.date.isoformat(), "course": r.course, "kind": r.kind,
    } for r, a in db.execute(q).all()]


def current_xc_season(benchmarks: list[dict], goals: list[dict] | None = None) -> str | None:
    seasons = {b["season"] for b in benchmarks if str(b["season"]).endswith(" XC")}
    seasons |= {g["season"] for g in (goals or []) if str(g.get("season", "")).endswith(" XC")}
    return max(seasons, key=lambda s: int(s.split()[0])) if seasons else None


def goal_rows(db: Session, season: str | None = None) -> list[dict]:
    q = select(Goal, Athlete).join(Athlete, Athlete.id == Goal.athlete_id).where(Athlete.active.is_(True))
    all_rows = [{"athlete_id": a.code, "event": g.event, "goal_sec": round(g.goal_s, 1), "season": g.season} for g, a in db.execute(q).all()]
    if season is None:
        season = current_xc_season(benchmark_rows(db), all_rows)
    return [r for r in all_rows if r["season"] == season]


def _injury_is_on(inj_onset: dt.date, inj_return: dt.date | None, start: dt.date, end: dt.date) -> bool:
    return inj_onset <= end and (inj_return is None or inj_return > start)


def weekly_load_rows(db: Session, as_of: dt.date, n_weeks: int = 21) -> list[dict]:
    starts = week_starts(as_of, n_weeks + 4)
    first = starts[0]
    athletes = db.scalars(select(Athlete).where(Athlete.active.is_(True)).order_by(Athlete.id)).all()
    acts = db.execute(
        select(Activity.athlete_id, Activity.local_date, Activity.distance_m, Activity.sport, Activity.rpe, Activity.duration_s)
        .where(Activity.local_date >= first, Activity.local_date <= as_of)
    ).all()
    miles = defaultdict(lambda: [0.0] * len(starts))
    srpe = defaultdict(lambda: [0.0] * len(starts))
    for aid, d, dist, sport, rpe, dur in acts:
        if not _is_run(sport):
            continue
        k = (d - first).days // 7
        if 0 <= k < len(starts):
            miles[aid][k] += (dist or 0) / METERS_PER_MILE
            if rpe is not None:
                srpe[aid][k] += rpe * (dur or 0) / 60.0
    injuries = defaultdict(list)
    for inj in db.scalars(select(Injury)).all():
        injuries[inj.athlete_id].append(inj)
    # An athlete's history starts with their first recorded run. Until four full weeks of history exist the
    # ratio is left blank: a new athlete's first weeks would otherwise look like a huge jump.
    first_run = dict(db.execute(select(Activity.athlete_id, func.min(Activity.local_date)).group_by(Activity.athlete_id)).all())
    out = []
    for a in athletes:
        m = miles[a.id]
        ratios = load_ratios(m)
        first = first_run.get(a.id)
        for k in range(4, len(starts)):
            ws = starts[k]
            we = ws + dt.timedelta(days=6)
            hurt = any(_injury_is_on(i.onset_date, i.return_date, ws, we) for i in injuries[a.id])
            enough = first is not None and ws >= first + dt.timedelta(days=28)
            ratio = ratios[k] if enough else None
            out.append({
                "athlete_id": a.code, "athlete": a.name, "group": a.group, "week_start": ws.isoformat(),
                "miles": round(m[k], 1), "acwr": "" if ratio is None else round(ratio, 2), "injured": 1 if hurt else 0,
                "srpe": round(srpe[a.id][k]),
            })
    return out


def season_range(d: dt.date) -> tuple[dt.date, dt.date]:
    """Jul 1 - Nov 30 for cross country; Dec 1 - Jun 30 for track."""
    s = season_for(d)
    yr = int(s.split()[0])
    if s.endswith("XC"):
        return dt.date(yr, 7, 1), dt.date(yr, 11, 30)
    return dt.date(yr - 1, 12, 1), dt.date(yr, 6, 30)


def athlete_series(db: Session, athlete: Athlete, as_of: dt.date) -> dict:
    """Everything one athlete's charts need, for every time range."""
    acts = db.scalars(select(Activity).where(Activity.athlete_id == athlete.id, Activity.local_date <= as_of)
                      .options(selectinload(Activity.laps), selectinload(Activity.planned)).order_by(Activity.start_utc)).all()
    runs = []
    for a in acts:
        if not _is_run(a.sport):
            continue
        mi = (a.distance_m or 0) / METERS_PER_MILE
        mins = (a.duration_s or 0) / 60.0
        reps = sum(1 for l in a.laps if l.is_rep)
        rtype = run_type_of(a)
        if a.planned:
            label = a.planned.title
        elif rtype == "Workout" and reps:
            label = f"{reps} reps"
        else:
            label = {"Long": "Long run", "Race": "Race", "Workout": "Workout"}.get(rtype, "")
        runs.append({
            "id": a.id, "date": a.local_date.isoformat(), "miles": round(mi, 2), "minutes": round(mins, 1), "type": rtype,
            "pace": round(a.duration_s / mi, 1) if mi >= 0.2 and a.duration_s else None,
            "avg_hr": round(a.avg_hr) if a.avg_hr else None, "rpe": a.rpe, "feel": a.feel or "",
            "srpe": round(a.rpe * mins) if a.rpe is not None else None,
            "workout": label,
        })
    first = dt.date.fromisoformat(runs[0]["date"]) if runs else as_of
    n_weeks = max(1, (as_of - first).days // 7 + 1)
    starts = week_starts(as_of, n_weeks + 4)
    daily = defaultdict(float)
    for r in runs:
        daily[r["date"]] += r["miles"]
    weekly = [sum(daily.get((s + dt.timedelta(days=i)).isoformat(), 0.0) for i in range(7)) for s in starts]
    ratios = load_ratios(weekly)
    injuries = db.scalars(select(Injury).where(Injury.athlete_id == athlete.id).order_by(Injury.onset_date)).all()
    weeks = []
    for k in range(4, len(starts)):
        ws = starts[k]
        enough = runs and ws >= first + dt.timedelta(days=28)
        weeks.append({"week_start": ws.isoformat(), "miles": round(weekly[k], 1),
                      "ratio": round(ratios[k], 2) if enough and ratios[k] is not None else None,
                      "injured": any(_injury_is_on(i.onset_date, i.return_date, ws, ws + dt.timedelta(days=6)) for i in injuries)})
    results = db.scalars(select(Result).where(Result.athlete_id == athlete.id).order_by(Result.date)).all()
    pains = db.scalars(select(PainReport).where(PainReport.athlete_id == athlete.id).order_by(PainReport.date)).all()
    missed = db.scalars(select(MissedDay).where(MissedDay.athlete_id == athlete.id, MissedDay.date <= as_of).order_by(MissedDay.date)).all()
    s0, s1 = season_range(as_of)
    return {
        "athlete": athlete.name, "today": as_of.isoformat(), "first_run": runs[0]["date"] if runs else None,
        "season": {"name": season_for(as_of), "start": s0.isoformat(), "end": s1.isoformat()},
        "load_ratio_flag": LOAD_RATIO_FLAG, "runs": runs, "weeks": weeks,
        "results": [{"date": r.date.isoformat(), "event": r.event, "time": round(r.time_s, 1), "season": r.season,
                     "course": r.course, "pace": round(r.time_s / EVENTS[r.event] * METERS_PER_MILE, 1)} for r in results if r.event in EVENTS],
        "pain": sorted([{"date": p.date.isoformat(), "area": p.body_area, "severity": p.severity} for p in pains]
                       + [{"date": m.date.isoformat(), "area": "Missed run: " + missed_label(m).lower(), "severity": m.pain}
                          for m in missed if m.reason in ("Injury", "Sick")], key=lambda x: x["date"]),
        "injuries": [{"onset": i.onset_date.isoformat(), "return": i.return_date.isoformat() if i.return_date else None,
                      "injury": i.injury} for i in injuries],
    }


def _is_run(sport: str | None) -> bool:
    s = (sport or "running").lower()
    return "run" in s


def injury_rows(db: Session, as_of: dt.date) -> list[dict]:
    q = select(Injury, Athlete).join(Athlete, Athlete.id == Injury.athlete_id).where(Athlete.active.is_(True)).order_by(Injury.onset_date)
    out = []
    for i, a in db.execute(q).all():
        active = i.return_date is None or i.return_date > as_of
        end = i.return_date or as_of
        out.append({
            "injury_id": f"INJ{i.id:02d}", "athlete_id": a.code, "athlete": a.name, "onset_date": i.onset_date.isoformat(),
            "injury": i.injury, "body_area": i.body_area, "days_missed": max(0, (end - i.onset_date).days),
            "status": "Active" if active else "Cleared", "return_date": i.return_date.isoformat() if i.return_date else "",
        })
    return out


def pain_rows(db: Session, as_of: dt.date, days: int = 28) -> list[dict]:
    """Athlete pain reports, plus days missed through injury or sickness (those carry a reason)."""
    since = as_of - dt.timedelta(days=days)
    q = (select(PainReport, Athlete).join(Athlete, Athlete.id == PainReport.athlete_id)
         .where(Athlete.active.is_(True), PainReport.date >= since, PainReport.date <= as_of))
    rows = [{"report_id": f"P{p.id:03d}", "athlete_id": a.code, "athlete": a.name, "date": p.date.isoformat(),
             "body_area": p.body_area, "severity": p.severity, "reason": ""} for p, a in db.execute(q).all()]
    q = (select(MissedDay, Athlete).join(Athlete, Athlete.id == MissedDay.athlete_id)
         .where(Athlete.active.is_(True), MissedDay.date >= since, MissedDay.date <= as_of, MissedDay.reason.in_(("Injury", "Sick"))))
    for m, a in db.execute(q).all():
        where = ", ".join(x for x in (m.body_area, (m.side or "").lower()) if x)
        rows.append({"report_id": f"M{m.id:03d}", "athlete_id": a.code, "athlete": a.name, "date": m.date.isoformat(),
                     "body_area": where, "severity": m.pain if m.pain is not None else "",
                     "reason": "Missed run (injury)" if m.reason == "Injury" else "Missed run (sick)"})
    rows.sort(key=lambda r: (r["date"], r["report_id"]))
    return rows


def target_pace(db: Session, athlete_id: int, pw: PlannedWorkout) -> float | None:
    """Target pace (sec/mile) from the athlete's best time in the workout's pace event."""
    if pw.pace_event not in EVENTS:
        return None
    rs = db.scalars(select(Result).where(Result.athlete_id == athlete_id, Result.event == pw.pace_event, Result.date <= pw.date)).all()
    if not rs:
        return None
    season = season_for(pw.date)
    same = [r for r in rs if r.season == season]
    recent = [r for r in rs if (pw.date - r.date).days <= 400]
    pool = same or recent
    if not pool:
        return None
    best = min(r.time_s for r in pool)
    return best / EVENTS[pw.pace_event] * METERS_PER_MILE * pw.pace_factor


# Workout types and their pace factor (times the pace event's race pace); anything else is "Other".
PACE_TYPES = {"Interval": 1.00, "Threshold": 1.07, "Tempo": 1.10, "Long": 1.25, "Recovery": 1.35}


def pace_type(factor: float) -> str:
    return next((name for name, f in PACE_TYPES.items() if abs(factor - f) < 0.0005), "Other")


RIEGEL_EXPONENT = 1.06  # race-time equivalence between distances: T2 = T1 * (D2 / D1) ** 1.06


def _pace_from(times: list[tuple[str, float, str]], event: str) -> tuple[float, str] | None:
    """Best equivalent time for `event` from (event, seconds, where) entries: the event itself if present, otherwise the
    fastest Riegel equivalent from another benchmark distance. Returns (seconds, plain-language source)."""
    exact = [t for t in times if t[0] == event]
    if exact:
        ev, sec, where = min(exact, key=lambda t: t[1])
        return sec, f"{where} {ev}, {fmt_time(sec)}"
    best = None
    for ev, sec, where in times:
        eq = sec * (EVENTS[event] / EVENTS[ev]) ** RIEGEL_EXPONENT
        if best is None or eq < best[0]:
            best = (eq, f"estimated from {where} {ev}, {fmt_time(sec)}")
    return best


def workout_paces(db: Session, athlete, workouts) -> list[dict]:
    """For each planned workout: the athlete's rep time, mile pace and 400 m split from their current best and from
    their goal, in the workout's pace event times its pace factor."""
    results = [r for r in db.scalars(select(Result).where(Result.athlete_id == athlete.id)).all() if r.event in EVENTS]
    goals = [g for g in db.scalars(select(Goal).where(Goal.athlete_id == athlete.id)).all() if g.event in EVENTS]
    out = []
    for pw in workouts:
        if pw.pace_event not in EVENTS:
            continue
        season = season_for(pw.date)
        done = [r for r in results if r.date <= pw.date]
        pool = [r for r in done if r.season == season] or [r for r in done if (pw.date - r.date).days <= 400]
        now = _pace_from([(r.event, r.time_s, "your best") for r in pool], pw.pace_event)
        goal_pool = [g for g in goals if g.season == season]
        if not goal_pool and goals:
            latest = max(g.season for g in goals)
            goal_pool = [g for g in goals if g.season == latest]
        goal = _pace_from([(g.event, g.goal_s, "your goal") for g in goal_pool], pw.pace_event)

        def targets(found):
            if found is None:
                return None
            per_mile = found[0] / EVENTS[pw.pace_event] * METERS_PER_MILE * pw.pace_factor
            return {"rep": per_mile * pw.rep_distance_m / METERS_PER_MILE, "mile": per_mile,
                    "lap": per_mile * 400 / METERS_PER_MILE, "source": found[1]}
        out.append({"workout": pw, "now": targets(now), "goal": targets(goal)})
    return out


def segment_rows(db: Session) -> list[dict]:
    acts = db.scalars(
        select(Activity).where(Activity.planned_workout_id.is_not(None)).options(
            selectinload(Activity.laps), selectinload(Activity.athlete), selectinload(Activity.planned))
        .order_by(Activity.local_date, Activity.athlete_id)
    ).all()
    out = []
    cache: dict[tuple[int, int], float | None] = {}
    for act in acts:
        pw, a = act.planned, act.athlete
        if pw is None or not a.active:
            continue
        key = (a.id, pw.id)
        if key not in cache:
            cache[key] = target_pace(db, a.id, pw)
        tgt = cache[key]
        rep_m = int(pw.rep_distance_m) if float(pw.rep_distance_m).is_integer() else pw.rep_distance_m
        for lap in act.laps:
            if not lap.is_rep or not lap.distance_m:
                continue
            out.append({
                "workout_id": f"W{pw.id}", "date": pw.date.isoformat(), "workout": pw.title, "athlete_id": a.code,
                "athlete": a.name, "rep": lap.rep_num, "rep_m": rep_m,
                "target_pace_sec_mi": "" if tgt is None else round(tgt, 1),
                "pace_sec_mi": round(lap.timer_s / lap.distance_m * METERS_PER_MILE, 1),
                "avg_hr": "" if lap.avg_hr is None else round(lap.avg_hr),
                "max_hr": "" if lap.max_hr is None else round(lap.max_hr),
                "hr_drop_60s": "" if lap.hr_drop_60s is None else round(lap.hr_drop_60s),
                "device": act.device,
            })
    return out


# ---------- pure analytics (mirror the dashboard's calculations) ----------

def _f(r, k):
    return num(r.get(k))


def _r0(x: float) -> int:
    """Round half up, like JavaScript's Math.round (Python's round() rounds half to even)."""
    return int(math.floor(x + 0.5))


def _r1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def risk_bands(weekly: list[dict], injuries: list[dict], flag: float = LOAD_RATIO_FLAG) -> list[dict]:
    def band(x):
        return "Under 0.8" if x < 0.8 else ("0.8 to 1.3" if x <= flag else "Over 1.3")
    onsets = defaultdict(list)
    for i in injuries:
        onsets[i["athlete_id"]].append(dt.date.fromisoformat(i["onset_date"]))
    train = [r for r in weekly if int(_f(r, "injured") or 0) != 1 and _f(r, "acwr") is not None]
    out = []
    for b in ("Under 0.8", "0.8 to 1.3", "Over 1.3"):
        weeks = [r for r in train if band(_f(r, "acwr")) == b]
        hits = 0
        for r in weeks:
            ws = dt.date.fromisoformat(r["week_start"])
            if any(ws + dt.timedelta(days=7) <= o < ws + dt.timedelta(days=21) for o in onsets[r["athlete_id"]]):
                hits += 1
        out.append({"band": b, "weeks": len(weeks), "injuries": hits, "rate": hits / len(weeks) if weeks else 0.0})
    return out


def injury_peaks(injuries: list[dict], weekly: list[dict]) -> dict[str, float | None]:
    by_a = defaultdict(list)
    for r in weekly:
        if int(_f(r, "injured") or 0) != 1 and _f(r, "acwr") is not None:
            by_a[r["athlete_id"]].append((dt.date.fromisoformat(r["week_start"]), _f(r, "acwr")))
    out = {}
    for i in injuries:
        t = dt.date.fromisoformat(i["onset_date"])
        vals = [v for ws, v in by_a[i["athlete_id"]] if ws + dt.timedelta(days=7) <= t < ws + dt.timedelta(days=21)]
        out[i["injury_id"]] = max(vals) if vals else None
    return out


def workout_summaries(segments: list[dict], slow: float = SLOW_RECOVERY_BPM) -> list[dict]:
    by = defaultdict(list)
    for s in segments:
        by[(s["athlete_id"], s["workout_id"])].append(s)
    out = []
    for (aid, wid), reps in by.items():
        reps.sort(key=lambda r: int(r["rep"]))
        s0 = reps[0]
        pace = statistics.fmean(float(r["pace_sec_mi"]) for r in reps)
        target = _f(s0, "target_pace_sec_mi")
        hrs = [float(r["avg_hr"]) for r in reps if _f(r, "avg_hr") is not None]
        drops = [float(r["hr_drop_60s"]) for r in reps if _f(r, "hr_drop_60s") is not None]
        rec = _r1(statistics.fmean(drops)) if drops else None
        out.append({
            "athlete_id": aid, "workout_id": wid, "athlete": s0["athlete"], "workout": s0["workout"], "date": s0["date"],
            "reps": len(reps), "avg_pace": _r1(pace), "target": target,
            "off_target": None if target is None else _r1(pace - target),
            "avg_hr": _r0(statistics.fmean(hrs)) if hrs else None,
            "hr_rise": (hrs[-1] - hrs[0]) if len(hrs) > 1 else None,
            "recovery": rec, "flag": "Slow recovery" if rec is not None and rec < slow else "",
        })
    return out


def watchlist(weekly, injuries, summaries, roster, pain=None, flag=LOAD_RATIO_FLAG, pain_flag=PAIN_FLAG) -> list[dict]:
    now = max((r["week_start"] for r in weekly), default="")
    last = max((s["date"] for s in summaries), default="")
    out = []
    for a in roster:
        aid = a["athlete_id"]
        wk = next((r for r in weekly if r["athlete_id"] == aid and r["week_start"] == now), None)
        inj = next((i for i in injuries if i["athlete_id"] == aid and i["status"] == "Active"), None)
        ws = next((s for s in summaries if s["athlete_id"] == aid and s["date"] == last), None)
        mine = [p for p in (pain or []) if p["athlete_id"] == aid and p["date"] >= now]
        sore = [p for p in mine if not p.get("reason") and (num(p["severity"]) or 0) >= pain_flag]
        missed = [p for p in mine if p.get("reason")]
        why = []
        if inj:
            why.append("Injured: " + inj["injury"])
        acwr = _f(wk, "acwr") if wk else None
        if acwr is not None and acwr > flag:
            why.append(f"Load ratio {acwr:.2f}")
        if ws and ws["flag"]:
            why.append("Slow recovery in latest workout")
        if sore:
            worst = max(sore, key=lambda p: num(p["severity"]))
            why.append(f"Pain {int(num(worst['severity']))}/10: {worst['body_area']}")
        if missed:
            m = missed[-1]
            sev = num(m["severity"])
            why.append(m["reason"] + (f": {m['body_area']}" if m["body_area"] else "") + (f", {int(sev)}/10" if sev is not None else ""))
        if why:
            out.append({"athlete_id": aid, "athlete": a["athlete"], "group": a["group"],
                        "miles": _f(wk, "miles") if wk else None, "acwr": acwr,
                        "recovery": ws["recovery"] if ws else None, "flags": len(why), "reasons": "; ".join(why)})
    out.sort(key=lambda r: (-r["flags"], r["athlete"]))
    return out


def progress(benchmarks: list[dict], goals: list[dict], roster: list[dict]) -> tuple[list[dict], dict]:
    cur = current_xc_season(benchmarks, goals)
    if cur is None:
        return [], {}
    yr = int(cur.split()[0])
    seasons = {"last": f"{yr - 1} XC", "track": f"{yr} Track", "current": cur}
    best = {}
    for b in benchmarks:
        k = (b["athlete_id"], b["event"], b["season"])
        t = float(b["time_sec"])
        if k not in best or t < best[k]:
            best[k] = t
    goal = {(g["athlete_id"], g["event"]): float(g["goal_sec"]) for g in goals if not g.get("season") or g.get("season") == cur}
    rows = []
    for r in roster:
        for e in EVENTS:
            last = best.get((r["athlete_id"], e, seasons["last"]))
            cur_t = best.get((r["athlete_id"], e, cur))
            g = goal.get((r["athlete_id"], e))
            rows.append({
                "athlete_id": r["athlete_id"], "event": e, "athlete": r["athlete"], "group": r["group"],
                "last_xc": last, "track": best.get((r["athlete_id"], e, seasons["track"])), "current": cur_t, "goal": g,
                "change": None if last is None or cur_t is None else _r1(cur_t - last),
                "to_goal": None if g is None or cur_t is None else _r1(cur_t - g),
            })
    return rows, seasons


def kpis(injuries, weekly, progress_rows, watch, roster, flag=LOAD_RATIO_FLAG) -> dict:
    weeks = sorted({r["week_start"] for r in weekly})
    now = weeks[-1] if weeks else None
    prev = weeks[-2] if len(weeks) > 1 else None

    def over(w):
        return sum(1 for r in weekly if r["week_start"] == w and (_f(r, "acwr") or 0) > flag)
    gains = sorted(p["last_xc"] - p["current"] for p in progress_rows
                   if p["event"] == "5K" and p["last_xc"] is not None and p["current"] is not None)
    fives = [p for p in progress_rows if p["event"] == "5K" and p["to_goal"] is not None]
    return {
        "week_start": now, "roster": len(roster),
        "injured_now": sum(1 for i in injuries if i["status"] == "Active"),
        "over_now": over(now), "over_prev": over(prev) if prev else 0,
        "median_gain": _r1(statistics.median(gains)) if gains else None, "returners": len(gains),
        "goals_met": sum(1 for p in fives if p["to_goal"] <= 0), "goal_total": len(fives), "watch_count": len(watch),
    }

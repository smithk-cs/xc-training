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

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .models import (Activity, AppSetting, Athlete, Goal, Injury, MissedDay, PainReport, PlannedWorkout, RaceResult, RestingHeartRate, Result,
                     VdotChange,
                     TrainingGroup)

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


def week_monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def week_miles(db: Session, athlete_ids: list[int], monday: dt.date) -> dict[int, float]:
    """Miles run in the Monday-to-Sunday week starting monday, per athlete."""
    rows = db.execute(select(Activity.athlete_id, func.sum(Activity.distance_m))
                      .where(Activity.athlete_id.in_(athlete_ids), Activity.local_date >= monday,
                             Activity.local_date <= monday + dt.timedelta(days=6)).group_by(Activity.athlete_id)).all()
    out = {aid: 0.0 for aid in athlete_ids}
    out.update({aid: (m or 0) / METERS_PER_MILE for aid, m in rows})
    return out


def mileage_goal(db: Session, athlete_id: int, group: str, today: dt.date, gender: str = "Boys") -> dict | None:
    """This week's miles against the athlete's group goal for their gender, or None when there is no goal."""
    g = db.scalar(select(TrainingGroup).where(TrainingGroup.name == group)) if group else None
    goal = g.miles_goal(gender) if g is not None else 0
    if not goal:
        return None
    monday = week_monday(today)
    done = week_miles(db, [athlete_id], monday)[athlete_id]
    last = week_miles(db, [athlete_id], monday - dt.timedelta(days=7))[athlete_id]
    return {"goal": goal, "done": done, "left": max(goal - done, 0), "pct": min(done / goal, 1),
            "days_left": 6 - today.weekday(), "last": last, "group": g.name}


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


def on_team():
    """Athletes counted in team numbers, the watch list, the VDOT page and the AI: on the team and not a test account."""
    return and_(Athlete.active.is_(True), Athlete.is_test.is_(False))


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


VDOT_MIN_M, VDOT_MAX_M = 1500, 10000   # race distances the VDOT formula is used for
VDOT_MIN, VDOT_MAX = 20, 90             # coach-set VDOT limits


def latest_races(db: Session, since: dt.date, as_of: dt.date) -> dict[int, tuple[float, str, dt.date]]:
    """athlete id -> (VDOT, description, date) from their most recent race or time trial in the window, at any distance
    from 1500 m to 10K (benchmark results, and meet results at other distances such as 4K). Two on the same day: the faster."""
    found: dict[int, tuple[dt.date, float, str]] = {}

    def offer(aid, day, v, text):
        cur = found.get(aid)
        if cur is None or day > cur[0] or (day == cur[0] and v > cur[1]):
            found[aid] = (day, v, text)

    for r in db.scalars(select(Result).where(Result.date >= since, Result.date <= as_of)).all():
        if r.event in EVENTS and r.time_s:
            offer(r.athlete_id, r.date, vdot(EVENTS[r.event], r.time_s), f"{r.event} {fmt_time(r.time_s)} on {r.date:%b %-d, %Y}")
    for r in db.scalars(select(RaceResult).where(RaceResult.date >= since, RaceResult.date <= as_of,
                                                 RaceResult.result_id.is_(None))).all():
        if r.time_s and VDOT_MIN_M <= r.distance_m <= VDOT_MAX_M:
            offer(r.athlete_id, r.date, vdot(r.distance_m, r.time_s),
                  f"{r.meet}, {r.distance_label} {fmt_time(r.time_s)} on {r.date:%b %-d, %Y}")
    return {aid: (v, text, day) for aid, (day, v, text) in found.items()}


def abilities(db: Session, as_of: dt.date) -> dict[int, dict]:
    """athlete id -> {group, vdot, source, override, ...}. VDOT comes from the athlete's most recent race in the look-back
    window, read on the team's scale, unless a coach has set it; the group follows from VDOT unless a coach pinned one."""
    lookback = int(get_setting(db, "group_lookback_days", "365"))
    groups = db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()
    names = {g.name for g in groups}
    colors = {g.name: g.color or "" for g in groups}
    targets = vdot_targets(db)
    latest = latest_races(db, as_of - dt.timedelta(days=lookback), as_of)
    out = {}
    for a in db.scalars(select(Athlete).where(Athlete.active.is_(True))).all():
        v, src, _day = latest.get(a.id, (None, "", None))
        race_vdot = team_vdot(v, targets) if v is not None else None
        tv = a.vdot_override if a.vdot_override is not None else race_vdot
        auto = group_for(tv, a.gender, groups)
        pinned = a.ability_override if a.ability_override in names else ""
        grp = pinned or auto
        tgt = targets.get(tv) if tv is not None else None
        out[a.id] = {
            "group": grp, "auto_group": auto, "override": pinned, "vdot": tv, "color": colors.get(grp, ""),
            "race_vdot": race_vdot, "vdot_set": a.vdot_override is not None,
            "target_800": tgt[0] if tgt else "", "target_1600": tgt[1] if tgt else "", "source": src,
        }
    return out


EASY_PCT, TEMPO_PCT = 0.67, 0.88   # share of VO2max for easy and tempo (threshold) running, as on the team's sheet
LONG_PCT, INTERVAL_PCT = 0.64, 0.975   # long runs a touch slower than easy; intervals near VO2max (Daniels' I pace)
PACE_ZONES = [("Easy", EASY_PCT), ("Long run", LONG_PCT), ("Tempo", TEMPO_PCT), ("Interval", INTERVAL_PCT)]
REP_PCT = 1.07   # Daniels' repetition (R) pace: short, fast reps, about mile race pace
WORKOUT_ZONES = dict(PACE_ZONES + [("Repetition", REP_PCT)])
OLD_TYPE_ZONE = {"Interval": "Interval", "Threshold": "Tempo", "Tempo": "Tempo", "Long": "Long run", "Recovery": "Easy"}


def minutes_text(mins: float) -> str:
    """14 -> '14 min', 1.5 -> '1:30 min'."""
    whole, secs = int(mins), round(mins % 1 * 60)
    return f"{whole}:{secs:02d} min" if secs else f"{whole} min"


def distance_text(m: float) -> str:
    """About how far: '1,250 m' under a mile, '2.3 mi' beyond."""
    return f"{m:,.0f} m" if m < METERS_PER_MILE else f"{m / METERS_PER_MILE:.1f} mi"


def part_label(p: dict) -> str:
    """'2 × 4 × 400 m', '6 × 1,000 m', '1 × 5 mi', '4 × 5 min' (parts run for a time have 'mins' instead of 'dist';
    parts set in miles keep 'mi' for showing, with 'dist' in meters)."""
    dist = minutes_text(p["mins"]) if p.get("mins") else f"{p['mi']:g} mi" if p.get("mi") else f"{p['dist']:,.0f} m"
    reps = f"{p['reps']} × {dist}" if p["reps"] > 1 else dist
    return f"{p['sets']} × {reps}" if p["sets"] > 1 else reps


def workout_summary(parts: list[dict]) -> str:
    """A name for a workout from its parts: '2 × 4 × 400 m Interval + 1,600 m Tempo'."""
    return " + ".join(f"{part_label(p)} {p['zone']}" for p in parts)


def main_part(parts: list[dict]) -> dict | None:
    """The hardest part (the one rep-matching and targets use)."""
    return max(parts, key=lambda p: WORKOUT_ZONES.get(p["zone"], 0)) if parts else None


def part_paces(parts: list[dict], v: float | None) -> list[dict]:
    """Each part with the athlete's time per rep, pace per mile and per 400 m at VDOT v."""
    out = []
    for p in parts:
        if v is None or p["zone"] not in WORKOUT_ZONES:
            out.append({"part": p, "label": part_label(p), "pace": None})
            continue
        mile = pace_at(v, WORKOUT_ZONES[p["zone"]])
        if p.get("mins"):  # run for a time: how far that is at this pace
            out.append({"part": p, "label": part_label(p), "pace": {
                "rep": None, "about": distance_text(p["mins"] * 60 / mile * METERS_PER_MILE), "mile": mile,
                "lap": mile * 400 / METERS_PER_MILE if p["zone"] in ("Tempo", "Interval", "Repetition") else None}})
            continue
        out.append({"part": p, "label": part_label(p), "pace": {
            "rep": mile * p["dist"] / METERS_PER_MILE, "mile": mile,
            "lap": mile * 400 / METERS_PER_MILE if p["dist"] > 400 and p["zone"] in ("Tempo", "Interval", "Repetition") else None}})
    return out


def group_part_paces(db: Session, parts: list[dict]) -> list[dict]:
    """For the coaches' preview: each running group's VDOT range and, per part, the rep time from its fastest to its
    slowest member (boys' cutoffs; the lowest group shows its top VDOT only)."""
    groups = sorted(db.scalars(select(TrainingGroup)).all(), key=lambda g: g.min_vdot_boys, reverse=True)
    out = []
    for i, g in enumerate(groups):
        lo = max(int(math.ceil(g.min_vdot_boys)), VDOT_MIN)
        hi = int(math.ceil(groups[i - 1].min_vdot_boys)) - 1 if i else lo
        bottom = g.min_vdot_boys < VDOT_MIN and i  # the catch-all group: its top VDOT only
        if bottom:
            lo = hi
        if hi < lo:
            continue
        times = []
        for p in parts:
            pct = WORKOUT_ZONES.get(p["zone"])
            if pct is None:
                times.append(None)
            elif p.get("mins"):  # timed parts show the pace per mile instead
                times.append((pace_at(hi, pct), pace_at(lo, pct), "/mi"))
            else:
                times.append((pace_at(hi, pct) * p["dist"] / METERS_PER_MILE, pace_at(lo, pct) * p["dist"] / METERS_PER_MILE, ""))
        out.append({"group": g, "range": f"{hi} and below" if bottom else f"{lo}-{hi}" if hi > lo else f"{lo}+" if i == 0 else str(lo),
                    "times": times})
    return out


def goal_vdot(db: Session, athlete_id: int, on: dt.date) -> int | None:
    """The VDOT of the athlete's fastest goal for this season (or their latest season of goals)."""
    goals = [g for g in db.scalars(select(Goal).where(Goal.athlete_id == athlete_id)).all() if g.event in EVENTS and g.goal_s]
    season = season_for(on)
    pool = [g for g in goals if g.season == season] or ([g for g in goals if g.season == max(x.season for x in goals)] if goals else [])
    if not pool:
        return None
    targets = vdot_targets(db)
    return max(team_vdot(vdot(EVENTS[g.event], g.goal_s), targets) for g in pool)


def training_paces(v: float | None) -> list[dict]:
    """Per-mile paces for each kind of run at VDOT v; intervals also per 400 m."""
    if v is None:
        return []
    out = []
    for name, pct in PACE_ZONES:
        mile = pace_at(v, pct)
        out.append({"name": name, "pct": pct, "mile": mile, "lap": mile * 400 / METERS_PER_MILE if name == "Interval" else None})
    return out


def pace_at(v: float, pct: float) -> float:
    """Seconds per mile when running at `pct` of VDOT `v` (Daniels' oxygen-cost equation, solved for speed)."""
    target = v * pct
    a, b, c = 0.000104, 0.182258, -4.60 - target
    speed = (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)   # meters per minute
    return METERS_PER_MILE / speed * 60


def weekly_plan_text(plan: str) -> str:
    """'7x8' -> '7x8=56'; anything else as typed."""
    m = re.fullmatch(r"\s*(\d+)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(?:=.*)?", plan or "")
    if not m:
        return (plan or "").strip()
    total = int(m.group(1)) * float(m.group(2))
    return f"{m.group(1)}x{m.group(2)}={total:g}"


def vdot_board(db: Session, as_of: dt.date, genders: tuple[str, ...] = ("Boys", "Girls")) -> list[dict]:
    """The VDOT page: per gender with athletes, each group from fastest down with its VDOT range, easy and tempo pace
    ranges, weekly mileage goal and its athletes by VDOT; then athletes with no VDOT yet."""
    ab = abilities(db, as_of)
    gains = vdot_gains(db, as_of)
    groups = db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()
    people = db.scalars(select(Athlete).where(on_team()).order_by(Athlete.last_name, Athlete.first_name)).all()
    out = []
    for gender in genders:
        mine = [a for a in people if a.gender == gender]
        if not mine:
            continue
        key = lambda g: g.min_vdot(gender)
        ordered = sorted(groups, key=key, reverse=True)
        sections = []
        for i, g in enumerate(ordered):
            lo = int(math.ceil(key(g)))
            hi = int(math.ceil(key(ordered[i - 1]))) - 1 if i else None
            members = sorted((a for a in mine if ab.get(a.id, {}).get("group") == g.name),
                             key=lambda a: (-(ab[a.id]["vdot"] or 0), a.last_name, a.first_name))
            bottom = lo < VDOT_MIN and hi is not None   # the catch-all group: "48 and below", paces of 48 or slower
            top = hi if hi is not None else lo
            zones = [{"name": n, "fast": pace_at(top, pct), "slow": pace_at(max(lo, VDOT_MIN), pct)} for n, pct in PACE_ZONES]
            rng = (f"{hi} and below" if bottom else f"{lo}-{hi}" if hi is not None and hi > lo else f"{lo} +" if hi is None else str(lo))
            sections.append({"group": g, "range": rng, "zones": zones, "miles": g.miles_goal(gender),
                             "single": hi is None or hi <= lo or bottom, "slower": bottom,
                             "rows": [{"a": a, **ab[a.id], "plan": weekly_plan_text(a.weekly_plan), "gain": gains.get(a.id),
                                       "paces": training_paces(ab[a.id]["vdot"])} for a in members]})
        rest = [{"a": a, **ab.get(a.id, {}), "plan": weekly_plan_text(a.weekly_plan), "paces": []}
                for a in mine if not ab.get(a.id, {}).get("group")]
        out.append({"gender": gender, "sections": sections, "rest": rest})
    return out


RESTING_HR_MIN, RESTING_HR_MAX = 30, 120
RESTING_HR_FLAG = 5      # bpm: a week this far above the athlete's usual is worth a check-in (illness, fatigue, poor sleep)
RESTING_HR_WINDOW = 35   # days looked at: the past week, and the four weeks before it as "usual"


def save_resting_hr(db: Session, athlete_id: int, day: dt.date, bpm: int, source: str) -> RestingHeartRate | None:
    """Store one day's value. Garmin updates its own value through the day but never replaces one the athlete typed."""
    row = db.scalar(select(RestingHeartRate).where(RestingHeartRate.athlete_id == athlete_id, RestingHeartRate.date == day))
    if row is not None and source == "garmin" and row.source == "athlete":
        return None
    if row is None:
        row = RestingHeartRate(athlete_id=athlete_id, date=day)
        db.add(row)
    row.bpm, row.source = bpm, source
    return row


RESTING_HR_BACKDATE = 34   # athletes can fill in or fix days this far back (the calendar's five weeks)


def resting_hr_calendar(db: Session, athlete_id: int, today: dt.date) -> dict:
    """Five Monday-to-Sunday weeks ending with this one, and the values entered on those days."""
    first = today - dt.timedelta(days=today.weekday() + 28)
    rows = db.scalars(select(RestingHeartRate).where(RestingHeartRate.athlete_id == athlete_id, RestingHeartRate.date >= first)).all()
    return {"today": today, "days": [first + dt.timedelta(days=i) for i in range(35)], "by_day": {r.date: r for r in rows}}


def resting_hr_summary(rows: list[RestingHeartRate], as_of: dt.date) -> dict | None:
    """Latest value, this week's average (3+ days), the usual (median of the four weeks before, 7+ days), and whether
    this week is RESTING_HR_FLAG or more above usual. rows: one athlete's values, any order."""
    rows = sorted((r for r in rows if r.date <= as_of), key=lambda r: r.date)
    if not rows:
        return None
    week = [r.bpm for r in rows if r.date > as_of - dt.timedelta(days=7)]
    before = [r.bpm for r in rows if as_of - dt.timedelta(days=RESTING_HR_WINDOW) < r.date <= as_of - dt.timedelta(days=7)]
    avg = round(statistics.mean(week), 1) if len(week) >= 3 else None
    usual = round(statistics.median(before), 1) if len(before) >= 7 else None
    up = round(avg - usual, 1) if avg is not None and usual is not None else None
    return {"latest": rows[-1], "week": avg, "usual": usual, "up": up, "flag": up is not None and up >= RESTING_HR_FLAG,
            "recent": [r for r in rows if r.date > as_of - dt.timedelta(days=14)][::-1]}


def resting_hr(db: Session, athlete_id: int, as_of: dt.date) -> dict | None:
    return resting_hr_summary(db.scalars(select(RestingHeartRate).where(
        RestingHeartRate.athlete_id == athlete_id, RestingHeartRate.date > as_of - dt.timedelta(days=RESTING_HR_WINDOW))).all(), as_of)


def resting_hr_flags(db: Session, as_of: dt.date) -> list[dict]:
    """Active athletes whose resting heart rate this week is well above their usual. Kept apart from the watch list,
    which goes into the AI briefing: resting heart rate is never sent to the AI."""
    rows = db.execute(select(RestingHeartRate, Athlete).join(Athlete, Athlete.id == RestingHeartRate.athlete_id)
                      .where(on_team(), RestingHeartRate.date > as_of - dt.timedelta(days=RESTING_HR_WINDOW))).all()
    by = defaultdict(list)
    people = {}
    for r, a in rows:
        by[a.id].append(r)
        people[a.id] = a
    out = []
    for aid, rs in by.items():
        sm = resting_hr_summary(rs, as_of)
        if sm and sm["flag"]:
            out.append({"athlete": people[aid], **sm})
    out.sort(key=lambda x: (-x["up"], x["athlete"].name))
    return out


def set_vdot(db: Session, a: Athlete, value: int | None, by: str, source: str) -> bool:
    """Set (or clear, with None) a coach VDOT, keeping a history entry. Returns False when nothing changed."""
    if a.vdot_override == value:
        return False
    db.add(VdotChange(athlete_id=a.id, date=team_today(), vdot=value, previous=a.vdot_override, by=by, source=source))
    a.vdot_override = value
    return True


def vdot_gains(db: Session, as_of: dt.date, days: int = 7) -> dict[int, int]:
    """athlete id -> coach VDOT points gained over the past `days` (only gains; the arrows on the VDOT page)."""
    since = as_of - dt.timedelta(days=days)
    out = {}
    changes = db.scalars(select(VdotChange).order_by(VdotChange.at)).all()
    by: dict[int, list[VdotChange]] = defaultdict(list)
    for c in changes:
        by[c.athlete_id].append(c)
    for aid, cs in by.items():
        recent = [c for c in cs if c.date > since]
        if not recent or recent[-1].vdot is None:
            continue
        before = [c for c in cs if c.date <= since]
        start = before[-1].vdot if before else recent[0].previous
        if start is not None and recent[-1].vdot > start:
            out[aid] = recent[-1].vdot - start
    return out


def achievements(db: Session, a: Athlete, as_of: dt.date) -> list[dict]:
    """Recent things to celebrate on an athlete's page: VDOT up this week, a new PR in the last two weeks, a team
    workout clearly faster than target this week, and a season-high training week without too big a jump."""
    out = []
    gain = vdot_gains(db, as_of).get(a.id)
    if gain:
        out.append({"kind": "vdot", "title": f"VDOT up {gain}", "detail": f"Your coach raised your VDOT to {a.vdot_override} this week."})
    races = db.scalars(select(RaceResult).where(RaceResult.athlete_id == a.id, RaceResult.date <= as_of).order_by(RaceResult.date)).all()
    for r in [r for r in races if r.date > as_of - dt.timedelta(days=14)][::-1]:
        earlier = [x.time_s for x in races if x.date < r.date and abs(x.distance_m - r.distance_m) <= r.distance_m * 0.01]
        if earlier and r.time_s < min(earlier):
            out.append({"kind": "pr", "title": "New PR", "detail": f"{fmt_time(r.time_s)} for {r.distance_label} at {r.meet}, "
                        f"{min(earlier) - r.time_s:.1f} s faster than before."})
            break
    acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id, Activity.local_date > as_of - dt.timedelta(days=7),
                                             Activity.local_date <= as_of, Activity.planned_workout_id.is_not(None))
                      .options(selectinload(Activity.laps), selectinload(Activity.planned))).all()
    for act in sorted(acts, key=lambda x: x.local_date, reverse=True):
        reps = [l for l in act.laps if l.is_rep and l.distance_m]
        tgt = target_pace(db, a.id, act.planned) if reps else None
        if reps and tgt:
            pace = statistics.fmean(l.timer_s / l.distance_m * METERS_PER_MILE for l in reps)
            if pace <= tgt * 0.98:
                out.append({"kind": "training", "title": "Faster than target",
                            "detail": f"{act.planned.title}: reps averaged {fmt_time(pace)[:-2]}/mi against a {fmt_time(tgt)[:-2]} target."})
                break
    start, _ = season_range(as_of)
    weeks = [w for w in week_starts(as_of, 30) if w + dt.timedelta(days=6) >= start]
    runs = db.execute(select(Activity.local_date, Activity.distance_m, Activity.sport).where(
        Activity.athlete_id == a.id, Activity.local_date >= weeks[0] if weeks else as_of, Activity.local_date <= as_of)).all()
    miles = [sum(d for day, d, sp in runs if _is_run(sp) and w <= day <= w + dt.timedelta(days=6)) / METERS_PER_MILE for w in weeks]
    if len(miles) >= 5 and miles[-1] >= 10 and miles[-1] > max(miles[:-1]):
        prior = [m for m in miles[-5:-1]]
        ratio = miles[-1] / statistics.fmean(prior) if any(prior) else None
        if ratio is not None and ratio <= LOAD_RATIO_FLAG:
            out.append({"kind": "training", "title": "Season-high week", "detail": f"{miles[-1]:.1f} miles in the past 7 days, your most this season."})
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
    rows = db.scalars(select(Athlete).where(on_team()).order_by(Athlete.id)).all()
    ab = abilities(db, as_of or team_today())
    return [{
        "athlete_id": a.code, "athlete": a.name, "gender": a.gender, "grade": a.grade, "group": a.group,
        "hr_max": a.hr_max if a.hr_max is not None else "", "hr_rest": a.hr_rest if a.hr_rest is not None else "",
        "ability_group": ab.get(a.id, {}).get("group", ""), "vdot": ab.get(a.id, {}).get("vdot") or "",
    } for a in rows]


def benchmark_rows(db: Session) -> list[dict]:
    q = select(Result, Athlete).join(Athlete, Athlete.id == Result.athlete_id).where(on_team()).order_by(Result.date, Result.id)
    return [{
        "athlete_id": a.code, "athlete": a.name, "gender": a.gender, "group": a.group, "season": r.season,
        "event": r.event, "time_sec": round(r.time_s, 1), "date": r.date.isoformat(), "course": r.course, "kind": r.kind,
    } for r, a in db.execute(q).all()]


def current_xc_season(benchmarks: list[dict], goals: list[dict] | None = None) -> str | None:
    seasons = {b["season"] for b in benchmarks if str(b["season"]).endswith(" XC")}
    seasons |= {g["season"] for g in (goals or []) if str(g.get("season", "")).endswith(" XC")}
    return max(seasons, key=lambda s: int(s.split()[0])) if seasons else None


def goal_rows(db: Session, season: str | None = None) -> list[dict]:
    q = select(Goal, Athlete).join(Athlete, Athlete.id == Goal.athlete_id).where(on_team())
    all_rows = [{"athlete_id": a.code, "event": g.event, "goal_sec": round(g.goal_s, 1), "season": g.season} for g, a in db.execute(q).all()]
    if season is None:
        season = current_xc_season(benchmark_rows(db), all_rows)
    return [r for r in all_rows if r["season"] == season]


def _injury_is_on(inj_onset: dt.date, inj_return: dt.date | None, start: dt.date, end: dt.date) -> bool:
    return inj_onset <= end and (inj_return is None or inj_return > start)


def weekly_load_rows(db: Session, as_of: dt.date, n_weeks: int = 21) -> list[dict]:
    starts = week_starts(as_of, n_weeks + 4)
    first = starts[0]
    athletes = db.scalars(select(Athlete).where(on_team()).order_by(Athlete.id)).all()
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
    q = select(Injury, Athlete).join(Athlete, Athlete.id == Injury.athlete_id).where(on_team()).order_by(Injury.onset_date)
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
         .where(on_team(), PainReport.date >= since, PainReport.date <= as_of))
    rows = [{"report_id": f"P{p.id:03d}", "athlete_id": a.code, "athlete": a.name, "date": p.date.isoformat(),
             "body_area": p.body_area, "severity": p.severity, "reason": ""} for p, a in db.execute(q).all()]
    q = (select(MissedDay, Athlete).join(Athlete, Athlete.id == MissedDay.athlete_id)
         .where(on_team(), MissedDay.date >= since, MissedDay.date <= as_of, MissedDay.reason.in_(("Injury", "Sick"))))
    for m, a in db.execute(q).all():
        where = ", ".join(x for x in (m.body_area, (m.side or "").lower()) if x)
        rows.append({"report_id": f"M{m.id:03d}", "athlete_id": a.code, "athlete": a.name, "date": m.date.isoformat(),
                     "body_area": where, "severity": m.pain if m.pain is not None else "",
                     "reason": "Missed run (injury)" if m.reason == "Injury" else "Missed run (sick)"})
    rows.sort(key=lambda r: (r["date"], r["report_id"]))
    return rows


def target_pace(db: Session, athlete_id: int, pw: PlannedWorkout) -> float | None:
    """Target pace (sec/mile): for a workout with parts, the hardest part's pace at the athlete's VDOT; for an older
    workout, the athlete's best time in the workout's pace event times its pace factor."""
    main = main_part(pw.part_list)
    if main is not None:
        key = ("abilities", pw.date)
        if key not in db.info:  # one team calculation per workout day, not one per athlete
            db.info[key] = abilities(db, pw.date)
        v = db.info[key].get(athlete_id, {}).get("vdot")
        return pace_at(v, WORKOUT_ZONES[main["zone"]]) if v and main["zone"] in WORKOUT_ZONES else None
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
    now_v = abilities(db, team_today()).get(athlete.id, {}).get("vdot") if any(w.part_list for w in workouts) else None
    for pw in workouts:
        if pw.part_list:
            gv = goal_vdot(db, athlete.id, pw.date)
            out.append({"workout": pw, "vdot_now": now_v, "vdot_goal": gv,
                        "parts": list(zip(part_paces(pw.part_list, now_v), part_paces(pw.part_list, gv)))})
            continue
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
        if pw is None or not a.active or a.is_test:
            continue
        key = (a.id, pw.id)
        if key not in cache:
            cache[key] = target_pace(db, a.id, pw)
        tgt = cache[key]
        rep_m = "" if not pw.rep_distance_m else int(pw.rep_distance_m) if float(pw.rep_distance_m).is_integer() else pw.rep_distance_m
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

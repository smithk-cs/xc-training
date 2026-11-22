"""Time trials at practice: any distance in meters, miles or kilometers, several on one day, each with its start time
and the weather then at the practice place (Jesuit High School unless the calendar's practice place is changed). Each
time shows its pace, the time adjusted for heat and humidity (as race results are, courses.adjusted_time, with no
course correction: it's the school's own track or loop), and its VDOT. Trials from 1500 m to 10K count toward VDOT
like races (analysis.latest_races)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import analysis as A
from . import courses as CS
from .models import Athlete, TimeTrial, TimeTrialResult, utcnow

UNITS = {"m": ("meters", 1.0), "mi": ("miles", CS.METERS_PER_MILE), "km": ("kilometers", 1000.0)}
LIMITS = {"m": (100, 20000), "mi": (0.1, 13.1), "km": (0.1, 21.1)}
WEATHER_DAYS = 3        # the nightly update also refreshes trials this many days back (forecast → what happened)


def distance(value, unit: str) -> tuple[float, float, str]:
    """(value, meters, unit) from the form; ValueError with a message for the coach."""
    if unit not in UNITS:
        raise ValueError("Pick meters, miles or kilometers.")
    try:
        v = float(str(value).replace(",", "").strip())
    except ValueError:
        raise ValueError("Enter the distance as a number, such as 1600, 2 or 3.") from None
    lo, hi = LIMITS[unit]
    if not lo <= v <= hi:
        raise ValueError(f"The distance should be between {lo:g} and {hi:g} {UNITS[unit][0]}.")
    return v, v * UNITS[unit][1], unit


def label(t: TimeTrial) -> str:
    """'1600 m', '2 mi', '3 km'."""
    return f"{t.distance_value:g} {t.distance_unit}"


def title(t: TimeTrial) -> str:
    return t.name or f"{label(t)} time trial"


def parse_time(text: str) -> float:
    """'5:12', '5:12.4', '17:45', '1:02:03' (or '312' seconds) -> seconds."""
    s = str(text or "").strip()
    if ":" not in s:
        try:
            v = float(s)
        except ValueError:
            raise ValueError(f"“{s}” isn’t a time like 5:12.4") from None
        if v <= 0:
            raise ValueError("Times must be positive.")
        return v
    return A.parse_time(s)


def conditions(t: TimeTrial):
    """The weather in the shape courses.adjusted_time takes, or None before it's known."""
    return SimpleNamespace(temp_f=t.temp_f, dew_point_f=t.dew_point_f, footing="") if t.temp_f is not None else None


def rows(t: TimeTrial) -> list[dict]:
    """The results, fastest first: {"r", "time", "pace" (per mile), "adjusted", "vdot", "place"} (r.athlete has the name)."""
    cond = conditions(t)
    miles = t.distance_m / CS.METERS_PER_MILE
    out = []
    for r in sorted(t.results, key=lambda r: r.time_s):
        adj = CS.adjusted_time(r.time_s, t.distance_m, None, cond) if cond else None
        v = A.vdot(t.distance_m, r.time_s) if A.VDOT_MIN_M <= t.distance_m <= A.VDOT_MAX_M else None
        out.append({"r": r, "time": r.time_s, "pace": r.time_s / miles, "vdot": v,
                    "adjusted": adj if adj and abs(adj - r.time_s) >= 0.5 else None})
    for i, x in enumerate(out, 1):
        x["place"] = i
    return out


def save_results(db: Session, t: TimeTrial, entries: dict[int, str]) -> tuple[int, list[str]]:
    """Save the times typed for a trial ({athlete id: "5:12.4"}; an empty box removes that athlete's time). Returns
    (how many changed, problems)."""
    have = {r.athlete_id: r for r in t.results}
    changed, problems = 0, []
    for aid, text in entries.items():
        text = str(text or "").strip()
        cur = have.get(aid)
        if not text:
            if cur is not None:
                t.results.remove(cur)
                changed += 1
            continue
        try:
            secs = parse_time(text)
        except ValueError as exc:
            a = db.get(Athlete, aid)
            problems.append(f"{a.name if a else aid}: {exc}")
            continue
        pace = secs / (t.distance_m / CS.METERS_PER_MILE)
        if pace < 180 or pace > 20 * 60:
            a = db.get(Athlete, aid)
            problems.append(f"{a.name if a else aid}: {text} for {label(t)} is {A.fmt_secs(pace)} a mile; check it.")
            continue
        if cur is None:
            t.results.append(TimeTrialResult(athlete_id=aid, time_s=round(secs, 1)))
            changed += 1
        elif abs(cur.time_s - secs) > 0.05:
            cur.time_s = round(secs, 1)
            changed += 1
    db.flush()
    return changed, problems


def fetch_weather(db: Session, t: TimeTrial, http: httpx.Client, today: dt.date) -> bool:
    """The weather at the practice place at the trial's start time (a forecast before, what happened after)."""
    from . import weather as WX
    lat, lon, place = WX.practice_place(db)
    w = WX.at(http, lat, lon, t.date, int((t.start_time or "15:30").split(":")[0]), today)
    t.place = place
    if w is None:
        return False
    for k in ("temp_f", "dew_point_f", "humidity_pct", "wind_mph", "rain_pct", "pressure_inhg", "summary"):
        setattr(t, k, w[k])
    from .practice_alerts import air_at
    air = air_at(http, lat, lon, t.date, int((t.start_time or "15:30").split(":")[0]), today)
    if air is not None:
        t.aqi, t.pm25 = air["aqi"], air["pm25"]
    t.weather_at = utcnow()
    return True


def refresh_weather(db: Session, http: httpx.Client, today: dt.date) -> int:
    """For the nightly update: trials in the next two weeks and the last few days."""
    from . import weather as WX
    n = 0
    for t in db.scalars(select(TimeTrial).where(TimeTrial.date >= today - dt.timedelta(days=WEATHER_DAYS),
                                                TimeTrial.date <= today + dt.timedelta(days=WX.FORECAST_DAYS))).all():
        n += fetch_weather(db, t, http, today)
    return n


def listing(db: Session, since: dt.date | None = None) -> list[TimeTrial]:
    q = select(TimeTrial).options(selectinload(TimeTrial.results).selectinload(TimeTrialResult.athlete))
    if since:
        q = q.where(TimeTrial.date >= since)
    return db.scalars(q.order_by(TimeTrial.date.desc(), TimeTrial.start_time.desc(), TimeTrial.id.desc())).all()


def for_athlete(db: Session, athlete_id: int) -> list[dict]:
    """An athlete's time trials, newest first: {"trial", "r", "pace", "adjusted", "vdot", "place", "of"}."""
    out = []
    mine = db.scalars(select(TimeTrialResult).options(selectinload(TimeTrialResult.trial).selectinload(TimeTrial.results))
                      .where(TimeTrialResult.athlete_id == athlete_id)).all()
    for r in mine:
        t = r.trial
        x = next(y for y in rows(t) if y["r"].id == r.id)
        out.append({"trial": t, **x, "of": len(t.results)})
    return sorted(out, key=lambda x: (x["trial"].date, x["trial"].start_time), reverse=True)

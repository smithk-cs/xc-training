"""Weather from Open-Meteo (open-meteo.com: free, no key, for non-commercial use): forecasts for upcoming meets, the
weather at past races for race-day conditions, and a map lookup for courses. Data by Open-Meteo, CC BY 4.0."""
from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analysis as A
from .config import settings
from .ingest import log
from .models import Course, MeetConditions, MeetEntry, MeetForecast, RaceResult, utcnow

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_DAYS = 15          # how far ahead forecasts reach
RECENT_DAYS = 80            # past days the forecast service still covers; older days come from the archive
FILL_DAYS = 30              # race-day conditions are filled in for races this recent
DEFAULT_HOUR = 9            # when a meet has no start time
HOURLY = "temperature_2m,dew_point_2m,relative_humidity_2m,wind_speed_10m,precipitation_probability,weather_code"
# WMO weather codes -> words
CODES = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog", 51: "Light drizzle",
         53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
         67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Showers", 81: "Showers",
         82: "Heavy showers", 95: "Thunderstorms", 96: "Thunderstorms", 99: "Thunderstorms"}


class WeatherError(Exception):
    pass


def make_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False)


def _get(http: httpx.Client, url: str, params: dict) -> dict:
    try:
        r = http.get(url, params=params)
    except httpx.HTTPError as exc:
        raise WeatherError(f"The weather service didn’t answer ({type(exc).__name__}).") from exc
    if r.status_code != 200:
        raise WeatherError(f"The weather service said {r.status_code}: {r.text[:160]}")
    return r.json()


def geocode(http: httpx.Client, text: str) -> tuple[float, float, str] | None:
    """A place name ('Gibson Ranch, Elverta') -> (latitude, longitude, what was found). Tries the whole text, then each
    part, preferring places in the United States."""
    tries = [text] + [p.strip() for p in text.split(",") if p.strip()][::-1]
    for q in dict.fromkeys(t for t in tries if t):
        data = _get(http, GEOCODE_URL, {"name": q, "count": 5, "language": "en", "format": "json"})
        hits = data.get("results") or []
        hits.sort(key=lambda h: h.get("country_code") != "US")
        if hits:
            h = hits[0]
            label = ", ".join(x for x in (h.get("name"), h.get("admin1"), h.get("country_code")) if x)
            return float(h["latitude"]), float(h["longitude"]), label
    return None


def start_hour(m: MeetEntry | None) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in (m.start_time or "").split(":")) if m else (DEFAULT_HOUR, 0)
        return (hh, mm) if 0 <= hh < 24 and 0 <= mm < 60 else (DEFAULT_HOUR, 0)
    except ValueError:
        return DEFAULT_HOUR, 0


def at(http: httpx.Client, lat: float, lon: float, day: dt.date, hour: int, today: dt.date) -> dict | None:
    """The weather at a place and local hour: temp_f, dew_point_f, humidity_pct, wind_mph, rain_pct (forecasts only),
    summary. None when the day is too far ahead."""
    if day > today + dt.timedelta(days=FORECAST_DAYS):
        return None
    recent = day >= today - dt.timedelta(days=RECENT_DAYS)
    params = {"latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": day.isoformat(), "end_date": day.isoformat(),
              "hourly": HOURLY if recent else HOURLY.replace(",precipitation_probability", ""),
              "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": settings().team_timezone}
    data = _get(http, FORECAST_URL if recent else ARCHIVE_URL, params)
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    key = f"{day.isoformat()}T{hour:02d}:00"
    if key not in times:
        return None
    i = times.index(key)

    def val(name):
        v = (hourly.get(name) or [None] * len(times))[i]
        return round(float(v), 1) if v is not None else None
    code = (hourly.get("weather_code") or [None] * len(times))[i]
    return {"at": key, "temp_f": val("temperature_2m"), "dew_point_f": val("dew_point_2m"), "humidity_pct": val("relative_humidity_2m"),
            "wind_mph": val("wind_speed_10m"), "rain_pct": val("precipitation_probability") if day >= today else None,
            "summary": CODES.get(int(code), "") if code is not None else ""}


def course_for(db: Session, m: MeetEntry) -> Course | None:
    """The meet's course: the one chosen for it, else a course whose name is in the meet's location or name."""
    if m.course is not None:
        return m.course
    where = f"{m.location} {m.name}".lower()
    return next((c for c in db.scalars(select(Course).order_by(func.length(Course.name).desc())).all()
                 if c.name.strip().lower() in where), None)


def refresh_forecast(db: Session, http: httpx.Client, m: MeetEntry, today: dt.date) -> bool:
    """Fetch the forecast for one upcoming meet at its start time. False when it can't (no map location, too far ahead)."""
    c = course_for(db, m)
    if c is None or c.latitude is None or c.longitude is None or not today <= m.date <= today + dt.timedelta(days=FORECAST_DAYS):
        return False
    hh, _mm = start_hour(m)
    w = at(http, c.latitude, c.longitude, m.date, hh, today)
    if w is None:
        return False
    f = m.forecast or MeetForecast(meet_id=m.id)
    if m.forecast is None:
        m.forecast = f
    f.fetched_at, f.at = utcnow(), w["at"]
    f.temp_f, f.dew_point_f, f.humidity_pct, f.wind_mph, f.rain_pct, f.summary = (
        w["temp_f"], w["dew_point_f"], w["humidity_pct"], w["wind_mph"], w["rain_pct"], w["summary"])
    return True


def refresh_forecasts(db: Session, http: httpx.Client, today: dt.date) -> int:
    meets = db.scalars(select(MeetEntry).where(MeetEntry.date >= today, MeetEntry.date <= today + dt.timedelta(days=FORECAST_DAYS))).all()
    return sum(refresh_forecast(db, http, m, today) for m in meets)


def fill_conditions(db: Session, http: httpx.Client, today: dt.date) -> int:
    """For recent races with results, fill in race-day temperature, dew point, humidity and wind from the weather at
    the start time. Only empty fields are filled: anything a coach entered stays."""
    n = 0
    since = today - dt.timedelta(days=FILL_DAYS)
    raced = db.execute(select(RaceResult.meet, RaceResult.date, func.max(RaceResult.course)).where(
        RaceResult.date >= since, RaceResult.date < today).group_by(RaceResult.meet, RaceResult.date)).all()
    for meet, day, course_name in raced:
        cond = db.scalar(select(MeetConditions).where(MeetConditions.meet == meet, MeetConditions.date == day))
        if cond is not None and None not in (cond.temp_f, cond.dew_point_f, cond.wind_mph):
            continue
        entry = db.scalar(select(MeetEntry).where(MeetEntry.date == day, MeetEntry.name == meet))
        course = (cond.course if cond and cond.course else None) or (course_for(db, entry) if entry else None) or \
            (db.scalar(select(Course).where(func.lower(Course.name) == (course_name or "").strip().lower())) if course_name else None) or \
            db.scalar(select(Course).where(func.lower(Course.name) == meet.strip().lower()))
        if course is None or course.latitude is None or course.longitude is None:
            continue
        hh, _mm = start_hour(entry)
        w = at(http, course.latitude, course.longitude, day, hh, today)
        if w is None:
            continue
        if cond is None:
            cond = MeetConditions(meet=meet, date=day, course_id=course.id)
            db.add(cond)
        for field in ("temp_f", "dew_point_f", "humidity_pct", "wind_mph"):
            if getattr(cond, field) is None and w[field] is not None:
                setattr(cond, field, w[field])
        note = f"Weather at {hh:02d}:00 from Open-Meteo."
        if note not in (cond.notes or ""):
            cond.notes = ((cond.notes or "") + " " + note).strip()
        n += 1
    return n


def nightly(db: Session, http: httpx.Client | None = None) -> dict:
    """Forecasts for the next two weeks of meets, and conditions for recent races. Errors are logged, not raised."""
    today = A.team_today()
    client = http or make_client()
    out = {}
    try:
        out["forecasts"] = refresh_forecasts(db, client, today)
        out["race_conditions"] = fill_conditions(db, client, today)
    except WeatherError as exc:
        out["error"] = str(exc)
        log(db, "weather", f"Weather update failed: {exc}", "error")
    finally:
        if http is None:
            client.close()
    return out


def describe(f) -> str:
    """'68°F, partly cloudy, 10% chance of rain, wind 6 mph, dew point 52°F'"""
    if f is None or f.temp_f is None:
        return ""
    bits = [f"{f.temp_f:.0f}°F"]
    if f.summary:
        bits.append(f.summary.lower())
    if f.rain_pct is not None and f.rain_pct >= 10:
        bits.append(f"{f.rain_pct:.0f}% chance of rain")
    if f.wind_mph is not None:
        bits.append(f"wind {f.wind_mph:.0f} mph")
    if f.dew_point_f is not None:
        bits.append(f"dew point {f.dew_point_f:.0f}°F")
    return ", ".join(bits)

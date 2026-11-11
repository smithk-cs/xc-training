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
from .db import page_cache
from .models import (CalendarEvent, Course, MeetConditions, MeetEntry, MeetForecast, PlannedWorkout, PracticeForecast, RaceResult,
                     RaceWeather, utcnow)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_DAYS = 15          # how far ahead forecasts reach
RECENT_DAYS = 80            # past days the forecast service still covers; older days come from the archive
FILL_DAYS = 30              # race-day conditions are filled in nightly for races this recent
IMPORT_DAYS = 400           # ... and for older ones right after results are imported or a past meet is saved
ALL_PAST_DAYS = 10 * 366    # "Fill in race-day weather" and the nightly catch-up reach back this far
CATCH_UP_PER_NIGHT = 25     # older races filled in each night, a few at a time
DEFAULT_HOUR = 9            # when a meet has no start time
HOURLY = "temperature_2m,dew_point_2m,relative_humidity_2m,wind_speed_10m,pressure_msl,precipitation_probability,weather_code"
HPA_TO_INHG = 0.0295300   # sea-level pressure in hPa -> inches of mercury, as airports report the altimeter setting
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
    msl = (hourly.get("pressure_msl") or [None] * len(times))[i]
    return {"at": key, "pressure_inhg": round(float(msl) * HPA_TO_INHG, 2) if msl is not None else None,
            "temp_f": val("temperature_2m"), "dew_point_f": val("dew_point_2m"), "humidity_pct": val("relative_humidity_2m"),
            "wind_mph": val("wind_speed_10m"), "rain_pct": val("precipitation_probability") if day >= today else None,
            "summary": CODES.get(int(code), "") if code is not None else ""}


def course_for(db: Session, m: MeetEntry) -> Course | None:
    """The meet's course: the one chosen for it, else a course whose name is in the meet's location or name."""
    if m.course is not None:
        return m.course
    where = f"{m.location} {m.name}".lower()
    return next((c for c in _courses(db) if c.name.strip().lower() in where), None)


def _courses(db: Session) -> list[Course]:
    """Every saved course, longest name first, read once per page."""
    cache = page_cache(db)
    if "courses" not in cache:
        cache["courses"] = db.scalars(select(Course).order_by(func.length(Course.name).desc())).all()
    return cache["courses"]


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
    f.temp_f, f.dew_point_f, f.humidity_pct, f.wind_mph, f.rain_pct, f.summary, f.pressure_inhg = (
        w["temp_f"], w["dew_point_f"], w["humidity_pct"], w["wind_mph"], w["rain_pct"], w["summary"], w["pressure_inhg"])
    return True


def refresh_forecasts(db: Session, http: httpx.Client, today: dt.date) -> int:
    meets = db.scalars(select(MeetEntry).where(MeetEntry.date >= today, MeetEntry.date <= today + dt.timedelta(days=FORECAST_DAYS))).all()
    return sum(refresh_forecast(db, http, m, today) for m in meets)


def _words(s: str) -> list[str]:
    """A meet name without years and punctuation, for matching 'Clovis Invitational 2026' to 'Clovis Invitational'."""
    import re
    return re.sub(r"\b(19|20)\d\d\b|[^a-z0-9]+", " ", (s or "").lower()).split()


def entry_for(db: Session, meet: str, day: dt.date) -> MeetEntry | None:
    """The Entries-page meet that results belong to: same date, and the same name give or take a year or a word;
    else the only meet that day."""
    cache = page_cache(db).setdefault("meets_on", {})
    if day not in cache:
        cache[day] = db.scalars(select(MeetEntry).where(MeetEntry.date == day)).all()
    same_day = cache[day]
    want = set(_words(meet))
    for m in same_day:
        have = set(_words(m.name))
        if have == want or (have and want and (have <= want or want <= have)):
            return m
    return same_day[0] if len(same_day) == 1 else None


def course_named(db: Session, *names: str) -> Course | None:
    """A saved course whose name matches one of these, or is part of one ('Woodward Park' in 'Clovis at Woodward Park')."""
    courses = _courses(db)
    for name in (n for n in names if n):
        low = name.strip().lower()
        hit = next((c for c in courses if c.name.strip().lower() == low), None) or \
            next((c for c in courses if c.name.strip().lower() in low), None)
        if hit:
            return hit
    return None


def result_course(db: Session, meet: str, day: dt.date, course_name: str = "", cond: MeetConditions | None = None) -> Course | None:
    """The course a race was run on: the one on its race-day conditions, else the course named in the results, else
    the course of its meet on the Entries page, else a saved course named in the meet's name."""
    if cond is not None and cond.course is not None:
        return cond.course
    entry = entry_for(db, meet, day)
    return course_named(db, course_name) or (course_for(db, entry) if entry else None) or course_named(db, meet)


def fill_conditions(db: Session, http: httpx.Client, today: dt.date, days: int = FILL_DAYS, limit: int | None = None) -> int:
    """For races with results in the past `days`, fill in race-day temperature, dew point, humidity and wind from the
    weather at the meet's start time (9:00 when unknown). Only empty fields are filled: anything a coach entered stays."""
    n = 0
    since = today - dt.timedelta(days=days)
    raced = db.execute(select(RaceResult.meet, RaceResult.date, func.max(RaceResult.course)).where(
        RaceResult.date >= since, RaceResult.date <= today).group_by(RaceResult.meet, RaceResult.date)).all()
    for meet, day, course_name in raced:
        cond = db.scalar(select(MeetConditions).where(MeetConditions.meet == meet, MeetConditions.date == day))
        if cond is not None and None not in (cond.temp_f, cond.dew_point_f, cond.wind_mph, cond.pressure_inhg):
            continue
        entry = entry_for(db, meet, day)
        course = result_course(db, meet, day, course_name, cond)
        if course is None or course.latitude is None or course.longitude is None:
            continue
        if limit is not None and n >= limit:
            break
        hh, _mm = start_hour(entry)
        w = at(http, course.latitude, course.longitude, day, hh, today)
        if w is None:
            continue
        if cond is None:
            cond = MeetConditions(meet=meet, date=day, course_id=course.id)
            db.add(cond)
        elif cond.course_id is None:
            cond.course_id = course.id
        for field in ("temp_f", "dew_point_f", "humidity_pct", "wind_mph", "pressure_inhg"):
            if getattr(cond, field) is None and w[field] is not None:
                setattr(cond, field, w[field])
        note = f"Weather at {hh:02d}:00 from Open-Meteo."
        if note not in (cond.notes or ""):
            cond.notes = ((cond.notes or "") + " " + note).strip()
        n += 1
    # races with their own start time
    from sqlalchemy import or_
    for rw in db.scalars(select(RaceWeather).where(RaceWeather.date >= since, RaceWeather.date <= today, RaceWeather.start_time != "",
                                                   or_(RaceWeather.temp_f.is_(None), RaceWeather.pressure_inhg.is_(None)))).all():
        if limit is not None and n >= limit:
            break
        name = db.scalar(select(RaceResult.course).where(RaceResult.meet == rw.meet, RaceResult.date == rw.date,
                                                         RaceResult.race == rw.race).limit(1)) or ""
        n += fill_race(db, http, rw, today, name)
    return n


FIELDS = ("temp_f", "dew_point_f", "humidity_pct", "wind_mph", "pressure_inhg")


def race_weather(db: Session) -> dict[tuple, RaceWeather]:
    """(meet, date, race) -> that race's own start time and weather."""
    return {(w.meet, w.date, w.race): w for w in db.scalars(select(RaceWeather)).all()}


def merged(cond: MeetConditions | None, rw: RaceWeather | None):
    """The conditions a race's adjusted times use: its own weather at its start time when known, with the meet's
    footing and course; otherwise the meet's race-day conditions."""
    if rw is None or rw.temp_f is None:
        return cond
    from types import SimpleNamespace
    return SimpleNamespace(**{f: getattr(rw, f) for f in FIELDS}, footing=cond.footing if cond else "",
                           course=cond.course if cond else None, notes=cond.notes if cond else "")


def fill_race(db: Session, http: httpx.Client, rw: RaceWeather, today: dt.date, course_name: str = "") -> bool:
    """Fetch the weather at one race's start time. False when its course has no map location or there's no data."""
    cond = db.scalar(select(MeetConditions).where(MeetConditions.meet == rw.meet, MeetConditions.date == rw.date))
    course = result_course(db, rw.meet, rw.date, course_name, cond)
    if course is None or course.latitude is None or course.longitude is None or not rw.start_time:
        return False
    w = at(http, course.latitude, course.longitude, rw.date, int(rw.start_time.split(":")[0]), today)
    if w is None:
        return False
    for f in FIELDS:
        setattr(rw, f, w[f])
    rw.fetched_at = utcnow()
    return True


def fill_now(db: Session, days: int = FILL_DAYS, http: httpx.Client | None = None) -> str:
    """Fill race-day conditions straight away (after an import or a meet edit). Returns a short note; never raises."""
    client = http or make_client()
    try:
        n = fill_conditions(db, client, A.team_today(), days)
        return f" Race-day weather filled in for {n} race{'' if n == 1 else 's'}." if n else ""
    except WeatherError as exc:
        log(db, "weather", f"Race-day weather not filled in: {exc}", "error")
        return " (Race-day weather couldn’t be fetched right now; it will be tried again tonight.)"
    finally:
        if http is None:
            client.close()


def nightly(db: Session, http: httpx.Client | None = None, days: int = FILL_DAYS) -> dict:
    """Forecasts for the next two weeks of meets, and conditions for recent races. Errors are logged, not raised."""
    today = A.team_today()
    client = http or make_client()
    out = {}
    try:
        out["forecasts"] = refresh_forecasts(db, client, today)
        out["practice_forecasts"] = refresh_practice_forecasts(db, client, today)
        out["race_conditions"] = fill_conditions(db, client, today, days)
        # older races still missing anything (such as pressure), a few each night
        out["race_conditions"] += fill_conditions(db, client, today, ALL_PAST_DAYS, limit=CATCH_UP_PER_NIGHT)
    except WeatherError as exc:
        out["error"] = str(exc)
        log(db, "weather", f"Weather update failed: {exc}", "error")
    finally:
        if http is None:
            client.close()
    return out


def describe(f, dew: bool = True, humidity: bool = False) -> str:
    """'68°F, partly cloudy, 10% chance of rain, wind 6 mph, dew point 52°F' (athletes see humidity instead of dew point:
    '68°F, partly cloudy, 45% humidity, ...')"""
    if f is None or f.temp_f is None:
        return ""
    bits = [f"{f.temp_f:.0f}°F"]
    if f.summary:
        bits.append(f.summary.lower())
    if humidity and getattr(f, "humidity_pct", None) is not None:
        bits.append(f"{f.humidity_pct:.0f}% humidity")
    if f.rain_pct is not None and f.rain_pct >= 10:
        bits.append(f"{f.rain_pct:.0f}% chance of rain")
    if f.wind_mph is not None:
        bits.append(f"wind {f.wind_mph:.0f} mph")
    if dew and f.dew_point_f is not None:
        bits.append(f"dew point {f.dew_point_f:.0f}°F")
    return ", ".join(bits)   # pressure is recorded for races already run, not shown in forecasts



# ---------- practice forecasts (for upcoming workouts) ----------

# Jesuit High School, Carmichael, CA: the center of Carmichael (weather data is on a grid a few kilometers wide).
DEFAULT_PRACTICE = (38.6171, -121.3283, "Jesuit High School, Carmichael")
DEFAULT_PRACTICE_TIME = "15:30"


SKY_ICONS = (("thunder", "⛈️"), ("snow", "❄️"), ("rain", "🌧️"), ("shower", "🌧️"), ("drizzle", "🌦️"), ("fog", "🌫️"),
             ("overcast", "☁️"), ("cloud", "⛅"), ("clear", "☀️"), ("sun", "☀️"))


def short(f) -> dict | None:
    """Just the temperature and the sky, for the team calendar: {'icon': '⛅', 'text': '68°F, partly cloudy'}."""
    if f is None or f.temp_f is None:
        return None
    sky = (f.summary or "").lower()
    icon = next((i for k, i in SKY_ICONS if k in sky), "🌡️")
    return {"icon": icon, "text": f"{f.temp_f:.0f}°F" + (f", {sky}" if sky else "")}


def practice_place(db: Session) -> tuple[float, float, str]:
    try:
        lat = float(A.get_setting(db, "practice_lat", ""))
        lon = float(A.get_setting(db, "practice_lon", ""))
        return lat, lon, A.get_setting(db, "practice_place", "") or "Practice"
    except ValueError:
        return DEFAULT_PRACTICE


def practice_slot(db: Session, day: dt.date, team: str) -> dict:
    """When and where practice is that day for a team ('Boys', 'Girls' or 'All'): the team calendar's practice
    (its start time, and its place when that's a saved course on the map), else the usual time at the usual place."""
    from .team_calendar import days_of, for_team
    lat, lon, label = practice_place(db)
    found = []
    for e in db.scalars(select(CalendarEvent).where(CalendarEvent.kind == "Practice")).all():
        if e.start_time and for_team(e.team, team) and days_of(e, day, day):
            found.append(e)
    if found:
        e = min(found, key=lambda x: x.start_time)
        c = course_named(db, e.location) if e.location else None
        if c is not None and c.latitude is not None and c.longitude is not None:
            lat, lon, label = c.latitude, c.longitude, c.name
        elif e.location:
            label = e.location
        return {"time": e.start_time, "lat": lat, "lon": lon, "where": label, "from_calendar": True}
    return {"time": DEFAULT_PRACTICE_TIME, "lat": lat, "lon": lon, "where": label, "from_calendar": False}


def practice_forecast(db: Session, day: dt.date, team: str) -> dict | None:
    """The saved forecast for that day's practice (no network): the slot plus the weather, or None."""
    s = practice_slot(db, day, team)
    f = db.scalar(select(PracticeForecast).where(PracticeForecast.date == day, PracticeForecast.start_time == s["time"],
                                                 PracticeForecast.lat == round(s["lat"], 3), PracticeForecast.lon == round(s["lon"], 3)))
    return ({**s, "forecast": f, "text": describe(f), "athlete_text": describe(f, dew=False, humidity=True)}
            if f is not None and f.temp_f is not None else None)


def refresh_practice_forecasts(db: Session, http: httpx.Client, today: dt.date, only: dt.date | None = None) -> int:
    """Forecasts for practice on every day with a planned workout or a practice on the team calendar in the next two
    weeks (or just `only`), for boys and girls."""
    from .team_calendar import days_of
    last = today + dt.timedelta(days=FORECAST_DAYS)
    days = {d for (d,) in db.execute(select(PlannedWorkout.date).where(
        PlannedWorkout.date >= today, PlannedWorkout.date <= last).distinct()).all()}
    for e in db.scalars(select(CalendarEvent).where(CalendarEvent.kind == "Practice")).all():
        days.update(days_of(e, today, last))
    days = sorted(d for d in days if only is None or d == only)
    n = 0
    for d in days:
        for s in {(x["time"], round(x["lat"], 3), round(x["lon"], 3)): x for x in (practice_slot(db, d, t) for t in ("Boys", "Girls"))}.values():
            w = at(http, s["lat"], s["lon"], d, int(s["time"].split(":")[0]), today)
            if w is None:
                continue
            key = (PracticeForecast.date == d, PracticeForecast.start_time == s["time"], PracticeForecast.lat == round(s["lat"], 3),
                   PracticeForecast.lon == round(s["lon"], 3))
            f = db.scalar(select(PracticeForecast).where(*key))
            if f is None:
                f = PracticeForecast(date=d, start_time=s["time"], lat=round(s["lat"], 3), lon=round(s["lon"], 3))
                db.add(f)
            for k in ("temp_f", "dew_point_f", "humidity_pct", "wind_mph", "rain_pct", "pressure_inhg", "summary"):
                setattr(f, k, w[k])
            f.fetched_at = utcnow()
            n += 1
    return n

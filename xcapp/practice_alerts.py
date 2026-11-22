"""Smoke and heat alerts for practice: the air quality (US AQI, from Open-Meteo's air-quality forecast) and the heat
index at practice time, rated with common school-sports guidance, shown wherever the practice forecast is, and sent
to the team the evening before (and again early that morning if it got worse). The school's own policy comes first;
these levels are a prompt to check it.

Levels: "caution" (adjust), "warning" (move indoors or cut back hard), "stop" (no outdoor practice).
"""
from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .config import settings
from .ingest import log

AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
AIR_DAYS = 4                     # how far ahead the air-quality forecast is worth fetching
PAST_DAYS = 90                   # ... and how far back the service has what the air was (for time trials)
PRACTICE_HOURS = 2               # the worst hour from the start of practice through this many hours
RANK = {"": 0, "caution": 1, "warning": 2, "stop": 3}
LABEL = {"caution": "Caution", "warning": "Warning", "stop": "No outdoor practice"}
MORNING_HOUR = 6                 # team time: the morning re-check (the evening one is at REMINDER_HOUR)

# (lowest AQI, level, what it means, what to do)
AQI_LEVELS = [
    (201, "stop", "Very unhealthy air (smoke)", "No outdoor practice. Move indoors or cancel."),
    (151, "warning", "Unhealthy air (smoke)", "Move practice indoors, or keep it short and easy; no hard efforts outside."),
    (101, "caution", "Air unhealthy for sensitive groups", "Athletes with asthma or breathing problems run indoors or easy; "
                                                          "shorten hard efforts for everyone."),
]
# (lowest heat index °F, level, what it means, what to do)
HEAT_LEVELS = [
    (105, "stop", "Dangerous heat", "No outdoor practice in the heat. Move it to early morning or indoors."),
    (95, "warning", "High heat", "Shorter, easier practice, early or in shade; water breaks every 15 minutes; "
                                  "no hard workout outside."),
    (90, "caution", "Heat", "Extra water breaks, lighter clothing, watch for heat illness."),
]


def heat_index(temp_f: float | None, rh: float | None) -> float | None:
    """The National Weather Service heat index (°F): how hot it feels with the humidity. Below 80 °F it is about the
    temperature."""
    if temp_f is None or rh is None:
        return None
    t, r = temp_f, rh
    simple = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + r * 0.094)
    if (simple + t) / 2 < 80:
        return round(simple, 1)
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r - 0.00683783 * t * t - 0.05481717 * r * r
          + 0.00122874 * t * t * r + 0.00085282 * t * r * r - 0.00000199 * t * t * r * r)
    if r < 13 and 80 <= t <= 112:
        hi -= ((13 - r) / 4) * ((17 - abs(t - 95)) / 17) ** 0.5
    elif r > 85 and 80 <= t <= 87:
        hi += ((r - 85) / 10) * ((87 - t) / 5)
    return round(hi, 1)


def _level(value, levels):
    if value is None:
        return None
    for lo, level, what, advice in levels:
        if value >= lo:
            return {"level": level, "what": what, "advice": advice}
    return None


def assess(f) -> dict | None:
    """The alert for a practice forecast (PracticeForecast), or None when the air and heat are fine:
    {"level", "label", "reasons": [{"level", "what", "advice", "value"}], "advice", "aqi", "heat_index"}."""
    if f is None:
        return None
    hi = heat_index(f.temp_f, f.humidity_pct)
    reasons = []
    air = _level(f.aqi, AQI_LEVELS)
    if air:
        reasons.append({**air, "value": f"AQI {f.aqi:.0f}"})
    heat = _level(hi, HEAT_LEVELS)
    if heat:
        reasons.append({**heat, "value": f"feels like {hi:.0f}°F"})
    if not reasons:
        return None
    reasons.sort(key=lambda x: -RANK[x["level"]])
    top = reasons[0]["level"]
    return {"level": top, "label": LABEL[top], "reasons": reasons, "aqi": f.aqi, "heat_index": hi,
            "summary": "; ".join(f"{x['what']} ({x['value']})" for x in reasons),
            "advice": " ".join(x["advice"] for x in reasons)}


def air_at(http: httpx.Client, lat: float, lon: float, day: dt.date, hour: int, today: dt.date) -> dict | None:
    """The worst US AQI and PM2.5 from the start of practice through PRACTICE_HOURS: {"aqi", "pm25"}, or None when the
    forecast doesn't reach that day."""
    from .weather import WeatherError, _get
    if not today - dt.timedelta(days=PAST_DAYS) <= day <= today + dt.timedelta(days=AIR_DAYS):
        return None
    try:
        data = _get(http, AIR_URL, {"latitude": round(lat, 4), "longitude": round(lon, 4), "hourly": "us_aqi,pm2_5",
                                    "start_date": day.isoformat(), "end_date": day.isoformat(),
                                    "timezone": settings().team_timezone})
    except WeatherError:
        return None
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    want = {f"{day.isoformat()}T{h:02d}:00" for h in range(hour, min(24, hour + PRACTICE_HOURS + 1))}
    aqi = [v for t, v in zip(times, hourly.get("us_aqi") or []) if t in want and v is not None]
    pm = [v for t, v in zip(times, hourly.get("pm2_5") or []) if t in want and v is not None]
    if not aqi and not pm:
        return None
    return {"aqi": round(max(aqi)) if aqi else None, "pm25": round(max(pm), 1) if pm else None}


# ---------- telling the team ----------

def _sent_key(day: dt.date, team: str) -> str:
    return f"practice_alert:{day.isoformat()}:{team}"


def upcoming(db: Session, today: dt.date) -> list[tuple[dt.date, str, dict]]:
    """Today's and tomorrow's practice alerts, [(day, team, alert)], for the home page. One look at the saved
    forecasts first, so a day with nothing to report costs a single query."""
    from .models import PracticeForecast
    days = [today, today + dt.timedelta(days=1)]
    saved = db.scalars(select(PracticeForecast).where(PracticeForecast.date.in_(days))).all()
    if not any(assess(f) for f in saved):
        return []
    return [(d, team, al) for d in days for team, al in for_day(db, d).items()]


def for_day(db: Session, day: dt.date) -> dict[str, dict]:
    """Each team's practice alert that day, from the saved forecasts: {"Boys": {..., "slot"}, "Girls": ...}."""
    from . import weather as WX
    out = {}
    for team in ("Boys", "Girls"):
        fc = WX.practice_forecast(db, day, team)
        alert = fc.get("alert") if fc else None
        if alert:
            out[team] = {**alert, "slot": fc}
    return out


def notify(db: Session, day: dt.date, when: str, http: httpx.Client | None = None) -> dict:
    """Refresh that day's practice forecast and tell each team whose practice has an alert: a phone notification to
    the athletes who have them on, and an email to the coaches. "evening" (the day before) tells every alert;
    "morning" (that day) only one that got worse than what was already sent. Returns what was sent."""
    from . import push as PU
    from . import weather as WX
    from .models import Athlete, StaffUser
    client = http or WX.make_client()
    out = {"teams": [], "pushed": 0, "emailed": 0}
    try:
        WX.refresh_practice_forecasts(db, client, A.team_today(), only=day)
    except WX.WeatherError as exc:
        log(db, "weather", f"Practice alert check failed: {exc}", "error")
        return out
    finally:
        if http is None:
            client.close()
    s = settings()
    alerts = for_day(db, day)
    for team, al in alerts.items():
        before = A.get_setting(db, _sent_key(day, team), "")
        if RANK[al["level"]] <= RANK.get(before, 0):      # already told (the evening before, or earlier)
            continue
        A.set_setting(db, _sent_key(day, team), al["level"])
        out["teams"].append(team)
        title = f"{al['label']}: {team.lower()}' practice {'today' if when == 'morning' else 'tomorrow'}"
        body = f"{al['summary']}. Check with your coach before practice."
        ids = [a.id for a in db.scalars(select(Athlete).where(Athlete.active.is_(True), Athlete.is_test.is_(False),
                                                              Athlete.gender == team)).all()]
        for aid in ids:
            a = db.get(Athlete, aid)
            out["pushed"] += PU.send(db, [aid], title, body, f"{s.base_url.rstrip('/')}/a/{a.link_token}")["sent"]
        if s.email_configured:
            from . import reminders as RM
            coaches = [u for u in db.scalars(select(StaffUser).where(StaffUser.role.in_(("admin", "coach")))).all() if u.email]
            for u in coaches:
                try:
                    RM.send_email(u.email, *compose_coach(al, team, day, when), reply=RM.reply_to(db))
                    out["emailed"] += 1
                except (RM.EmailError, httpx.HTTPError) as exc:
                    log(db, "email", f"Practice alert to {u.username} not sent: {exc}", "error")
    if out["teams"]:
        log(db, "weather", f"Practice alert ({when}) for {', '.join(out['teams'])} on {day:%b %-d}: "
                           f"{out['pushed']} phone notifications, {out['emailed']} coach emails.")
    return out


def compose_coach(al: dict, team: str, day: dt.date, when: str) -> tuple[str, str, str]:
    from html import escape
    s = settings()
    slot = al["slot"]
    subject = f"{s.team_name}: {al['label'].lower()} for {team.lower()}' practice {day:%a %b %-d}"
    lines = [f"{team}' practice {'today' if when == 'morning' else 'tomorrow'} ({day:%A %b %-d}, {slot['time']} at {slot['where']}):",
             "", al["summary"] + ".", "", "Suggested: " + al["advice"], "",
             "Based on common school-sports guidance for air quality and heat; follow the school's own policy.",
             f"Athletes with phone notifications on were told too. {s.base_url.rstrip('/')}/workouts", "", s.team_name]
    html = (f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;color:#1B1B1F">'
            f'<div style="background:#A6192E;color:#fff;padding:14px 18px;border-bottom:4px solid #C99A2E;font-weight:bold;font-size:18px">{escape(s.team_name)}</div>'
            f'<div style="padding:16px 18px"><p><b>{escape(al["label"])}</b>: {escape(team)}’ practice {"today" if when == "morning" else "tomorrow"}, '
            f'{day:%A %b %-d} at {escape(slot["time"])}, {escape(slot["where"])}.</p><p>{escape(al["summary"])}.</p>'
            f'<p><b>Suggested:</b> {escape(al["advice"])}</p>'
            f'<p style="font-size:12px;color:#6B645C">Based on common school-sports guidance for air quality and heat; follow the '
            f'school’s own policy. Athletes with phone notifications on were told too.</p></div></div>')
    return subject, "\n".join(lines), html

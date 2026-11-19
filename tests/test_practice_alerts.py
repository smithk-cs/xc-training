"""Smoke and heat alerts for practice (practice_alerts.py): the heat index, the levels, the air-quality forecast, and
telling the team the evening before and (only if worse) that morning. Names and addresses are made up."""
import datetime as dt
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import practice_alerts as PA
from xcapp import push as PU
from xcapp import weather as WX
from xcapp.db import SessionLocal
from xcapp.models import Athlete, PlannedWorkout, PracticeForecast, StaffUser
from xcapp.security import hash_password

TODAY = A.team_today()
TOMORROW = TODAY + dt.timedelta(days=1)


def service(state):
    """The weather and air-quality services: temperature and humidity at practice from state["temp"]/["rh"], and the
    AQI at 3, 4 and 5 p.m. from state["aqi"] (a list)."""
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        day = q["start_date"]
        times = [f"{day}T{h:02d}:00" for h in range(24)]
        if request.url.host.startswith("air-quality"):
            state["air_calls"] = state.get("air_calls", 0) + 1
            aqi = [20] * 24
            pm = [5.0] * 24
            for i, v in enumerate(state["aqi"]):
                aqi[15 + i] = v
                pm[15 + i] = v / 3
            return httpx.Response(200, json={"hourly": {"time": times, "us_aqi": aqi, "pm2_5": pm}})
        hourly = {"time": times, "temperature_2m": [state["temp"]] * 24, "dew_point_2m": [50.0] * 24,
                  "relative_humidity_2m": [state["rh"]] * 24, "wind_speed_10m": [5.0] * 24, "weather_code": [0] * 24,
                  "pressure_msl": [1013.0] * 24, "precipitation_probability": [0] * 24}
        return httpx.Response(200, json={"hourly": hourly})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_heat_index_and_levels():
    assert PA.heat_index(90, 60) == pytest.approx(100, abs=1.5)        # NWS table: 90 °F at 60% feels like 100
    assert PA.heat_index(96, 40) == pytest.approx(101, abs=1.5)
    assert PA.heat_index(70, 50) == pytest.approx(69.6, abs=1)          # cool: about the temperature
    f = PracticeForecast(temp_f=72, humidity_pct=40, aqi=45)
    assert PA.assess(f) is None
    assert PA.assess(PracticeForecast(temp_f=72, humidity_pct=40, aqi=120))["level"] == "caution"
    assert PA.assess(PracticeForecast(temp_f=72, humidity_pct=40, aqi=160))["level"] == "warning"
    smoke = PA.assess(PracticeForecast(temp_f=72, humidity_pct=40, aqi=230))
    assert smoke["level"] == "stop" and smoke["label"] == "No outdoor practice" and "AQI 230" in smoke["summary"]
    hot = PA.assess(PracticeForecast(temp_f=99, humidity_pct=35, aqi=110))   # feels like ~102: warning; the air: caution
    assert hot["level"] == "warning" and [r["level"] for r in hot["reasons"]] == ["warning", "caution"]
    assert "water breaks" in hot["advice"] and "asthma" in hot["advice"]


def test_air_quality_is_the_worst_hour_of_practice():
    state = {"aqi": [80, 155, 120], "temp": 70, "rh": 40}
    with service(state) as http:
        assert PA.air_at(http, 38.6, -121.3, TOMORROW, 15, TODAY) == {"aqi": 155, "pm25": pytest.approx(51.7, abs=0.1)}
        assert PA.air_at(http, 38.6, -121.3, TODAY + dt.timedelta(days=9), 15, TODAY) is None     # too far ahead
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy")))
    assert PA.air_at(down, 38.6, -121.3, TOMORROW, 15, TODAY) is None


def team(db):
    db.add(StaffUser(username="coach1", role="coach", email="coach@example.org", password_hash=hash_password("correct-horse-1")))
    db.add(PlannedWorkout(date=TOMORROW, title="Tempo", rep_distance_m=1600, pace_event="5K", pace_factor=1.0, groups="All"))
    db.add(PlannedWorkout(date=TODAY, title="Easy", rep_distance_m=1600, pace_event="5K", pace_factor=1.0, groups="All"))
    for i, (first, g) in enumerate((("Sam", "Boys"), ("Ava", "Girls"))):
        db.add(Athlete(first_name=first, last_name="Lowry", gender=g, grade=11, group="Varsity", link_token=f"t{i}"))
    db.flush()


def test_evening_and_morning_notifications(session, monkeypatch):
    team(session)
    pushed, emailed = [], []
    monkeypatch.setattr(PU, "send", lambda db, ids, title, body, url, http=None: pushed.append((ids, title, body)) or {"sent": 1, "gone": 0, "failed": 0})
    from xcapp import config
    from xcapp import reminders as RM
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("EMAIL_FROM", "Team XC <team@example.org>")
    config.reload_settings()
    monkeypatch.setattr(RM, "send_email", lambda to, subj, text, html, http=None, reply="", attachments=None: emailed.append((to, subj, text)) or "id")
    state = {"aqi": [130, 140, 135], "temp": 75, "rh": 40}
    with service(state) as http:
        out = PA.notify(session, TOMORROW, "evening", http=http)
    assert out["teams"] == ["Boys", "Girls"] and out["pushed"] == 2 and out["emailed"] == 2
    assert pushed[0][1] == "Caution: boys' practice tomorrow" and "AQI 140" in pushed[0][2]
    assert "Suggested:" in emailed[0][2] and emailed[0][0] == "coach@example.org"
    with service(state) as http:                                    # the same again: nothing new to say
        assert PA.notify(session, TOMORROW, "evening", http=http)["teams"] == []
    state["aqi"] = [150, 140, 140]
    with service(state) as http:                                    # next morning, about the same: quiet
        assert PA.notify(session, TOMORROW, "morning", http=http)["teams"] == []
    state["aqi"] = [180, 210, 190]
    with service(state) as http:                                    # worse overnight: tell them
        out = PA.notify(session, TOMORROW, "morning", http=http)
    assert out["teams"] == ["Boys", "Girls"] and pushed[-1][1] == "No outdoor practice: girls' practice today"
    config.reload_settings()


def test_shown_on_pages(monkeypatch):
    db = SessionLocal()
    team(db)
    state = {"aqi": [160, 170, 160], "temp": 98, "rh": 30}
    with service(state) as http:
        WX.refresh_practice_forecasts(db, http, TODAY)
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    home = c.get("/").text
    assert "Smoke and heat at practice" in home and "Tomorrow, Boys" in home and "AQI 170" in home
    assert "Move practice indoors" in c.get("/workouts").text
    athlete = TestClient(app).get("/a/t0").text
    assert "practice-alert" in athlete and "AQI 170" in athlete

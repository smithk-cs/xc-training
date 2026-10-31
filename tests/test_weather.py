"""Weather from Open-Meteo: course map lookup, forecasts for upcoming meets, race-day conditions for past races."""
import datetime as dt
import re

import httpx
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import weather as WX
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Course, MeetConditions, MeetEntry, RaceResult, StaffUser
from xcapp.security import hash_password

TODAY = A.team_today()


def fake_service(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        q = request.url.params
        if request.url.host.startswith("geocoding"):
            if q["name"] == "Elverta":
                return httpx.Response(200, json={"results": [{"name": "Elverta", "admin1": "California", "country_code": "US",
                                                              "latitude": 38.71, "longitude": -121.46}]})
            return httpx.Response(200, json={})
        day = q["start_date"]
        times = [f"{day}T{h:02d}:00" for h in range(24)]
        hourly = {"time": times, "temperature_2m": [50 + h for h in range(24)], "dew_point_2m": [45.0] * 24,
                  "relative_humidity_2m": [70.0] * 24, "wind_speed_10m": [6.0] * 24, "weather_code": [2] * 24}
        if "precipitation_probability" in q["hourly"]:
            hourly["precipitation_probability"] = [20] * 24
        return httpx.Response(200, json={"hourly": hourly})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_forecast_and_race_day_conditions():
    calls = []
    http = fake_service(calls)
    db = SessionLocal()
    course = Course(name="Gibson Ranch", location="Gibson Ranch, Elverta")
    db.add(course)
    db.flush()
    assert WX.geocode(http, course.location) == (38.71, -121.46, "Elverta, California, US")   # whole name fails, the town works
    course.latitude, course.longitude = 38.71, -121.46
    soon = MeetEntry(name="Section Finals", date=TODAY + dt.timedelta(days=3), location="Gibson Ranch", start_time="10:30")
    far = MeetEntry(name="State", date=TODAY + dt.timedelta(days=30), location="Gibson Ranch")
    db.add_all([soon, far])
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t1")
    db.add(a)
    db.flush()
    past = TODAY - dt.timedelta(days=4)
    db.add(RaceResult(athlete_id=a.id, date=past, meet="League Finals", race="Boys Varsity", distance_m=5000, distance_label="5K",
                      time_s=1000, course="Gibson Ranch"))
    db.add(MeetConditions(meet="League Finals", date=past, wind_mph=12))          # entered by a coach: kept
    db.flush()
    out = WX.nightly(db, http)
    assert out == {"forecasts": 1, "race_conditions": 1}                           # 30 days out: too far ahead to forecast
    f = soon.forecast
    assert f.at.endswith("T10:00") and f.temp_f == 60 and f.rain_pct == 20 and f.summary == "Partly cloudy"
    assert WX.describe(f) == "60°F, partly cloudy, 20% chance of rain, wind 6 mph, dew point 45°F"
    cond = db.query(MeetConditions).one()
    assert cond.temp_f == 59 and cond.dew_point_f == 45 and cond.wind_mph == 12 and "Open-Meteo" in cond.notes   # 9 a.m. default
    assert all("latitude" in str(c.url) or c.url.host.startswith("geocoding") for c in calls)
    assert not any("Lowry" in str(c.url) for c in calls)                           # nothing about athletes leaves the app
    db.rollback()
    db.close()


def test_archive_for_old_days_and_errors_are_logged():
    calls = []
    http = fake_service(calls)
    assert WX.at(http, 38.7, -121.4, TODAY - dt.timedelta(days=200), 9, TODAY)["rain_pct"] is None
    assert calls[-1].url.host == "archive-api.open-meteo.com"
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy")))
    db = SessionLocal()
    db.add(Course(name="X", latitude=1, longitude=1))
    db.add(MeetEntry(name="X meet", date=TODAY + dt.timedelta(days=1), location="X"))
    db.flush()
    assert "503" in WX.nightly(db, down)["error"]
    db.rollback()
    db.close()


def test_meet_and_course_pages(monkeypatch):
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    c = Course(name="Gibson Ranch", location="Gibson Ranch, Elverta")
    db.add(c)
    db.flush()
    m = MeetEntry(name="Section Finals", date=TODAY + dt.timedelta(days=3), location="Gibson Ranch")
    db.add(m)
    db.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t1"))
    db.commit()
    cid, mid = c.id, m.id
    db.close()
    monkeypatch.setattr(WX, "make_client", lambda: fake_service([]))
    from xcapp.web import create_app
    app = create_app()
    cl = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    cl.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', cl.get("/courses").text).group(1)
    page = cl.get(f"/registrations/{mid}").text
    assert "Forecast: pending (needs the course’s map location)" in page
    cl.post(f"/courses/{cid}/locate", data={"csrf": csrf})
    assert "Found Elverta, California, US (38.71, -121.46)" in cl.get(f"/courses?edit={cid}").text
    cl.post(f"/registrations/{mid}", data={"csrf": csrf, "name": "Section Finals", "location": "Gibson Ranch", "date": m.date.isoformat(), "start_time": "9:5"})
    assert "Start time looks like 09:30" in cl.get(f"/registrations/{mid}").text
    cl.post(f"/registrations/{mid}", data={"csrf": csrf, "name": "Section Finals", "location": "Gibson Ranch", "date": m.date.isoformat(), "start_time": "11:00"})
    cl.post(f"/registrations/{mid}/forecast", data={"csrf": csrf})
    assert "Forecast updated: 61°F, partly cloudy" in cl.get(f"/registrations/{mid}").text
    assert "Forecast for 11:00:</b> 61°F" in TestClient(app).get("/a/t1").text


def test_meet_location_is_a_saved_course_or_a_place():
    db = SessionLocal()
    db.add(StaffUser(username="coach2", role="coach", password_hash=hash_password("correct-horse-1")))
    c = Course(name="Gibson Ranch", location="Elverta")
    db.add(c)
    db.commit()
    cid = c.id
    db.close()
    from xcapp.web import create_app
    cl = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    cl.post("/login", data={"csrf": csrf, "username": "coach2", "password": "correct-horse-1"})
    page = cl.get("/registrations").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert f'<option value="{cid}">Gibson Ranch · Elverta</option>' in page
    day = (TODAY + dt.timedelta(days=9)).isoformat()
    mid = int(cl.post("/registrations", data={"csrf": csrf, "name": "Gibson Invite", "date": day, "course_id": str(cid), "location": ""})
              .headers["location"].rsplit("/", 1)[-1])
    db = SessionLocal()
    m = db.get(MeetEntry, mid)
    assert (m.course_id, m.location) == (cid, "Elverta")
    db.close()
    cl.post(f"/registrations/{mid}", data={"csrf": csrf, "name": "Gibson Invite", "date": day, "course_id": "", "location": "Woodward Park, Fresno"})
    db = SessionLocal()
    m = db.get(MeetEntry, mid)
    assert (m.course_id, m.location) == (None, "Woodward Park, Fresno")
    db.close()


def test_new_meet_takes_a_start_time():
    db = SessionLocal()
    db.add(StaffUser(username="coach3", role="coach", password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    from xcapp.web import create_app
    cl = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    cl.post("/login", data={"csrf": csrf, "username": "coach3", "password": "correct-horse-1"})
    page = cl.get("/registrations").text
    assert 'name="start_time" value="09:00"' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = cl.post("/registrations", data={"csrf": csrf, "name": "Early Bird", "date": (TODAY + dt.timedelta(days=5)).isoformat(),
                                        "course_id": "", "location": "Folsom", "start_time": "08:15"})
    db = SessionLocal()
    assert db.get(MeetEntry, int(r.headers["location"].rsplit("/", 1)[-1])).start_time == "08:15"
    db.close()

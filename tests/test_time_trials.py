"""Time trials (time_trials.py): distances in meters, miles and kilometers, several on a day, weather at the start time,
adjusted times and VDOT, and the pages. Names are made up."""
import datetime as dt
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import time_trials as TT
from xcapp import weather as WX
from xcapp.db import SessionLocal
from xcapp.models import Athlete, StaffUser, TimeTrial, TimeTrialResult

TODAY = A.team_today()


def weather_service(calls, temp=82.0, dew=64.0):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        q = request.url.params
        day = q["start_date"]
        times = [f"{day}T{h:02d}:00" for h in range(24)]
        return httpx.Response(200, json={"hourly": {"time": times, "temperature_2m": [temp - 15 + h for h in range(24)],
                                                    "dew_point_2m": [dew] * 24, "relative_humidity_2m": [50.0] * 24,
                                                    "wind_speed_10m": [5.0] * 24, "weather_code": [0] * 24,
                                                    "pressure_msl": [1013.0] * 24, "precipitation_probability": [0] * 24}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_distances_and_times():
    assert TT.distance("1600", "m")[1] == 1600
    assert TT.distance("2", "mi")[1] == pytest.approx(3218.688)
    assert TT.distance("3", "km")[1] == 3000
    for bad in (("50", "m"), ("x", "km"), ("30", "mi"), ("3", "yd")):
        with pytest.raises(ValueError):
            TT.distance(*bad)
    assert TT.parse_time("5:12.4") == pytest.approx(312.4) and TT.parse_time("312") == 312 and TT.parse_time("1:02:03") == 3723
    with pytest.raises(ValueError):
        TT.parse_time("fast")


def test_results_weather_and_vdot(session):
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0")
    b = Athlete(first_name="Ben", last_name="Ortiz", gender="Boys", grade=10, group="JV", link_token="t1")
    session.add_all([a, b])
    session.flush()
    day = TODAY - dt.timedelta(days=1)
    t1 = TimeTrial(date=day, start_time="15:00", distance_value=3, distance_unit="km", distance_m=3000)
    t2 = TimeTrial(date=day, start_time="16:30", distance_value=1, distance_unit="mi", distance_m=1609.344)   # same day
    session.add_all([t1, t2])
    session.flush()
    calls = []
    with weather_service(calls) as http:
        assert TT.fetch_weather(session, t1, http, TODAY)
    assert t1.temp_f == 82.0 and t1.place == "Jesuit High School, Carmichael"
    assert calls[0].url.params["latitude"] == "38.6171"
    n, problems = TT.save_results(session, t1, {a.id: "10:00", b.id: "nonsense"})
    assert n == 1 and problems and "Ben Ortiz" in problems[0]
    n, problems = TT.save_results(session, t1, {b.id: "1:30"})                    # 48 s a mile: a typo
    assert n == 0 and "check it" in problems[0]
    TT.save_results(session, t1, {b.id: "10:40.5"})
    rs = TT.rows(t1)
    assert [x["r"].athlete_id for x in rs] == [a.id, b.id] and rs[0]["place"] == 1
    assert rs[0]["adjusted"] < 600 and rs[0]["vdot"] == pytest.approx(A.vdot(3000, 600))      # heat: adjusted is faster
    TT.save_results(session, t2, {a.id: "4:50"})
    TT.save_results(session, t1, {b.id: ""})                                       # an empty box removes the time
    assert session.query(TimeTrialResult).count() == 2
    best = A.latest_races(session, TODAY - dt.timedelta(days=30), TODAY)[a.id]
    assert best[0] == pytest.approx(max(A.vdot(3000, 600), A.vdot(1609.344, 290))) and "time trial" in best[1]
    mine = TT.for_athlete(session, a.id)
    assert [x["trial"].start_time for x in mine] == ["16:30", "15:00"] and mine[1]["of"] == 1


def test_pages(monkeypatch):
    calls = []
    monkeypatch.setattr(WX, "make_client", lambda: weather_service(calls))
    db = SessionLocal()
    from xcapp.security import hash_password
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Reed", gender="Girls", grade=10, group="Varsity", link_token="t-ava"))
    db.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t-sam"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/time-trials").text
    assert 'href="/time-trials"' in page and "Add a time trial" in page and "Jesuit High School" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    day = TODAY.isoformat()
    c.post("/time-trials", data={"csrf": csrf, "date": day, "start_time": "15:30", "distance_value": "abc", "distance_unit": "m"})
    assert "Enter the distance as a number" in c.get("/time-trials").text
    r1 = c.post("/time-trials", data={"csrf": csrf, "date": day, "start_time": "15:30", "distance_value": "1600", "distance_unit": "m", "team": "Girls"})
    r2 = c.post("/time-trials", data={"csrf": csrf, "date": day, "start_time": "16:15", "distance_value": "2", "distance_unit": "mi"})
    t1, t2 = (int(r.headers["location"].rsplit("/", 1)[1]) for r in (r1, r2))
    trial = c.get(f"/time-trials/{t1}").text
    assert "Added the 1600 m time trial" in trial and "Weather" in trial and "dew point 64°F" in trial
    assert "Ava Reed" in trial and "Sam Lowry" not in trial                       # the girls' trial lists the girls
    c.post(f"/time-trials/{t1}/times", data={"csrf": csrf, "athletes": ["1"], "t_1": "5:31.2"})
    trial = c.get(f"/time-trials/{t1}").text
    assert "Saved 1 time" in trial and "5:31.2" in trial
    listing = c.get("/time-trials").text
    assert listing.count('class="trial-row"') == 2 and "1600 m time trial" in listing and "2 mi time trial" in listing
    assert "My time trials" in TestClient(app).get("/a/t-ava").text
    assert f'/time-trials/{t1}' in c.get("/athletes/1").text
    c.post(f"/time-trials/{t2}/edit", data={"csrf": csrf, "action": "delete"})
    db = SessionLocal()
    assert db.query(TimeTrial).count() == 1
    db.close()
    t = TestClient(app, follow_redirects=False)
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/login").text).group(1)
    t.post("/login", data={"csrf": tcsrf, "username": "trainer1", "password": "correct-horse-1"})
    tp = t.get(f"/time-trials/{t1}").text
    assert "Enter times" not in tp and "5:31.2" in tp
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', tp).group(1)
    assert t.post(f"/time-trials/{t1}/times", data={"csrf": tcsrf, "athletes": ["1"], "t_1": "5:00"}).status_code == 403

"""Combining two roster entries for one athlete, and other names matched on upload."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from fitgen import rep_workout
from xcapp import imports as I
from xcapp.db import SessionLocal
from xcapp.fitfile import parse_fit
from xcapp.ingest import ingest
from xcapp.models import (Activity, Athlete, AthletePhoto, Goal, RaceResult, RestingHeartRate, Result, StaffUser)
from xcapp.security import hash_password

RUN1 = rep_workout(dt.datetime(2026, 10, 20, 15, tzinfo=dt.timezone.utc), [(1000, 200, 180, 20)] * 3)
RUN2 = rep_workout(dt.datetime(2026, 10, 22, 15, tzinfo=dt.timezone.utc), [(1000, 200, 180, 20)] * 3)


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    rafa = Athlete(first_name="Will", last_name="Pell", gender="Boys", grade=12, group="Varsity", link_token="t-rafa")
    rafael = Athlete(first_name="William", last_name="Pell", gender="Boys", grade=12, group="JV", link_token="t-rafael", hr_max=199)
    db.add_all([rafa, rafael])
    db.flush()
    ingest(db, rafa, parse_fit(RUN1), "upload")
    ingest(db, rafael, parse_fit(RUN1), "upload")                     # the same run under both names
    ingest(db, rafael, parse_fit(RUN2), "upload")
    d = dt.date(2026, 10, 24)
    db.add(RestingHeartRate(athlete_id=rafa.id, date=d, bpm=48, source="athlete"))
    db.add(RestingHeartRate(athlete_id=rafael.id, date=d, bpm=55, source="garmin"))
    db.add(RestingHeartRate(athlete_id=rafael.id, date=d - dt.timedelta(days=1), bpm=50, source="garmin"))
    db.add(Goal(athlete_id=rafael.id, event="5K", season="2026 XC", goal_s=900))
    res = Result(athlete_id=rafael.id, date=dt.date(2026, 8, 29), event="5K", time_s=950, kind="race", season="2026 XC")
    db.add(res)
    db.flush()
    db.add(RaceResult(athlete_id=rafael.id, date=res.date, meet="Oakmont", race="Boys Varsity", distance_m=5000,
                      distance_label="5K", time_s=950, place=10, result_id=res.id))
    db.add(AthletePhoto(athlete_id=rafael.id, data=b"jpeg"))
    db.commit()
    ids = rafa.id, rafael.id
    db.close()
    from xcapp.web import create_app
    return create_app(), ids


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c


def test_combine(app):
    web, (rafa, rafael) = app
    c = login(web)
    roster = c.get("/athletes").text
    assert f'href="/athletes/{rafa}?merge={rafael}#combine">Will Pell and William Pell</a>' in roster
    page = c.get(f"/athletes/{rafa}?merge={rafael}").text
    assert '<details id="combine" open>' in page and f'<option value="{rafael}" selected>' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post(f"/athletes/{rafa}/merge", data={"csrf": csrf, "other_id": str(rafael)})        # not confirmed: nothing happens
    db = SessionLocal()
    assert db.get(Athlete, rafael) is not None
    db.close()
    c.post(f"/athletes/{rafa}/merge", data={"csrf": csrf, "other_id": str(rafael), "confirm": "1"})
    msg = c.get(f"/athletes/{rafa}").text
    assert "Combined William Pell into Will Pell:" in msg and "duplicates dropped" in msg
    db = SessionLocal()
    a = db.get(Athlete, rafa)
    assert db.get(Athlete, rafael) is None and db.query(Athlete).count() == 1
    assert sorted(x.local_date.day for x in a.activities) == [20, 22]                    # shared run once, other run moved
    assert sorted((r.date.day, r.bpm, r.source) for r in db.query(RestingHeartRate)) == [(23, 50, "garmin"), (24, 48, "athlete")]
    assert db.query(Goal).one().athlete_id == rafa and db.query(RaceResult).one().athlete_id == rafa
    assert db.query(Result).one().athlete_id == rafa and db.query(AthletePhoto).one().athlete_id == rafa
    assert (a.group, a.hr_max, a.other_names, a.link_token) == ("Varsity", 199, "William Pell", "t-rafa")
    # The old name now matches on upload.
    I.import_races(db, "meet,date,race,distance,athlete,time\nCity,2026-10-31,Boys Varsity,5K,William Pell,15:50\n", False)
    assert db.query(RaceResult).filter_by(meet="City").one().athlete_id == rafa
    db.close()
    assert TestClient(web).get("/a/t-rafael").status_code == 404


def test_other_names(app):
    web, (rafa, rafael) = app
    c = login(web)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/athletes/{rafa}").text).group(1)
    c.post(f"/athletes/{rafa}/other-names", data={"csrf": csrf, "other_names": "W. Pell, Billy Pell; will pell"})
    db = SessionLocal()
    assert db.get(Athlete, rafa).other_names == "W. Pell; Billy Pell"                   # its own name isn't repeated
    I.import_races(db, "meet,date,race,distance,athlete,time\nCity,2026-10-31,Boys Varsity,5K,\"Pell, Billy\",15:50\n", False)
    assert db.query(RaceResult).filter_by(meet="City").one().athlete_id == rafa
    db.close()


def test_put_back_from_list_and_compact_roster(app):
    web, (rafa, _) = app
    c = login(web)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/athletes/{rafa}").text).group(1)
    c.post(f"/athletes/{rafa}/status", data={"csrf": csrf, "action": "leave"})
    roster = c.get("/athletes").text
    assert 'value="return"><input type="hidden" name="next" value="/athletes"><button class="secondary" type="submit">Put back on the roster' in roster
    r = c.post(f"/athletes/{rafa}/status", data={"csrf": csrf, "action": "return", "next": "/athletes"})
    assert r.headers["location"] == "/athletes" and "is back on the roster" in c.get("/athletes").text
    assert 'class="list sortable compact"' in roster and '<details class="plink"><summary>Show</summary>' in roster

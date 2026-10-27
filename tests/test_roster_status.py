"""Test athletes are left out of team numbers and the AI; athletes who leave are kept but off the roster."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import ai
from xcapp import analysis as A
from xcapp import datasets as DS
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, Result, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    today = A.team_today()
    for first, token in (("Sam", "t-sam"), ("Coach", "t-coach"), ("Theo", "t-theo")):
        a = Athlete(first_name=first, last_name="Test" if first == "Coach" else "Runner", gender="Boys", grade=11, group="Varsity",
                    link_token=token, ai_consent_at=dt.datetime(2026, 10, 1))
        db.add(a)
        db.flush()
        d = today - dt.timedelta(days=2)
        db.add(Activity(athlete_id=a.id, source="upload", external_id=f"x{a.id}", start_utc=dt.datetime(d.year, d.month, d.day, 15),
                        local_date=d, duration_s=2400, distance_m=8000, sport="running"))
        db.add(Result(athlete_id=a.id, date=today - dt.timedelta(days=20), event="5K", time_s=1000, kind="race", season="2026 XC"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c


def ids():
    db = SessionLocal()
    out = {a.first_name: a.id for a in db.query(Athlete)}
    db.close()
    return out


def post(c, aid, **data):
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/athletes/{aid}").text).group(1)
    return c.post(f"/athletes/{aid}/status", data={"csrf": csrf, **data})


def team_names():
    db = SessionLocal()
    snap = DS.snapshot(db)
    payload, code_map = ai.build_payload(snap)
    names = {r["athlete"] for r in snap["roster"]}, {v["name"] for v in code_map.values()}, \
        {r["athlete"] for r in snap["weekly_load"]}, {s["a"].name for b in A.vdot_board(db, A.team_today()) for sec in b["sections"] for s in sec["rows"]}
    db.close()
    return names


def test_test_athlete_and_leaving(app):
    c = login(app)
    i = ids()
    everyone = {"Sam Runner", "Coach Test", "Theo Runner"}
    assert all(n == everyone for n in team_names())
    post(c, i["Coach"], action="test", is_test="1")
    assert all(n == everyone - {"Coach Test"} for n in team_names())         # out of roster stats, AI, mileage and VDOT page
    roster = c.get("/athletes").text
    assert "Coach Test</a> <span class=\"pill\">Test</span>" in roster
    assert "Test athlete." in c.get(f"/athletes/{i['Coach']}").text
    assert "Hi Coach" in TestClient(app).get("/a/t-coach").text                # their own page still works

    post(c, i["Theo"], action="leave")
    msg = c.get(f"/athletes/{i['Theo']}").text
    keep = (A.team_today() + dt.timedelta(days=90)).strftime("%b %-d, %Y")
    assert "Theo Runner is off the roster." in msg and f"kept until {keep}" in msg and "has left the team" in msg
    assert all(n == {"Sam Runner"} for n in team_names())
    roster = c.get("/athletes").text
    assert "Former athletes (1)" in roster and roster.index("Former athletes") < roster.index(f'href="/athletes/{i["Theo"]}"')
    assert TestClient(app).get("/a/t-theo").status_code == 404                  # link stops working
    db = SessionLocal()
    theo = db.get(Athlete, i["Theo"])
    assert len(theo.activities) == 1 and len(theo.results) == 1                # data kept
    theo.left_on = A.team_today() - dt.timedelta(days=80)                       # deletion due within two weeks
    db.commit()
    db.close()
    home = c.get("/").text
    assert "Former athletes to delete" in home and "Theo Runner</a>, left" in home

    post(c, i["Theo"], action="return")
    assert "back on the roster" in c.get(f"/athletes/{i['Theo']}").text
    assert TestClient(app).get("/a/t-theo").status_code == 200 and "Former athletes" not in c.get("/athletes").text

"""The web app: sign-in, CSRF, roles, athlete link pages, uploads, imports, data feeds, security headers."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from fitgen import rep_workout
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, Injury, PainReport, Result, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    from xcapp.web import create_app
    db = SessionLocal()
    for name, role in (("admin1", "admin"), ("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Lucia", last_name="Sorensen", gender="Girls", grade=11, group="Varsity", link_token="tok-lucia"))
    db.commit()
    db.close()
    return create_app()


def csrf_of(c, url="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)


def login(app, user):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"csrf": csrf_of(c), "username": user, "password": "correct-horse-1"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    return c


def test_auth_csrf_and_headers(app):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/dashboard").headers["location"] == "/login"
    assert c.get("/api/datasets/roster.csv").status_code == 303
    r = c.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    assert c.post("/login", data={"username": "coach1", "password": "correct-horse-1"}).status_code == 400  # no CSRF token
    for _ in range(5):
        c.post("/login", data={"csrf": csrf_of(c), "username": "coach1", "password": "wrong"})
    c.post("/login", data={"csrf": csrf_of(c), "username": "coach1", "password": "correct-horse-1"})
    assert "Too many attempts" in c.get("/login").text


def test_roles(app):
    t = login(app, "trainer1")
    assert t.get("/staff").status_code == 403
    assert t.post("/results", data={"csrf": csrf_of(t, "/injuries")}).status_code == 403
    assert t.get("/injuries").status_code == 200
    co = login(app, "coach1")
    assert co.get("/staff").status_code == 403
    assert co.post("/staff/1/password", data={"csrf": csrf_of(co, "/"), "password": "another-pass-9"}).status_code == 403
    for page in ("/", "/dashboard", "/athletes", "/results", "/workouts", "/injuries", "/ai", "/sync", "/charts", "/athletes/1"):
        assert co.get(page).status_code == 200, page
    assert login(app, "admin1").get("/staff").status_code == 200


def test_injury_notes_are_private(app):
    t = login(app, "trainer1")
    r = t.post("/injuries", data={"csrf": csrf_of(t, "/injuries"), "athlete_id": "1", "onset_date": "2026-10-06",
                                  "injury": "Hamstring strain", "body_area": "Thigh", "notes": "PRIVATE-NOTE-123"})
    assert r.status_code == 303
    assert "PRIVATE-NOTE-123" in t.get("/injuries").text
    co = login(app, "coach1")
    assert "Hamstring strain" in co.get("/injuries").text and "PRIVATE-NOTE-123" not in co.get("/injuries").text
    assert "PRIVATE-NOTE-123" not in co.get("/athletes/1").text
    co.post("/injuries", data={"csrf": csrf_of(co, "/injuries"), "athlete_id": "1", "onset_date": "2026-10-20",
                               "injury": "Ankle sprain", "body_area": "Ankle", "notes": "coach-typed-note"})
    db = SessionLocal()
    assert db.query(Injury).filter_by(injury="Ankle sprain").one().notes == ""  # coaches cannot write notes
    db.close()


def test_athlete_link_pages(app):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/a/not-a-token").status_code == 404
    csrf = csrf_of(c, "/a/tok-lucia")
    assert c.post("/a/tok-lucia/pain", data={"csrf": csrf, "body_area": "Knee", "severity": "6"}).status_code == 303
    assert "tell your coach or athletic trainer in person" in c.get("/a/tok-lucia").text
    fit = rep_workout(dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc), [(1000, 200, 185, 30), (1000, 200, 187, 0)])
    r = c.post("/a/tok-lucia/upload", data={"csrf": csrf}, files={"file": ("run.fit", fit, "application/octet-stream")})
    assert r.status_code == 303
    assert "Added a" in c.get("/a/tok-lucia").text
    c.post("/a/tok-lucia/upload", data={"csrf": csrf}, files={"file": ("x.fit", b"garbage", "application/octet-stream")})
    assert "not a FIT file" in c.get("/a/tok-lucia").text
    db = SessionLocal()
    assert db.query(PainReport).count() == 1 and db.query(Activity).count() == 1
    db.close()


def test_imports_all_or_nothing(app):
    co = login(app, "coach1")
    bad = "athlete,event,time,date,course\nLucia Sorensen,5K,18:40.2,2026-10-17,Home\nNobody Here,5K,19:00,2026-10-17,Home\nLucia Sorensen,800m,2:30,2026-10-17,\n"
    co.post("/results/import", data={"csrf": csrf_of(co, "/results")}, files={"file": ("r.csv", bad, "text/csv")})
    page = co.get("/results").text
    assert "Nothing was imported" in page and "Line 3" in page and "Line 4" in page
    good = "athlete,event,time,date,course,kind\n\"Sorensen, Lucia\",5K,18:40.2,2026-10-17,Home,race\nLucia Sorensen,1600m,5:30.0,04/18/2026,,track\n"
    co.post("/results/import", data={"csrf": csrf_of(co, "/results")}, files={"file": ("r.csv", good, "text/csv")})
    db = SessionLocal()
    rs = {r.event: r for r in db.query(Result).all()}
    assert rs["5K"].season == "2026 XC" and rs["1 Mile"].season == "2026 Track"
    assert rs["1 Mile"].time_s == pytest.approx(330 * 1609.344 / 1600, abs=0.01) and "converted" in rs["1 Mile"].note
    db.close()
    roster = "name,gender,grade,group\nNew Runner,girls,9,JV\nLucia Sorensen,Girls,12,Varsity\n"
    co.post("/athletes/import", data={"csrf": csrf_of(co, "/athletes")}, files={"file": ("r.csv", roster, "text/csv")})
    db = SessionLocal()
    assert db.query(Athlete).count() == 2 and db.query(Athlete).filter_by(first_name="Lucia").one().grade == 12
    db.close()


def test_data_feeds_and_links(app):
    from xcapp.datasets import COLUMNS
    co = login(app, "coach1")
    for name, cols in COLUMNS.items():
        r = co.get(f"/api/datasets/{name}.csv")
        assert r.status_code == 200 and r.text.splitlines()[0] == ",".join(cols), name
    assert co.get("/api/datasets/secrets.csv").status_code == 404
    j = co.get("/api/charts/results.json").json()
    assert set(j) == {"results", "roster"} and j["roster"][0]["athlete"] == "Lucia Sorensen"
    links = co.get("/athletes/links.csv").text
    assert "https://xc.test/a/tok-lucia" in links and "not connected" in links
    assert login(app, "trainer1").get("/athletes/links.csv").status_code == 403


def test_workout_planning_and_delete_confirmation(app):
    co = login(app, "coach1")
    r = co.post("/workouts", data={"csrf": csrf_of(co, "/workouts"), "date": "2026-10-20", "title": "5 x 1K", "rep_distance_m": "1000",
                                   "pace_event": "5K", "pace_factor": "1.0"})
    assert r.status_code == 303 and "Saved 5 x 1K" in co.get("/workouts").text
    ad = login(app, "admin1")
    ad.post("/athletes/1/delete", data={"csrf": csrf_of(ad, "/athletes/1"), "confirm_name": "lucia"})
    db = SessionLocal()
    assert db.query(Athlete).count() == 1
    db.close()
    ad.post("/athletes/1/delete", data={"csrf": csrf_of(ad, "/athletes/1"), "confirm_name": "Lucia Sorensen"})
    db = SessionLocal()
    assert db.query(Athlete).count() == 0
    db.close()

"""Athletes reporting a day they didn't run (injury, sickness, rest...), and what coaches and the AI see."""
import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import datasets as DS
from xcapp.ai import build_payload
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, Injury, MissedDay, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("AS_OF_DATE", "2026-10-25")   # a Sunday
    from xcapp import config
    config.reload_settings()
    from xcapp.web import create_app
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    ava = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava")
    mat = Athlete(first_name="Mateo", last_name="Garza", gender="Boys", grade=10, group="JV", link_token="tok-mateo")
    db.add_all([ava, mat])
    db.flush()
    for k, day in enumerate((12, 13, 14, 19, 20, 22)):
        db.add(Activity(athlete_id=ava.id, source="upload", external_id=f"r{k}", start_utc=dt.datetime(2026, 10, day, 22),
                        local_date=dt.date(2026, 10, day), distance_m=8000, duration_s=2400, sport="running"))
    db.commit()
    db.close()
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/tok-ava").text).group(1)
    return app, c, csrf


def staff(app, user):
    c = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": tok, "username": user, "password": "correct-horse-1"})
    return c


INJURY = {"reason": "Injury", "body_area": "Knee", "side": "Right", "pain": "6", "started": "This week",
          "description": "Sharp pain just below the kneecap, worse going downstairs", "seen_by": "Not yet", "more_days": "A few days"}


def test_runs_page_lists_unreported_practice_days(web):
    app, c, csrf = web
    page = c.get("/a/tok-ava/runs").text
    assert "Log a day I didn’t run" in page
    # Mon-Sat days with no run since the first run (Oct 12), not today, not Sundays: Oct 15, 16, 17, 21, 23, 24
    assert page.count("Tell coaches why") == 6
    assert "/missed/2026-10-18" not in page and "/missed/2026-10-11" not in page and "/missed/2026-10-25" not in page
    r = c.get("/a/tok-ava/missed")
    assert r.status_code == 303 and r.headers["location"] == "/a/tok-ava/missed/2026-10-24"
    assert c.get("/a/tok-ava/missed/2026-10-26").status_code == 400      # future
    assert c.get("/a/tok-ava/missed/not-a-date").status_code == 404


def test_report_injury_update_and_remove(web):
    app, c, csrf = web
    url = "/a/tok-ava/missed/2026-10-23"
    page = c.get(url).text
    assert "Why didn’t you run?" in page and "Describe the injury" in page and "Do you think you’ll miss more days?" in page
    c.post(url, data={"csrf": csrf, "reason": "Injury", "body_area": "Knee"})
    assert "Please add which side and how much it hurts" in c.get(url + "?edit=1").text
    r = c.post(url, data={"csrf": csrf, **INJURY})
    assert r.status_code == 303 and r.headers["location"] == url
    page = c.get(url).text
    assert "Sent to your coaches" in page and "Knee, right" in page and "6 out of 10" in page and "below the kneecap" in page
    assert "tell your coach or athletic trainer in person" in page
    runs = c.get("/a/tok-ava/runs").text
    assert "Coaches told" in runs and "Injury: knee, right" in runs and runs.count("Tell coaches why") == 5
    c.post(url, data={"csrf": csrf, "reason": "Sick", "note": "Fever since Thursday night"})
    db = SessionLocal()
    m = db.query(MissedDay).one()
    assert (m.reason, m.body_area, m.pain, m.description) == ("Sick", "", None, "Fever since Thursday night")
    db.close()
    assert "Why didn’t you run?" in c.get("/a/tok-mateo/missed/2026-10-23").text   # Mateo sees his own blank day
    c.post(url + "/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(MissedDay).count() == 0
    db.close()


def test_watch_list_ai_and_charts(web):
    app, c, csrf = web
    c.post("/a/tok-ava/missed/2026-10-23", data={"csrf": csrf, **INJURY})
    c.post("/a/tok-mateo/missed/2026-10-21", data={"csrf": csrf, "reason": "Sick", "note": "flu"})
    c.post("/a/tok-mateo/missed/2026-10-24", data={"csrf": csrf, "reason": "Planned rest day"})
    db = SessionLocal()
    snap = DS.snapshot(db, dt.date(2026, 10, 25))
    why = {w["athlete"]: w["reasons"] for w in snap["watchlist"]}
    assert why["Ava Doyle"] == "Missed run (injury): Knee, right, 6/10"
    assert why["Mateo Garza"] == "Missed run (sick)"                       # a rest day is not flagged
    payload, _ = build_payload(snap)
    text = json.dumps(payload)
    assert "kneecap" not in text and "flu" not in text                    # descriptions never go to the AI
    ava = next(x for x in payload["athletes"] if x["days_missed_last_14"] and x["days_missed_last_14"][0]["reason"] == "Injury")
    assert ava["days_missed_last_14"][0] == {"reason": "Injury", "area": "Knee", "side": "Right", "pain": 6, "started": "This week",
                                             "seen_by": "Not yet", "more_days": "A few days", "days_ago": 2}
    db.close()
    s = c.get("/a/tok-ava/series.json").json()
    assert {"date": "2026-10-23", "area": "Missed run: injury: knee, right", "severity": 6} in s["pain"]


def test_trainer_adds_report_to_injury_log(web):
    app, c, csrf = web
    c.post("/a/tok-ava/missed/2026-10-23", data={"csrf": csrf, **INJURY})
    co = staff(app, "coach1")
    page = co.get("/injuries").text
    assert "Days missed, reported by athletes" in page and "below the kneecap" in page and "Add to injury log" in page
    tr = staff(app, "trainer1")
    tok = re.search(r'name="csrf" value="([^"]+)"', tr.get("/injuries").text).group(1)
    db = SessionLocal()
    mid = db.query(MissedDay).one().id
    db.close()
    tr.post(f"/missed/{mid}/log-injury", data={"csrf": tok, "injury": "Patellofemoral pain", "onset_date": "2026-10-21"})
    db = SessionLocal()
    inj = db.query(Injury).one()
    assert (inj.injury, inj.body_area, inj.onset_date, inj.logged_by) == ("Patellofemoral pain", "Knee", dt.date(2026, 10, 21), "trainer1")
    assert "below the kneecap" in inj.notes and db.query(MissedDay).one().injury_id == inj.id
    db.close()
    assert ">Added<" in co.get("/injuries").text
    assert "Missed run (injury): knee, right, pain 6/10" in co.get("/athletes/1").text

"""Athlete effort and pain per run, per-athlete charts data, branding, and upgrading an older database."""
import datetime as dt
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from fitgen import rep_workout
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, PainReport, Result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>')
    monkeypatch.setenv("TEAM_NAME", "Jesuit Cross Country")
    monkeypatch.setenv("TEAM_LOGO_FILE", str(logo))
    monkeypatch.setenv("AS_OF_DATE", "2026-10-25")
    from xcapp import config
    config.reload_settings()
    from xcapp.web import create_app
    db = SessionLocal()
    for i, (f, l) in enumerate((("Ava", "Doyle"), ("Mateo", "Garza"))):
        db.add(Athlete(first_name=f, last_name=l, gender="Girls" if i == 0 else "Boys", grade=11, group="Varsity", link_token=f"tok-{f.lower()}",
                       ai_consent_at=dt.datetime(2026, 10, 1)))
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/tok-ava").text).group(1)
    fit = rep_workout(dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc), [(1000, 200, 185, 30), (1000, 200, 187, 25), (1000, 200, 189, 0)])
    c.post("/a/tok-ava/upload", data={"csrf": csrf}, files={"file": ("run.fit", fit, "application/octet-stream")})
    db = SessionLocal()
    act_id = db.query(Activity).one().id
    db.close()
    return c, csrf, act_id


def test_branding(setup):
    c, _, _ = setup
    page = c.get("/a/tok-ava").text
    assert "Jesuit Cross Country" in page and 'src="/branding/logo"' in page and "Hi Ava" in page
    r = c.get("/branding/logo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "1 recent run needs your effort rating" in page


def test_effort_and_pain_per_run(setup):
    c, csrf, act_id = setup
    assert "Add effort" in c.get("/a/tok-ava/runs").text
    page = c.get(f"/a/tok-ava/runs/{act_id}").text
    assert "How did it go?" in page and "HR drop in 60 s" in page and "Recorded on Garmin Forerunner 265" in page
    # missing effort, and pain area without a level, are refused
    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "pain_area": "None"})
    assert "Pick how hard the run felt" in c.get(f"/a/tok-ava/runs/{act_id}").text
    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "7", "pain_area": "Knee"})
    assert "Also pick how much" in c.get(f"/a/tok-ava/runs/{act_id}").text
    r = c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "8", "feel": "Heavy", "pain_area": "Lower leg", "pain_level": "6", "note": "left shin sore"})
    assert r.status_code == 303 and r.headers["location"] == "/a/tok-ava/runs"
    runs_page = c.get("/a/tok-ava/runs").text
    assert "tell your coach or athletic trainer in person" in runs_page and "Effort 8 · Heavy" in runs_page and "Pain 6/10 Lower leg" in runs_page
    db = SessionLocal()
    act = db.get(Activity, act_id)
    pain = db.query(PainReport).one()
    assert (act.rpe, act.feel, act.athlete_note) == (8, "Heavy", "left shin sore")
    assert (pain.activity_id, pain.severity, pain.body_area, pain.date) == (act_id, 6, "Lower leg", act.local_date)
    db.close()
    # editing to "no pain" removes the linked report; editing again does not duplicate
    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "6", "feel": "Normal", "pain_area": "None"})
    db = SessionLocal()
    assert db.query(PainReport).count() == 0 and db.get(Activity, act_id).rpe == 6
    db.close()
    # another athlete cannot open or change this run
    assert c.get(f"/a/tok-mateo/runs/{act_id}").status_code == 404
    assert c.post(f"/a/tok-mateo/runs/{act_id}", data={"csrf": csrf, "rpe": "1", "pain_area": "None"}).status_code == 404


def test_pain_from_a_run_reaches_the_watch_list(setup):
    from xcapp import analysis as A
    from xcapp import datasets as DS
    c, csrf, act_id = setup
    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "8", "pain_area": "Knee", "pain_level": "5"})
    db = SessionLocal()
    snap = DS.snapshot(db, dt.date(2026, 10, 25))
    assert any(w["athlete"] == "Ava Doyle" and "Pain 5/10: Knee" in w["reasons"] for w in snap["watchlist"])
    wk = [r for r in snap["weekly_load"] if r["athlete"] == "Ava Doyle"][-1]
    act = db.get(Activity, act_id)
    assert wk["srpe"] == round(8 * act.duration_s / 60)
    from xcapp.ai import build_payload
    payload, _ = build_payload(snap)
    me = next(x for x in payload["athletes"] if x["runs_last_7_days"])
    assert me["runs_last_7_days"][0]["rpe"] == 8 and "note" not in str(me).lower()
    db.close()


def test_series_scoping_and_content(setup):
    c, csrf, act_id = setup
    db = SessionLocal()
    ava = db.query(Athlete).filter_by(first_name="Ava").one()
    db.add(Result(athlete_id=ava.id, event="5K", time_s=1122.3, date=dt.date(2026, 10, 17), season="2026 XC", course="Home"))
    db.commit()
    db.close()
    s = c.get("/a/tok-ava/series.json").json()
    assert s["athlete"] == "Ava Doyle" and s["today"] == "2026-10-25" and s["season"] == {"name": "2026 XC", "start": "2026-08-03", "end": "2026-12-05"}
    assert len(s["runs"]) == 1 and s["runs"][0]["workout"] == "3 reps" and s["runs"][0]["pace"] > 0
    assert s["results"][0]["pace"] == pytest.approx(1122.3 / 5000 * 1609.344, abs=0.1)
    assert c.get("/a/not-real/series.json").status_code == 404
    assert c.get(f"/api/athletes/{ava.id}/series.json").status_code == 303     # staff only
    assert c.get("/a/tok-mateo/series.json").json()["runs"] == []             # each athlete sees only their own


def test_upgrade_from_first_release(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE athletes (id INTEGER PRIMARY KEY, first_name VARCHAR(80), last_name VARCHAR(80), gender VARCHAR(10), grade INTEGER,
        "group" VARCHAR(30), hr_max INTEGER, hr_rest INTEGER, active BOOLEAN, link_token VARCHAR(64), created_at DATETIME);
      CREATE TABLE activities (id INTEGER PRIMARY KEY, athlete_id INTEGER, source VARCHAR(30), external_id VARCHAR(120), start_utc DATETIME,
        local_date DATE, duration_s FLOAT, distance_m FLOAT, avg_hr FLOAT, max_hr FLOAT, sport VARCHAR(40), device VARCHAR(120),
        planned_workout_id INTEGER, created_at DATETIME);
      CREATE TABLE pain_reports (id INTEGER PRIMARY KEY, athlete_id INTEGER, date DATE, body_area VARCHAR(60), severity INTEGER, note VARCHAR(300), created_at DATETIME);
      INSERT INTO activities (id, athlete_id, source, external_id, local_date, distance_m) VALUES (1, 1, 'upload', 'x', '2026-10-20', 5000);
    """)
    con.commit()
    con.close()
    from xcapp import db as D
    D.init_engine(f"sqlite:///{path}")
    D.create_all()
    D.create_all()  # running twice is safe
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(activities)")}
    assert {"rpe", "feel", "athlete_note", "feedback_at"} <= cols
    assert "activity_id" in {r[1] for r in con.execute("PRAGMA table_info(pain_reports)")}
    assert con.execute("SELECT distance_m FROM activities WHERE id = 1").fetchone()[0] == 5000
    con.close()

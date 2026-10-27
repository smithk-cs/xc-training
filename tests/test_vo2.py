"""The watch's VO2 max estimate: read from each run's FIT file, kept per run, shown for interest (never sets groups)."""
import datetime as dt
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from fitgen import rep_workout
from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.fitfile import parse_fit
from xcapp.ingest import ingest
from xcapp.models import Activity, Athlete, StaffUser
from xcapp.security import hash_password


def run(day, vo2=None):
    return rep_workout(dt.datetime(2026, 10, day, 14, 0, tzinfo=dt.timezone.utc), [(1000, 200, 180, 20)] * 3, vo2_max=vo2)


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def upload(c, files):
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/tok-ava").text).group(1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files:
            z.writestr(name, data)
    assert c.post("/a/tok-ava/upload", data={"csrf": csrf}, files=[("file", ("runs.zip", buf.getvalue()))]).status_code == 303


def test_parsing():
    assert parse_fit(run(1, 52.3)).vo2_max == 52.3
    assert parse_fit(run(1)).vo2_max is None
    assert parse_fit(run(1, 5.0)).vo2_max is None          # nonsense values are dropped


def test_stored_per_run_and_shown(app):
    c = TestClient(app, follow_redirects=False)
    upload(c, [("a.fit", run(10, 51.0)), ("b.fit", run(12, 52.4)), ("c.fit", run(14))])
    db = SessionLocal()
    by_day = {x.local_date.day: x.vo2_max for x in db.query(Activity)}
    assert by_day == {10: 51.0, 12: 52.4, 14: None}
    aid = db.query(Athlete).one().id
    latest = A.latest_vo2(db, aid)
    assert latest.vo2_max == 52.4 and latest.local_date.day == 12   # a later run without one doesn't hide it
    ids = {x.local_date.day: x.id for x in db.query(Activity)}
    assert A.abilities(db, dt.date(2026, 10, 20)).get(aid, {}).get("vdot") is None   # never feeds VDOT
    db.close()

    page = c.get("/a/tok-ava").text
    assert "Watch VO2 max: 52.4</b> (Oct 12)" in page and "watch’s VO2 max estimate" in page
    assert "Watch VO2 max</span><span class=\"big\">51.0" in c.get(f"/a/tok-ava/runs/{ids[10]}").text
    assert "Watch VO2 max" not in c.get(f"/a/tok-ava/runs/{ids[14]}").text
    assert page.count('<span class="small muted">VO2 max ') == 2 and "VO2 max 51.0</span>" in page   # Recent runs
    assert c.get("/a/tok-ava/runs").text.count('<span class="small muted">VO2 max ') == 2           # My runs
    export = json.loads(c.get("/a/tok-ava/my-data.json").text)
    assert sorted((r["date"], r["watch_vo2_max"]) for r in export["runs"]) == [
        ("2026-10-10", 51.0), ("2026-10-12", 52.4), ("2026-10-14", None)]

    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    coach_page = c.get(f"/athletes/{aid}").text
    assert "Watch VO2 max 52.4 (Oct 12;" in coach_page
    assert '<th class="num">VO2 max</th>' in coach_page and '<td class="num">52.4</td>' in coach_page
    assert "Watch VO2 max</span><span class=\"big\">52.4" in c.get(f"/athletes/{aid}/runs/{ids[12]}").text
    assert "VO2 max estimate" in c.get("/privacy").text


def test_an_upgrade_without_vo2_keeps_it(app):
    db = SessionLocal()
    a = db.query(Athlete).one()
    act, _ = ingest(db, a, parse_fit(run(5, 50.0)), "garmin_details")
    p = parse_fit(run(5))
    act2, outcome = ingest(db, a, p, "garmin_file")
    db.commit()
    assert outcome == "upgraded" and act2.id == act.id and act2.vo2_max == 50.0
    db.close()

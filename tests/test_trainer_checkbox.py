"""Athletes can say they've seen the athletic trainer when they report pain; staff see it."""
import re

from fastapi.testclient import TestClient

from test_feedback import setup  # noqa: F401  (fixture: athlete client, csrf, a synced run)
from xcapp.db import SessionLocal
from xcapp.models import PainReport, StaffUser
from xcapp.security import hash_password


def test_saw_trainer_on_both_pain_forms(setup):
    c, csrf, act_id = setup
    page = c.get("/a/tok-ava").text
    assert 'name="saw_trainer"' in page and "I’ve seen the athletic trainer about this." in page
    assert 'name="saw_trainer"' in c.get(f"/a/tok-ava/runs/{act_id}").text

    c.post("/a/tok-ava/pain", data={"csrf": csrf, "body_area": "Knee", "severity": "5", "saw_trainer": "yes"})
    c.post("/a/tok-ava/pain", data={"csrf": csrf, "body_area": "Foot", "severity": "2"})
    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "6", "pain_area": "Ankle", "pain_level": "4", "saw_trainer": "yes"})
    db = SessionLocal()
    seen = {p.body_area: p.saw_trainer for p in db.query(PainReport)}
    assert seen == {"Knee": True, "Foot": False, "Ankle": True}
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    assert 'checked><span>I’ve seen the athletic trainer' in c.get(f"/a/tok-ava/runs/{act_id}").text   # remembered

    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "6", "pain_area": "Ankle", "pain_level": "4"})  # unticked
    db = SessionLocal()
    assert db.query(PainReport).filter_by(body_area="Ankle").one().saw_trainer is False
    db.close()
    assert c.get("/a/tok-ava/my-data.json").json()["pain_reports"][0]["saw_athletic_trainer"] in (True, False)

    t = TestClient(c.app, follow_redirects=False)
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/login").text).group(1)
    t.post("/login", data={"csrf": tcsrf, "username": "trainer1", "password": "correct-horse-1"})
    injuries = t.get("/injuries").text
    assert injuries.count('<span class="pill good">Saw trainer</span>') == 1   # the knee report

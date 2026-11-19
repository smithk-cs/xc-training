"""Return to running after an injury (return_run.py): moving on, repeating and pausing by pain, finishing, and the
trainer's and athlete's pages. Names are made up."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import return_run as RR
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Injury, PainReport, ReturnCheck, ReturnPlan, StaffUser
from xcapp.security import hash_password

TODAY = dt.date(2026, 9, 1)


def athlete(db):
    a = Athlete(first_name="Ava", last_name="Reed", gender="Girls", grade=11, group="Varsity", link_token="t-ava")
    db.add(a)
    db.flush()
    inj = Injury(athlete_id=a.id, onset_date=TODAY - dt.timedelta(days=20), injury="Shin splints", body_area="Shin", logged_by="t")
    db.add(inj)
    db.flush()
    return a, inj


def test_steps_move_by_pain(session):
    a, inj = athlete(session)
    p = RR.start(session, a, TODAY, step=0, injury_id=inj.id, by="trainer1")
    assert RR.view(session, p, TODAY)["open"] and RR.view(session, p, TODAY)["title"] == "Brisk walk"
    assert "next: Walk 4, jog 1 on Thu Sep 3" in RR.report(session, p, 1, TODAY)
    assert p.step == 1 and not RR.view(session, p, TODAY + dt.timedelta(days=1))["open"]
    with pytest.raises(ValueError):
        RR.report(session, p, 0, TODAY + dt.timedelta(days=1))                 # not open yet: a day off between steps
    d = TODAY + dt.timedelta(days=2)
    assert "repeat Walk 4, jog 1" in RR.report(session, p, 3, d) and p.step == 1
    d += dt.timedelta(days=2)
    assert "see the athletic trainer" in RR.report(session, p, 6, d, "sharp on the left")
    assert p.status == "paused" and p.step == 0
    pr = session.query(PainReport).one()
    assert pr.severity == 6 and pr.body_area == "Shin" and "Walk 4, jog 1: sharp on the left" in pr.note
    with pytest.raises(ValueError):
        RR.report(session, p, 0, d + dt.timedelta(days=3))                     # paused until the trainer restarts it
    # Finishing: the last step at 0-2 pain
    p2 = RR.start(session, a, d, step=len(RR.STEPS) - 1)
    assert p.status == "stopped" and RR.active(session, a.id) is p2
    assert "back to easy running" in RR.report(session, p2, 2, d) and p2.status == "done" and p2.finished_on == d
    assert session.query(ReturnCheck).count() == 4


def test_pages():
    db = SessionLocal()
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    a, inj = athlete(db)
    db.commit()
    iid = inj.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    t = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/login").text).group(1)
    t.post("/login", data={"csrf": csrf, "username": "trainer1", "password": "correct-horse-1"})
    page = t.get("/injuries").text
    assert f'action="/injuries/{iid}/return-run"' in page and "Step 3: Walk 3, jog 2" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    t.post(f"/injuries/{iid}/return-run", data={"csrf": csrf, "step": "2"})
    page = t.get("/injuries").text
    assert "Started Ava Reed’s return to running at step 3" in page and "Return to running" in page and "3 of 8: Walk 3, jog 2" in page
    assert f'action="/injuries/{iid}/return-run"' not in page                       # one plan at a time
    me = TestClient(app, follow_redirects=False)
    mine = me.get("/a/t-ava").text
    assert 'id="return-run"' in mine and "Walk 3 minutes, then jog 2 minutes" in mine and "Done with today’s step" in mine
    acsrf = re.search(r'name="csrf" value="([^"]+)"', mine).group(1)
    me.post("/a/t-ava/return-run", data={"csrf": acsrf, "pain": "7", "note": "hurt on the downhill"})
    mine = me.get("/a/t-ava").text
    assert "see the athletic trainer" in mine and "Paused." in mine
    page = t.get("/injuries").text
    assert "Paused: pain" in page and "pain 7/10" in page and "hurt on the downhill" in page
    db = SessionLocal()
    pid = db.query(ReturnPlan).one().id
    db.close()
    t.post(f"/injuries/return/{pid}", data={"csrf": csrf, "action": "resume"})
    assert "now on step 2, Walk 4, jog 1, open today" in t.get("/injuries").text
    assert "Done with today’s step" in me.get("/a/t-ava").text

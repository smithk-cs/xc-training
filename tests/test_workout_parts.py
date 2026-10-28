"""Workouts made of parts (sets x reps x distance at a pace), with each athlete's times from their VDOT and goal."""
import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Goal, PlannedWorkout, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam", vdot_override=60)
    db.add(a)
    db.add(Athlete(first_name="Pat", last_name="Reed", gender="Boys", grade=9, group="JV", link_token="t-pat"))
    db.flush()
    db.add(Goal(athlete_id=a.id, event="5K", season=A.season_for(A.team_today()), goal_s=16 * 60))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/workouts").text).group(1)


def test_parts_workout(app):
    c, csrf = login(app)
    page = c.get("/workouts").text
    assert 'id="add-part"' in page and "<option>Repetition</option>" in page
    day = (A.team_today() + dt.timedelta(days=1)).isoformat()
    form = {"csrf": csrf, "date": day, "title": "",
            "p_sets": ["1", "2", "1", "1"], "p_reps": ["1", "4", "1", "6"], "p_dist": ["3200", "400", "1600", ""],
            "p_zone": ["Easy", "Interval", "Tempo", "Repetition"], "p_rest": ["", "90 s jog", "", ""]}
    c.post("/workouts", data=form)
    page = c.get("/workouts").text
    assert "Saved 3,200 m Easy + 2 × 4 × 400 m Interval + 1,600 m Tempo." in page               # empty last row ignored
    assert "2 × 4 × 400 m Interval (90 s jog rest)" in page and "Paces by group" in page
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert w.rep_distance_m == 400 and json.loads(w.parts)[1] == {"sets": 2, "reps": 4, "dist": 400, "zone": "Interval", "rest": "90 s jog"}
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    assert round(A.target_pace(db, sam.id, w)) == round(A.pace_at(60, A.INTERVAL_PCT))                     # rep-matching target
    db.close()
    me = TestClient(app).get("/a/t-sam").text
    assert ">At VDOT 60</th>" in me and ">Goal VDOT " in me
    assert "<b>2 × 4 × 400 m</b> Interval" in me and "<b>1:21.0</b>" in me                       # 400 at VDOT 60 interval pace
    assert "5:26/mi" in me and "90 s jog rest" in me
    pat = TestClient(app).get("/a/t-pat").text
    assert "Your times appear once you have a VDOT" in pat
    c.post("/workouts", data={**form, "wid": str(w.id), "p_dist": ["3200", "50", "1600", ""]})
    assert "Check part 2: 1 to 10 sets, 1 to 40 reps, 100 to 20,000 meters, and a pace." in c.get("/workouts").text
    c.post("/workouts", data={**form, "p_dist": ["", "", "", ""]})
    assert "Add at least one part" in c.get("/workouts").text


def test_older_workout_opens_as_one_part(app):
    c, csrf = login(app)
    db = SessionLocal()
    db.add(PlannedWorkout(date=A.team_today(), title="6 x 1K", rep_distance_m=1000, pace_event="5K", pace_factor=1.07, groups="All"))
    db.commit()
    wid = db.query(PlannedWorkout).one().id
    db.close()
    page = c.get(f"/workouts?edit={wid}").text
    assert 'name="p_dist" min="100" max="20000" step="1" value="1000"' in page and "<option selected>Tempo</option>" in page

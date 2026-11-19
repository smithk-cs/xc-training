"""Race-day pacing plans (pacing.py): the predicted time agrees with how results are adjusted, the splits follow the
course's hills and add up, and the athlete's and coach's pages. Names are made up."""
import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import courses as CS
from xcapp import pacing as PC
from xcapp.db import SessionLocal
from xcapp.models import (Athlete, Course, CourseFlyover, DivisionAthlete, MeetDivision, MeetEntry, MeetForecast, StaffUser)
from xcapp.security import hash_password

TODAY = A.team_today()
HILLS = [[0, 100], [0.5, 100], [1.0, 100], [1.5, 180], [2.0, 180], [2.5, 100], [3.107, 100]]   # a climb in mile 2


def meet(db, flyover=True, forecast=True):
    c = Course(name="Willow Park", distance_m=5000, hill_tier=3, surface="Firm grass", turns="Some", latitude=38.6, longitude=-121.3)
    db.add(c)
    db.flush()
    if flyover:
        db.add(CourseFlyover(course_id=c.id, distance="5K", data=json.dumps({"E": HILLS, "total": 3.107}),
                             notes="1.2 The big hill by the pond\n2.6 Downhill to the lake"))
    m = MeetEntry(name="Valley Invitational", date=TODAY + dt.timedelta(days=5), course_id=c.id, start_time="09:00")
    db.add(m)
    db.flush()
    if forecast:
        db.add(MeetForecast(meet_id=m.id, at="x", temp_f=75, dew_point_f=58))
    d = MeetDivision(meet_id=m.id, name="Varsity Boys", distance="5K", gender="Boys", level="Varsity")
    db.add(d)
    db.flush()
    return c, m, d


def test_prediction_matches_the_result_adjustment_and_splits_follow_hills(session):
    c, m, d = meet(session)
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0")
    session.add(a)
    session.flush()
    session.add(DivisionAthlete(division_id=d.id, athlete_id=a.id))
    session.flush()
    session.refresh(m)
    p = PC.plan(session, a, m, TODAY, vdot=55)
    track = A.equivalent_time(5000, 55)
    assert p["track"] == pytest.approx(track) and p["total"] > track
    cond = type("C", (), {"temp_f": 75, "dew_point_f": 58, "footing": ""})()
    assert CS.adjusted_time(p["total"], 5000, c, cond) == pytest.approx(track, abs=0.5)    # the reverse of adjusting a result
    sp = p["splits"]
    assert [s["mile"] for s in sp] == [1, 2, 3, 4] and sp[-1]["to"] == pytest.approx(5000 / 1609.344)
    assert sum(s["time"] for s in sp) == pytest.approx(p["total"]) and sp[-1]["cum"] == pytest.approx(p["total"])
    assert sp[1]["pace"] > sp[0]["pace"] > sp[2]["pace"] and sp[1]["climb"] == 80          # slower up, faster down
    assert sp[1]["notes"] == ["The big hill by the pond"] and sp[2]["notes"] == ["Downhill to the lake"]
    assert "Mile 2 climbs about 80 ft" in " ".join(PC.tips(p))
    assert PC.plan(session, a, m, TODAY) is None                                          # no VDOT yet: no plan


def test_even_splits_without_a_profile(session):
    _, m, d = meet(session, flyover=False, forecast=False)
    a = Athlete(first_name="Ava", last_name="Reed", gender="Boys", grade=10, group="JV", link_token="t1")
    session.add(a)
    session.flush()
    p = PC.plan(session, a, m, TODAY, vdot=48, division=d)
    assert p["even"] and p["weather"] is None
    full = [s["pace"] for s in p["splits"]]
    assert max(full) - min(full) < 0.01


def test_pages():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    _, m, d = meet(db)
    sam = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0", vdot_override=55)
    ben = Athlete(first_name="Ben", last_name="Ortiz", gender="Boys", grade=9, group="JV", link_token="t2")
    db.add_all([sam, ben])
    db.flush()
    db.add_all([DivisionAthlete(division_id=d.id, athlete_id=sam.id, position=0), DivisionAthlete(division_id=d.id, athlete_id=ben.id, position=1)])
    db.commit()
    mid = m.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    athlete = TestClient(app)
    page = athlete.get("/a/t0").text
    assert f'/a/t0/meets/{mid}/plan' in page and "My race plan" in page
    plan = athlete.get(f"/a/t0/meets/{mid}/plan").text
    assert "Target time" in plan and "Mile splits" in plan and "The big hill by the pond" in plan and "VDOT 55" in plan
    assert "There’s no plan yet" in athlete.get(f"/a/t2/meets/{mid}/plan").text          # no VDOT
    assert athlete.get(f"/a/t0/meets/999/plan").status_code == 404
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert f'href="/registrations/{mid}/plans"' in c.get(f"/registrations/{mid}").text
    coach = c.get(f"/registrations/{mid}/plans").text
    assert "Race plans: Valley Invitational" in coach and "Mile 3</th>" in coach and "Finish</th>" in coach and "No VDOT yet" in coach
    assert coach.index("Sam Lowry") < coach.index("Ben Ortiz")

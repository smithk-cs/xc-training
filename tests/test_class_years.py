"""Class years: grades follow from them each school year, graduates become alumni with all their data kept, and old
race imports get class years from the race date. Names are made up."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import classyear as CY
from xcapp import imports as I
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, RaceResult, StaffUser
from xcapp.security import hash_password


def test_school_years():
    assert CY.school_year_end(dt.date(2026, 10, 28)) == 2027 and CY.school_year_end(dt.date(2027, 6, 30)) == 2027
    assert CY.school_year_end(dt.date(2027, 7, 1)) == 2028
    assert CY.grade_for(2027, dt.date(2026, 10, 28)) == 12 and CY.grade_for(2030, dt.date(2026, 10, 28)) == 9
    assert CY.class_of(12, dt.date(2025, 10, 25)) == 2026 and CY.class_of(9, dt.date(2026, 9, 1)) == 2030


def test_graduating_and_grading_up(session):
    today = dt.date(2026, 10, 28)
    sr = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t1", grad_year=2027)
    fr = Athlete(first_name="Pat", last_name="Reed", gender="Boys", grade=9, group="JV", link_token="t2")       # no class year yet
    session.add_all([sr, fr])
    session.flush()
    session.add(Activity(athlete_id=sr.id, source="upload", external_id="x", start_utc=dt.datetime(2026, 10, 1, 15), local_date=dt.date(2026, 10, 1),
                         duration_s=1800, distance_m=8000, sport="running"))
    session.flush()
    assert CY.refresh(session, today) == {"filled": 1, "graduated": 0, "grades": 0} and fr.grad_year == 2030
    r = CY.refresh(session, dt.date(2027, 7, 1))                         # the next school year starts
    assert r == {"filled": 0, "graduated": 1, "grades": 1}
    assert (sr.alumni, sr.active, sr.left_on, len(sr.activities)) == (True, False, None, 1)   # all data kept
    assert (fr.grade, fr.active) == (10, True)


def test_old_race_imports_and_confirming(session):
    today = A.team_today()
    old = (dt.date(today.year - 1, 10, 25)).isoformat()
    I.import_races(session, f"meet,date,race,distance,athlete,time,grade\nHilltop,{old},Boys Varsity,5K,Ray Cole,16:00,12\n"
                            f"Hilltop,{old},Boys Varsity,5K,Eli Hart,16:30,10\n", True)
    ray = session.query(Athlete).filter_by(first_name="Ray").one()
    eli = session.query(Athlete).filter_by(first_name="Eli").one()
    assert ray.grad_year == CY.school_year_end(dt.date.fromisoformat(old)) and eli.grad_year == ray.grad_year + 2
    CY.refresh(session, today)
    assert ray.alumni and not eli.alumni and eli.grade == 11                # a senior last year has graduated; a sophomore is now a junior
    # Someone added before class years existed, whose grade came from an old race: confirm before graduating them.
    ivy = Athlete(first_name="Ivy", last_name="Stone", gender="Girls", grade=12, group="Varsity", link_token="t3")
    session.add(ivy)
    session.flush()
    session.add(RaceResult(athlete_id=ivy.id, date=dt.date.fromisoformat(old), meet="Hilltop", race="Girls", distance_m=5000,
                           distance_label="5K", time_s=1200))
    session.flush()
    CY.refresh(session, today)
    assert ivy.grad_year is None and not ivy.alumni
    assert [(x["athlete"].first_name, x["suggested"]) for x in CY.to_confirm(session, today)] == [("Ivy", ray.grad_year)]


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ivy", last_name="Stone", gender="Girls", grade=12, group="Varsity", link_token="t-ivy"))
    db.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t-sam"))
    db.flush()
    ivy = db.query(Athlete).filter_by(first_name="Ivy").one()
    db.add(RaceResult(athlete_id=ivy.id, date=A.team_today() - dt.timedelta(days=400), meet="Hilltop", race="Girls", distance_m=5000,
                      distance_label="5K", time_s=1200))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def test_pages(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/athletes").text
    db = SessionLocal()
    ivy = db.query(Athlete).filter_by(first_name="Ivy").one().id
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    assert sam.grad_year == CY.class_of(11, A.team_today())                  # filled in from the current grade
    db.close()
    assert "Class years to confirm (1)" in page and f'name="cls_{ivy}"' in page and ">Class</th>" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/athletes/class-years", data={"csrf": csrf, f"cls_{ivy}": str(CY.school_year_end(A.team_today()) - 1)})
    page = c.get("/athletes").text
    assert "1 moved to alumni." in page and "Alumni (1)" in page and "Class years to confirm" not in page
    assert f"Class of {CY.school_year_end(A.team_today()) - 1}" in page
    assert TestClient(app).get("/a/t-ivy").status_code == 404
    detail = c.get(f"/athletes/{ivy}").text
    assert "Alumni, class of" in detail and "Hilltop" in detail                # her races are still there
    # Back for another year: a class year in the future returns her to the roster.
    c.post(f"/athletes/{ivy}/edit", data={"csrf": csrf, "grade": "12", "group": "Varsity", "grad_year": str(CY.school_year_end(A.team_today()))})
    db = SessionLocal()
    i = db.get(Athlete, ivy)
    assert (i.alumni, i.active, i.grade) == (False, True, 12)
    db.close()
    assert "When athletes graduate, their records (races, results and training) are kept" in c.get("/privacy").text

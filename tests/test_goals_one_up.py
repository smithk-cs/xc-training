"""Every athlete's goal VDOT set to their VDOT + 1 (analysis.goals_one_up and the Goals page button)."""
import datetime as dt
import json
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.models import Athlete, Goal, StaffUser

TODAY = dt.date(2026, 11, 11)


def test_goal_vdot_becomes_vdot_plus_one(session):
    season = A.season_for(TODAY)
    a = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="t1", vdot_override=50)
    b = Athlete(first_name="Ana", last_name="Lee", gender="Boys", grade=10, group="Varsity", link_token="t2", vdot_override=44)
    c = Athlete(first_name="New", last_name="Kid", gender="Boys", grade=9, group="JV", link_token="t3")   # no VDOT yet
    session.add_all([a, b, c])
    session.flush()
    session.add_all([Goal(athlete_id=a.id, event="5K", season=season, goal_s=900.0),          # far faster: replaced
                     Goal(athlete_id=a.id, event="5K", season="2025 XC", goal_s=1100.0)])     # another season: kept
    session.flush()
    assert A.goals_one_up(session, TODAY) == 2
    assert A.goal_vdot(session, a.id, TODAY) == 51 and A.goal_vdot(session, b.id, TODAY) == 45
    mine = session.query(Goal).filter_by(athlete_id=a.id, season=season).all()
    assert sorted(g.event for g in mine) == sorted(A.EVENTS) and next(g for g in mine if g.event == "5K").goal_s > 900
    assert session.query(Goal).filter_by(athlete_id=a.id, season="2025 XC").one().goal_s == 1100.0
    assert session.query(Goal).filter_by(athlete_id=c.id).count() == 0
    # Each goal time is the slowest that still reaches VDOT 51: a tenth slower falls to 50
    five = next(g for g in mine if g.event == "5K")
    table = A.vdot_targets(session)                                       # the team's own VDOT table
    assert A.team_vdot(A.vdot(5000, five.goal_s), table) == 51 and A.team_vdot(A.vdot(5000, five.goal_s + 0.1), table) == 50


def test_works_with_the_team_vdot_table(session):
    # The team's own table (Groups page): VDOT n reaches when the 1600 equivalent beats its 1600 time
    targets = {n: ("2:30", f"{5 + (60 - n) // 10}:{(60 - n) % 10 * 6:02d}") for n in range(40, 61)}
    A.set_setting(session, "vdot_targets", json.dumps({str(k): list(v) for k, v in targets.items()}))
    a = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="t1", vdot_override=48)
    session.add(a)
    session.flush()
    A.goals_one_up(session, TODAY)
    assert A.goal_vdot(session, a.id, TODAY) == 49


def test_goals_page_button():
    from xcapp.db import SessionLocal
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="t1", vdot_override=50))
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/results").text
    assert "Set every goal VDOT to VDOT + 1" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/goals/one-up", data={"csrf": csrf})
    assert r.status_code == 303 and "Set 1 athletes" in c.get("/results").text
    db = SessionLocal()
    aid = db.query(Athlete).one().id
    assert A.goal_vdot(db, aid, A.team_today()) == 51
    db.close()

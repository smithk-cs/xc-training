"""Post-race reflections (reflections.py): saving, the nudge for a recent race, and the athlete's and coaches' pages.
Names are made up."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import reflections as RF
from xcapp.db import SessionLocal
from xcapp.models import Athlete, RaceReflection, RaceResult, StaffUser
from xcapp.security import hash_password

TODAY = A.team_today()


def setup(db):
    sam = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0")
    ben = Athlete(first_name="Ben", last_name="Ortiz", gender="Boys", grade=10, group="JV", link_token="t1")
    db.add_all([sam, ben])
    db.flush()
    races = {}
    for a, t in ((sam, 1020), (ben, 1100)):
        races[a.first_name] = RaceResult(athlete_id=a.id, meet="Clovis Invitational", date=TODAY - dt.timedelta(days=2), race="Boys Varsity",
                                         distance_m=5000, distance_label="5K", time_s=t, place=10)
        db.add(races[a.first_name])
    old = RaceResult(athlete_id=sam.id, meet="Early Bird", date=TODAY - dt.timedelta(days=40), distance_m=5000, distance_label="5K", time_s=1060)
    db.add(old)
    db.flush()
    return sam, ben, races, old


def test_save_and_nudge(session):
    sam, ben, races, old = setup(session)
    mine = [races["Sam"], old]
    assert RF.to_ask(mine, {}, TODAY) is races["Sam"]                                   # the recent one, not the old one
    with pytest.raises(ValueError, match="Tap how happy"):
        RF.save(session, sam, races["Sam"], "", "x", "", "")
    with pytest.raises(ValueError, match="at least one"):
        RF.save(session, sam, races["Sam"], 4, " ", "", "")
    with pytest.raises(ValueError, match="isn’t your race"):
        RF.save(session, sam, races["Ben"], 4, "x", "", "")
    RF.save(session, sam, races["Sam"], 4, "Stayed with the pack", "Started too fast", "Even first mile")
    RF.save(session, sam, races["Sam"], 5, "Stayed with the pack", "", "Even first mile")   # changed, not doubled
    assert session.query(RaceReflection).count() == 1 and RF.by_race(session, sam.id)[races["Sam"].id].happy == 5
    assert RF.to_ask(mine, RF.by_race(session, sam.id), TODAY) is None
    assert RF.counts(session) == {(races["Sam"].date, "Clovis Invitational"): 1}


def test_pages():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    sam, ben, races, _ = setup(db)
    db.commit()
    rid, sid = races["Sam"].id, sam.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    me = TestClient(app, follow_redirects=False)
    page = me.get("/a/t0").text
    assert "How did Clovis Invitational go?" in page and f"/a/t0/races/{rid}/reflect" in page
    form = me.get(f"/a/t0/races/{rid}/reflect").text
    assert "How happy are you with this race?" in form
    csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    assert me.get(f"/a/t1/races/{rid}/reflect").status_code == 404                     # someone else's race
    r = me.post(f"/a/t0/races/{rid}/reflect", data={"csrf": csrf, "happy": "4", "went_well": "Strong last mile",
                                                     "change": "Got boxed in at the start", "next_focus": "Get out clean"})
    assert r.status_code == 303
    page = me.get("/a/t0").text
    assert "Saved your reflection on Clovis Invitational" in page and "How did Clovis" not in page and "✓ Reflection" in page
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    races_page = c.get("/races").text
    assert "/races/reflections?date=" in races_page and "1 reflection" in races_page
    meet = c.get(f"/races/reflections?date={(TODAY - dt.timedelta(days=2)).isoformat()}&meet=Clovis Invitational").text
    assert "1 of 2 athletes reflected" in meet and "Got boxed in at the start" in meet and "no reflection yet" in meet
    assert c.get("/races/reflections?date=nope&meet=x").status_code == 404
    detail = c.get(f"/athletes/{sid}").text
    assert 'id="reflections"' in detail and "Strong last mile" in detail

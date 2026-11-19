"""The morning check-in (wellness.py): saving, flags against the athlete's usual and with resting heart rate, the
athlete's card, the coach's pages and the reminder. Names are made up."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import reminders as RM
from xcapp import wellness as WL
from xcapp.db import SessionLocal
from xcapp.models import Athlete, StaffUser, Wellness

TODAY = A.team_today()


def people(db):
    out = []
    for i, first in enumerate(("Sam", "Ava", "Ben")):
        a = Athlete(first_name=first, last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token=f"t{i}")
        db.add(a)
        out.append(a)
    db.flush()
    return out


def history(db, a, days, sleep, sore, mood, start=4):
    for k in range(start, start + days):
        db.add(Wellness(athlete_id=a.id, date=TODAY - dt.timedelta(days=k), sleep_h=sleep, soreness=sore, mood=mood))


def test_save_and_flags(session):
    sam, ava, ben = people(session)
    w = WL.save(session, sam, TODAY, "8", "2", "4")
    assert (w.sleep_h, w.soreness, w.mood) == (8, 2, 4)
    WL.save(session, sam, TODAY, 7, 3, 3)                                   # the same day again: changed, not doubled
    assert session.query(Wellness).filter_by(athlete_id=sam.id).count() == 1
    with pytest.raises(ValueError):
        WL.save(session, sam, TODAY, 8, 7, 3)
    history(session, sam, 10, 8, 2, 4)                                       # usual: 8 h, fine
    history(session, ava, 10, 9, 2, 4)                                       # usual 9 h; lately 7 and low, sore
    for k in range(3):
        session.add(Wellness(athlete_id=ava.id, date=TODAY - dt.timedelta(days=k), sleep_h=7, soreness=4, mood=2))
    history(session, ben, 10, 8, 2, 4)
    for k in range(2):
        session.add(Wellness(athlete_id=ben.id, date=TODAY - dt.timedelta(days=k), sleep_h=6, soreness=2, mood=4))
    session.flush()
    flags = WL.flags(session, TODAY, {ben.id: 7.0})
    assert [f["athlete"].first_name for f in flags] == ["Ava", "Ben"]          # Sam: nothing stands out
    ava_f = flags[0]
    assert ava_f["signals"] == 3 and any("sleeping 7.0 h (usually 9.0)" in r for r in ava_f["reasons"])
    assert any(r.startswith("sore 3 of the last 3") for r in ava_f["reasons"]) and any("mood low" in r for r in ava_f["reasons"])
    assert flags[1]["rhr_up"] == 7.0 and flags[1]["signals"] == 2                # short sleep + resting heart rate


def test_pages_and_reminder(monkeypatch):
    db = SessionLocal()
    from xcapp.security import hash_password
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    sam, ava, _ = people(db)
    history(db, ava, 10, 9, 2, 4)
    for k in range(1, 3):
        db.add(Wellness(athlete_id=ava.id, date=TODAY - dt.timedelta(days=k), sleep_h=5, soreness=5, mood=1))
    db.commit()
    assert "Do your morning check-in (sleep, soreness, mood)." in RM.todo(db, sam, TODAY, ["checkin"])
    aid = ava.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    me = TestClient(app, follow_redirects=False)
    page = me.get("/a/t0").text
    assert 'id="checkin" open' in page and "Hours of sleep last night" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    me.post("/a/t0/checkin", data={"csrf": csrf, "sleep": "8", "soreness": "2"})
    assert "Tap one answer" in me.get("/a/t0").text
    me.post("/a/t0/checkin", data={"csrf": csrf, "sleep": "8", "soreness": "2", "mood": "4"})
    page = me.get("/a/t0").text
    assert "Check-in saved" in page and "✓ 8 h sleep · a little soreness · mood good" in page and 'id="checkin" open' not in page
    db = SessionLocal()
    assert RM.todo(db, db.get(Athlete, 1), TODAY, ["checkin"]) == []
    db.close()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    home = c.get("/").text
    assert "Check-in flags" in home and "Ava Lowry" in home and "3 signs" in home
    detail = c.get(f"/athletes/{aid}").text
    assert 'id="checkins"' in detail and "Very sore" in detail and "Awful" in detail
    assert "morning check-in" in c.get("/reminders").text

"""Announcements: entered on the coaches' home page, shown at the top of athletes' pages between their dates."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Announcement, Athlete, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam", ability_override="Gold"))
    db.add(Athlete(first_name="Ivy", last_name="Stone", gender="Girls", grade=10, group="JV", link_token="t-ivy"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app, user="coach1"):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/").text).group(1)


def test_announcements(app):
    c, csrf = login(app)
    home = c.get("/").text
    assert 'id="announcements"' in home and "New announcement" in home
    today = A.team_today()
    day = lambda n: (today + dt.timedelta(days=n)).isoformat()
    post = lambda **kw: c.post("/announcements", data={"csrf": csrf, **kw})
    post(title="Bus leaves 6:45 Saturday", body="Meet at the gym.\nBring spikes.", start_date=day(0), end_date=day(3))
    post(title="Gold group: tempo moved", start_date=day(-1), end_date=day(1), audience="Gold")
    post(title="Girls team photo", start_date=day(0), end_date=day(2), audience="Girls")
    post(title="Next week", start_date=day(5), end_date=day(9))
    post(title="Old news", start_date=day(-10), end_date=day(-2))
    post(title="Backwards", start_date=day(3), end_date=day(1))
    home = c.get("/").text
    assert "The end date is before the start date." in home and "Showing now" in home and "Coming up" in home and "Past announcements (1)" in home

    sam = TestClient(app).get("/a/t-sam").text
    assert sam.count('class="announcement"') == 2 and "Bus leaves 6:45 Saturday" in sam and "Meet at the gym.\nBring spikes." in sam
    assert "Gold group: tempo moved" in sam and "Girls team photo" not in sam and "Next week" not in sam and "Old news" not in sam
    ivy = TestClient(app).get("/a/t-ivy").text
    assert "Girls team photo" in ivy and "Gold group" not in ivy
    assert "Bus leaves" not in c.get("/athletes").text and "Bus leaves" not in c.get("/dashboard").text   # athletes' pages only

    db = SessionLocal()
    nid = db.query(Announcement).filter_by(title="Bus leaves 6:45 Saturday").one().id
    db.close()
    post(id=str(nid), title="Bus leaves 7:00 Saturday", start_date=day(0), end_date=day(3))
    assert "Bus leaves 7:00 Saturday" in TestClient(app).get("/a/t-sam").text
    post(id=str(nid), delete="1")
    assert "Bus leaves" not in TestClient(app).get("/a/t-sam").text

    t, tcsrf = login(app, "trainer1")
    assert 'id="announcements"' not in t.get("/").text
    assert t.post("/announcements", data={"csrf": tcsrf, "title": "x", "start_date": day(0), "end_date": day(1)}).status_code == 403

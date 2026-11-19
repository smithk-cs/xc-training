"""Practice attendance (attendance.py): saving marks, the season summary, and the page. Names are made up."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import attendance as AT
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Attendance, MissedDay, StaffUser
from xcapp.security import hash_password

TODAY = A.team_today()


def team(db):
    out = []
    for i, (first, g) in enumerate((("Sam", "Boys"), ("Ben", "Boys"), ("Ava", "Girls"))):
        a = Athlete(first_name=first, last_name="Lowry", gender=g, grade=11, group="Varsity", link_token=f"t{i}")
        db.add(a)
        out.append(a)
    db.flush()
    return out


def test_save_and_summary(session):
    sam, ben, ava = team(session)
    d1, d2, d3 = (TODAY - dt.timedelta(days=k) for k in (3, 2, 1))
    assert AT.save(session, d1, {sam.id: "present", ben.id: "absent", ava.id: "present"}, "coach1") == 3
    assert AT.save(session, d1, {sam.id: "present", ben.id: "excused"}, "coach1") == 1       # only what changed
    AT.save(session, d2, {sam.id: "late", ben.id: "absent", ava.id: "nonsense"}, "coach1")
    AT.save(session, d3, {sam.id: "present", ben.id: "present"}, "coach1")
    AT.save(session, d3, {ben.id: ""}, "coach1")                                             # cleared
    assert session.query(Attendance).count() == 6
    s = AT.summary(session, d1, TODAY, [sam.id, ben.id, ava.id])
    assert s[sam.id] == {"there": 3, "late": 1, "excused": 0, "absent": 0, "days": 3, "rate": 100, "missed": []}
    assert s[ben.id]["rate"] == 0 and s[ben.id]["excused"] == 1 and s[ben.id]["missed"] == [d2]
    assert s[ava.id]["rate"] == 100 and s[ava.id]["days"] == 1
    assert AT.marked_days(session, d1, TODAY) == [d1, d2, d3]


def test_page():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    sam, ben, ava = team(db)
    db.add(MissedDay(athlete_id=ben.id, date=TODAY, reason="Sick", description=""))
    db.commit()
    ids = (sam.id, ben.id, ava.id)
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert 'href="/attendance"' in c.get("/week").text                              # in the menu
    page = c.get("/attendance?team=Boys").text
    assert "Sam Lowry" in page and "Ava Lowry" not in page and "Told coaches: sick" in page and "Everyone else here" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/attendance", data={"csrf": csrf, "date": TODAY.isoformat(), "team": "Boys", "athletes": [str(ids[0]), str(ids[1])],
                                    f"s_{ids[0]}": "present", f"s_{ids[1]}": "excused"})
    assert r.status_code == 303 and r.headers["location"] == f"/attendance?date={TODAY.isoformat()}&team=Boys"
    page = c.get(r.headers["location"]).text
    assert "(2 changes)" in page and "1 here, 2 of 2 marked" in page
    assert re.search(rf'name="s_{ids[1]}" value="excused" checked', page)
    future = (TODAY + dt.timedelta(days=3)).isoformat()
    assert TODAY.strftime('%A %b %-d') in c.get(f"/attendance?date={future}").text   # no marking the future
    t = TestClient(app, follow_redirects=False)
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/login").text).group(1)
    t.post("/login", data={"csrf": tcsrf, "username": "trainer1", "password": "correct-horse-1"})
    tp = t.get("/attendance").text
    assert "Save attendance" not in tp and " disabled" in tp                         # the trainer can read, not mark
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', tp).group(1)
    assert t.post("/attendance", data={"csrf": tcsrf, "date": TODAY.isoformat(), "athletes": [str(ids[0])], f"s_{ids[0]}": "absent"}).status_code == 403

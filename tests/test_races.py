"""Race results at their real distances: import (optionally adding athletes), Races page, athlete pages, deletion."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import imports as I
from xcapp.db import SessionLocal
from xcapp.models import Athlete, RaceResult, Result, StaffUser
from xcapp.security import hash_password

MEET = """meet,date,course,race,distance,place,athlete,time,grade
Riverbend Invitational,2026-10-24,Riverbend Park,Boys Varsity Large School,"5,000 Meters",6,Sam Lowry,15:29.9,12
Riverbend Invitational,2026-10-24,Riverbend Park,Boys Varsity Large School,"5,000 Meters",30,Theo Marsh,16:31.7,11
Riverbend Invitational,2026-10-24,Riverbend Park,Girls JV,"4,000 Meters",12,"Quill, June",17:02.0,10
"""


@pytest.fixture
def staff():
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app, user):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c


def post_import(c, text, add=True):
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/races").text).group(1)
    data = {"csrf": csrf, **({"add_athletes": "yes"} if add else {})}
    r = c.post("/races/import", data=data, files={"file": ("meet.csv", text.encode(), "text/csv")})
    assert r.status_code == 303
    return re.findall(r'<div class="flash (\w+)"[^>]*>([^<]*)', c.get("/races").text)


def test_distances():
    assert [I.parse_distance(t) for t in ("5,000 Meters", "5000m", "4K", "3 Miles", "2 mi", "5 K")] == [
        5000.0, 5000.0, 4000.0, 4828.0, 3218.7, 5000.0]
    assert [I.benchmark_event(I.parse_distance(t)) for t in ("5,000 Meters", "4K", "2 Miles", "1 mile")] == [
        "5K", None, "2 Mile", "1 Mile"]
    for bad in ("far", "10 parsecs", "50"):
        with pytest.raises(ValueError):
            I.parse_distance(bad)


def test_import_creates_athletes_and_counts_5ks_toward_vdot(staff):
    c = login(staff, "coach1")
    assert '<a href="/races"' in c.get("/").text
    assert post_import(c, MEET) == [("ok", "Imported 3 race results and added 3 athletes to the roster.")]
    db = SessionLocal()
    people = {a.name: (a.gender, a.grade, a.group, a.active, bool(a.link_token)) for a in db.query(Athlete)}
    assert people == {"Sam Lowry": ("Boys", 12, "Varsity", True, True), "Theo Marsh": ("Boys", 11, "Varsity", True, True),
                      "June Quill": ("Girls", 10, "JV", True, True)}
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    rr = db.query(RaceResult).filter_by(athlete_id=sam.id).one()
    assert (rr.meet, rr.date, rr.race, rr.distance_m, rr.distance_label, rr.place, rr.time_s) == (
        "Riverbend Invitational", dt.date(2026, 10, 24), "Boys Varsity Large School", 5000.0, "5,000 Meters", 6, 929.9)
    res = db.get(Result, rr.result_id)
    assert (res.event, res.time_s, res.kind, res.course) == ("5K", 929.9, "race", "Riverbend Park")
    june = db.query(Athlete).filter_by(first_name="June").one()
    assert db.query(RaceResult).filter_by(athlete_id=june.id).one().result_id is None   # 4K isn't a benchmark distance
    assert A.abilities(db, dt.date(2026, 10, 25))[sam.id]["vdot"]                        # the 5K set a VDOT
    db.close()

    assert post_import(c, MEET) == [("ok", "Imported 0 race results.")]                  # same file again: nothing doubled
    db = SessionLocal()
    assert db.query(RaceResult).count() == 3 and db.query(Result).count() == 2 and db.query(Athlete).count() == 3
    db.close()

    page = c.get("/races").text
    assert "Riverbend Invitational" in page and "Boys Varsity Large School · 5,000 Meters" in page and "Girls JV · 4,000 Meters" in page
    assert "15:29.9" in page and "4:59" in page                                          # time and pace per mile
    assert "Riverbend Invitational" in c.get(f"/athletes/{sam.id}").text
    me = TestClient(staff).get(f"/a/{sam.link_token}").text
    assert "My races" in me and "Boys Varsity Large School · 5,000 Meters" in me and "Place 6" in me


def test_unknown_athletes_and_bad_rows_are_refused(staff):
    c = login(staff, "coach1")
    msgs = post_import(c, MEET, add=False)
    assert msgs[0][0] == "error" and "No athlete named &#39;Sam Lowry&#39; on the roster" in msgs[0][1]
    bad = MEET.replace("15:29.9", "fast").replace('"4,000 Meters"', "far")
    msgs = post_import(c, bad)
    assert "Line 2: Time &#39;fast&#39; should look like 16:42.3" in msgs[0][1] and "Line 4: Distance &#39;far&#39;" in msgs[0][1]
    nogender = "meet,date,race,distance,athlete,time,grade\nX,2026-10-01,Open,5K,Pat Reed,17:00,11\n"
    assert "add a gender (Boys or Girls) for new athlete &#39;Pat Reed&#39;" in post_import(c, nogender)[0][1]
    db = SessionLocal()
    assert db.query(Athlete).count() == 0 and db.query(RaceResult).count() == 0
    db.close()


def test_deleting_a_meet_and_trainer_access(staff):
    c = login(staff, "coach1")
    post_import(c, MEET)
    t = login(staff, "trainer1")
    page = t.get("/races").text
    assert "Riverbend Invitational" in page and "Import race results" not in page and "Delete this meet" not in page
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/").text).group(1)
    assert t.post("/races/delete", data={"csrf": tcsrf, "date": "2026-10-24", "meet": "Riverbend Invitational"}).status_code == 403

    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/races").text).group(1)
    c.post("/races/delete", data={"csrf": csrf, "date": "2026-10-24", "meet": "Riverbend Invitational"})
    assert "Deleted Riverbend Invitational (3 results)." in c.get("/races").text
    db = SessionLocal()
    assert db.query(RaceResult).count() == 0 and db.query(Result).count() == 0 and db.query(Athlete).count() == 3
    db.close()

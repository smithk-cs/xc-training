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


HISTORY = """meet,date,course,race,distance,place,athlete,time,grade,gender
Riverbend Invitational,2025-10-25,Riverbend Park,Boys JV,5K,20,Sam Lowry,16:20.0,11,Boys
Hilltop Classic,2025-11-01,Hilltop Farm,Boys Varsity,"4,000 Meters",9,Sam Lowry,13:05.0,11,Boys
Riverbend Invitational,2026-10-24,Riverbend Park,Boys Varsity Large School,"5,000 Meters",6,Sam Lowry,15:29.9,12,Boys
Lakeside Open,2026-10-10,Lakeside Course,Boys Varsity,"5,000 Meters",3,Sam Lowry,15:45.5,12,Boys
"""


def test_race_history_by_year_and_comparisons(staff):
    c = login(staff, "coach1")
    post_import(c, HISTORY)
    db = SessionLocal()
    races = db.query(RaceResult).order_by(RaceResult.date.desc()).all()
    h = A.race_history(races)
    assert [(s, [x["r"].meet for x in rows]) for s, rows in h["seasons"]] == [
        ("2026 XC", ["Riverbend Invitational", "Lakeside Open"]), ("2025 XC", ["Hilltop Classic", "Riverbend Invitational"])]
    marks = {(x["r"].date.year, x["r"].meet): (x["pr"], x["sb"]) for _, rows in h["seasons"] for x in rows}
    assert marks[(2026, "Riverbend Invitational")] == (True, True)      # best 5K ever ("5K" and "5,000 Meters" match)
    assert marks[(2026, "Lakeside Open")] == (False, False)
    assert marks[(2025, "Riverbend Invitational")] == (False, True)     # best 5K that season
    assert marks[(2025, "Hilltop Classic")] == (True, True)             # only 4K: its own distance
    assert [g["label"] for g in h["compare"]] == ["Riverbend Park · 5,000 Meters (2 races)", "Every 5,000 Meters race (3)"]
    same_course = h["compare"][0]["rows"]
    assert [(x["r"].date.year, x["vs_prev"], x["vs_best"]) for x in same_course] == [(2025, None, 50.1), (2026, -50.1, 0.0)]
    assert [round(x["vs_prev"], 1) if x["vs_prev"] is not None else None for x in h["compare"][1]["rows"]] == [None, -34.5, -15.6]
    assert [A.fmt_gap(x) for x in (-50.1, 15.6, 65.2, 0.0, None)] == ["−50.1 s", "+15.6 s", "+1:05.2", "same", ""]
    token = db.query(Athlete).one().link_token
    db.close()

    page = TestClient(staff).get(f"/a/{token}").text
    assert '<select id="race-view"' in page and "Compare: Riverbend Park · 5,000 Meters (2 races)" in page
    assert '<h3 class="season">2026 XC <span class="small muted">2 races</span></h3>' in page
    assert '<span class="pill good">PR</span>' in page and '<span class="pill">SB</span>' in page
    assert 'class="num nowrap faster">−50.1 s</td>' in page and 'class="num nowrap">+15.6 s</td>' in page   # faster than last; gap to best
    c2 = login(staff, "coach1")
    post_import(c2, "meet,date,course,race,distance,athlete,time\nCity Finals,2026-11-07,Riverbend Park,Boys Varsity,5K,Sam Lowry,15:40.0\n")
    page = TestClient(staff).get(f"/a/{token}").text
    assert 'class="num nowrap slower">+10.1 s</td>' in page                                             # slower than the 15:29.9


def test_one_race_has_nothing_to_compare(staff):
    c = login(staff, "coach1")
    post_import(c, MEET)
    db = SessionLocal()
    token = db.query(Athlete).filter_by(first_name="Sam").one().link_token
    db.close()
    page = TestClient(staff).get(f"/a/{token}").text
    assert "My races" in page and "2026 XC" in page and 'id="race-view"' not in page


def test_editing_a_meet_and_a_race_distance(staff):
    c = login(staff, "coach1")
    post_import(c, MEET)
    page = c.get("/races").text
    assert "Edit meet" in page and "Edit race" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    # The 4K was really a 5K: its time now counts toward VDOT.
    c.post("/races/edit-race", data={"csrf": csrf, "date": "2026-10-24", "meet": "Riverbend Invitational", "race": "Girls JV",
                                     "meters": "4000.0", "new_race": "Girls JV", "distance": "5,000 Meters"})
    assert "Saved Girls JV at 5,000 Meters (1 results). These times now count toward VDOT." in c.get("/races").text
    db = SessionLocal()
    june = db.query(RaceResult).filter_by(race="Girls JV").one()
    assert (june.distance_m, june.distance_label) == (5000.0, "5,000 Meters") and db.get(Result, june.result_id).event == "5K"
    db.close()
    # The boys' 5K was really 4K: the VDOT times go.
    c.post("/races/edit-race", data={"csrf": csrf, "date": "2026-10-24", "meet": "Riverbend Invitational", "race": "Boys Varsity Large School",
                                     "meters": "5000.0", "new_race": "Boys Varsity", "distance": "4K"})
    db = SessionLocal()
    boys = db.query(RaceResult).filter_by(race="Boys Varsity").all()
    assert len(boys) == 2 and all(r.distance_m == 4000 and r.result_id is None for r in boys) and db.query(Result).count() == 1
    db.close()
    c.post("/races/edit-race", data={"csrf": csrf, "date": "2026-10-24", "meet": "Riverbend Invitational", "race": "Boys Varsity",
                                     "meters": "4000.0", "distance": "far"})
    assert "Distance &#39;far&#39; should look like 5K" in c.get("/races").text
    c.post("/races/edit-meet", data={"csrf": csrf, "date": "2026-10-24", "meet": "Riverbend Invitational", "name": "Riverbend XC Classic",
                                     "new_date": "2026-10-25", "course": "Riverbend Park North"})
    db = SessionLocal()
    assert {(r.meet, r.date, r.course) for r in db.query(RaceResult)} == {("Riverbend XC Classic", dt.date(2026, 10, 25), "Riverbend Park North")}
    res = db.query(Result).one()
    assert (res.date, res.course) == (dt.date(2026, 10, 25), "Riverbend Park North")
    db.close()
    t = login(staff, "trainer1")
    assert "Edit race" not in t.get("/races").text


def test_season_tabs(staff):
    c = login(staff, "coach1")
    post_import(c, HISTORY)                                  # races in 2025 XC and 2026 XC
    page = c.get("/races").text
    assert '<nav class="tabs" aria-label="Seasons">' in page and '>2026 XC <span class="small">(2)</span></a>' in page
    assert 'href="/races?season=2026%20XC" aria-current="page"' in page                     # latest season first
    assert "Lakeside Open" in page and "Hilltop Classic" not in page
    older = c.get("/races?season=2025 XC").text
    assert "Hilltop Classic" in older and "Lakeside Open" not in older
    both = c.get("/races?season=all").text
    assert "Hilltop Classic" in both and "Lakeside Open" in both

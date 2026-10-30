"""Courses and race-day conditions: adjusted times on a common baseline (track, 55°F, sea level, low dew point)."""
import datetime as dt

import pytest

from xcapp import courses as C
from xcapp.models import Course, MeetConditions


def test_daniels_numbers():
    assert C.temp_pct(55) == 0 and C.temp_pct(40) == 0
    assert C.temp_pct(60) == pytest.approx(0.77, abs=0.01)                 # 0.77% slower per 5°F over 55
    assert C.altitude_pct(500) == 0 and C.altitude_pct(3281) == pytest.approx(0.675)
    assert C.altitude_pct(5741.5) == pytest.approx((1.95 + 3.25) / 2)        # halfway between 1,500 m and 2,000 m
    assert [C.dew_pct(x) for x in (None, 50, 58, 63, 68, 75)] == [0, 0, 0.5, 1.0, 2.0, 3.0]


def test_course_factor():
    flat = Course(name="Track", surface="Track", turns="Few", elevation_gain_ft=10, distance_m=5000)
    assert C.hill_tier(flat) == 1 and C.course_seconds(flat) == 0
    hilly = Course(name="Hills", surface="Firm grass", turns="Some", elevation_gain_ft=200, distance_m=5000)   # 64 ft a mile
    assert C.hill_tier(hilly) == 4 and C.course_seconds(hilly) == 18 + 5 + 2
    hilly.hill_tier = 2
    assert C.course_seconds(hilly) == 5 + 5 + 2                            # the coach's tier wins
    hilly.adjust_s_per_mile = 30
    assert C.course_seconds(hilly) == 30                                   # and the coach's own figure wins over everything
    assert C.hill_tier(Course(name="x")) is None


def test_adjusted_time():
    assert C.adjusted_time(1080, 5000, None, None) is None
    course = Course(name="Gibson", surface="Firm grass", turns="Some", elevation_gain_ft=60, distance_m=5000)  # tier 2: 12 s/mi
    hot = MeetConditions(meet="m", date=dt.date(2026, 9, 5), temp_f=85, dew_point_f=66, footing="Dry")
    miles = 5000 / 1609.344
    pace = 1080 / miles
    want = (pace / (1 + (30 * 0.154 + 2.0) / 100) - 12) * miles
    assert C.adjusted_time(1080, 5000, course, hot) == pytest.approx(want)
    assert C.adjusted_time(1080, 5000, course, hot) < C.adjusted_time(1080, 5000, course, None) < 1080
    mud = MeetConditions(meet="m", date=dt.date(2026, 9, 5), temp_f=50, footing="Muddy")
    assert C.adjusted_time(1080, 5000, None, mud) == pytest.approx((pace - 10) * miles)
    assert "for the course and footing" in C.explain(None, mud) and C.explain(None, MeetConditions(meet="m", date=dt.date(2026, 1, 1))) == "no adjustment"


def test_courses_page_and_race_conditions():
    import re

    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, RaceResult, StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava")
    db.add(a)
    db.flush()
    db.add(RaceResult(athlete_id=a.id, meet="Flat SAC", date=dt.date(2026, 10, 24), course="Gibson Ranch", race="Girls Varsity",
                      distance_m=5000, distance_label="5,000 Meters", time_s=1080, place=3))
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()

    def login(user):
        c = TestClient(app, follow_redirects=False)
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
        return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/courses").text).group(1)

    c, csrf = login("coach1")
    page = c.get("/courses").text
    assert "No courses yet." in page and "not added here yet: Gibson Ranch" in page
    base = {"csrf": csrf, "name": "Gibson Ranch", "location": "Elverta", "dist_5K": "on", "dist_2 mi": "on", "surface": "Firm grass",
            "turns": "Some", "elevation_gain_ft": "60", "altitude_ft": "100", "hill_tier": "", "adjust_s_per_mile": "", "map_url": "", "notes": "Two loops"}
    c.post("/courses", data={**base, "map_url": "javascript:alert(1)"})
    assert "should start with https://" in c.get("/courses").text
    c.post("/courses", data=base)
    page = c.get("/courses").text
    assert "Added Gibson Ranch." in page and '<span class="tag">5K</span>' in page and '<span class="tag">2 mi</span>' in page and "12 s/mi" in page and "not added here yet" not in page
    assert "Two loops" not in page                                                  # notes stay in the edit form, not the list
    form = c.get("/courses").text
    assert 'name="dist_2.25 mi"' in form and 'name="dist_1 mi"' not in form and 'name="dist_10K"' not in form and "Usual distance" not in form
    c.post("/courses", data={**base, "name": "gibson ranch"})
    assert "already a course called Gibson Ranch" in c.get("/courses").text

    races = c.get("/races?season=all").text
    assert "Course: <a href=\"/courses\">Gibson Ranch</a>" in races and "Adjusted times: 12 s/mile for the course" in races
    c.post("/races/conditions", data={"csrf": csrf, "date": "2026-10-24", "meet": "Flat SAC", "course_id": "", "temp_f": "85",
                                      "dew_point_f": "66", "humidity_pct": "", "wind_mph": "8", "footing": "Dry", "notes": ""})
    races = c.get("/races?season=all").text
    assert "Race day: 85°F, dew point 66°F, wind 8 mph, dry." in races and "6.6% for heat, humidity and altitude" in races
    adj = C.adjusted_time(1080, 5000, Course(name="g", surface="Firm grass", turns="Some", elevation_gain_ft=60, distance_m=5000.2),
                          MeetConditions(meet="m", date=dt.date(2026, 1, 1), temp_f=85, dew_point_f=66))
    from xcapp.analysis import fmt_time
    assert fmt_time(adj) in races
    c.post("/races/conditions", data={"csrf": csrf, "date": "2026-10-24", "meet": "Flat SAC", "temp_f": "200"})
    assert "Temperature should be from -20 to 120." in c.get("/races?season=all").text

    db = SessionLocal()
    odd = Course(name="Odd Loop", distance_m=5100)
    db.add(odd)
    db.commit()
    oid = odd.id
    db.close()
    edit_page = c.get(f"/courses?edit={oid}").text
    assert "Usual distance" not in edit_page
    db = SessionLocal()
    g = db.query(Course).filter_by(name="Gibson Ranch").one()
    g.distances, gid = "5K,3 mi", g.id
    db.commit()
    db.close()
    assert '<input type="checkbox" name="dist_3 mi" checked>' in c.get(f"/courses?edit={gid}").text   # a retired choice stays ticked
    no_ticks = {k: v for k, v in base.items() if not k.startswith("dist_")}
    c.post("/courses", data={**no_ticks, "cid": str(oid), "name": "Odd Loop"})
    db = SessionLocal()
    assert db.get(Course, oid).distance_m == 5100                                  # a saved odd distance is kept
    db.close()

    t, tcsrf = login("trainer1")
    assert "Add a course" not in t.get("/courses").text and "Race-day conditions" not in t.get("/races?season=all").text
    assert t.post("/courses", data={**base, "csrf": tcsrf}).status_code == 403


def test_course_video_on_athletes_upcoming_meets():
    import re

    from fastapi.testclient import TestClient

    from xcapp import analysis as A
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, MeetEntry, StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava"))
    soon = A.team_today() + dt.timedelta(days=10)
    db.add(MeetEntry(name="Clovis Invite", date=soon, location="Woodward Park, Fresno"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/courses").text).group(1)
    form = {"csrf": csrf, "name": "Woodward Park", "surface": "Firm grass", "turns": "Some", "video_url": "http://youtube.com/x"}
    c.post("/courses", data=form)
    assert "The video link should start with https://" in c.get("/courses").text
    c.post("/courses", data={**form, "video_url": "https://youtu.be/abc123"})
    assert 'href="https://youtu.be/abc123" target="_blank" rel="noopener">video</a>' in c.get("/courses").text
    me = TestClient(app).get("/a/t-ava").text
    assert '<a class="button secondary" href="https://youtu.be/abc123" target="_blank" rel="noopener">▶ Course video: Woodward Park</a>' in me   # matched by location
    db = SessionLocal()
    meet = db.query(MeetEntry).one()
    other = Course(name="Lagoon Valley", video_url="https://youtu.be/lagoon")
    db.add(other)
    db.commit()
    mid, oid = meet.id, other.id
    db.close()
    page = c.get(f"/registrations/{mid}").text
    assert "Woodward Park (matched by name)" in page and "Lagoon Valley (video)" in page
    c.post(f"/registrations/{mid}", data={"csrf": csrf, "name": "Clovis Invite", "date": soon.isoformat(), "location": "Woodward Park, Fresno",
                                          "director_name": "", "director_email": "", "notes": "", "course_id": str(oid)})
    assert "▶ Course video: Lagoon Valley" in TestClient(app).get("/a/t-ava").text    # the chosen course wins


def test_course_distances():
    assert C.distance_label(5000) == "5K" and C.distance_label(2 * 1609.344) == "2 mi" and C.distance_label(5100) == "3.17 mi"
    assert list(C.KM_DISTANCES)[:3] == ["2K", "3K", "4K"] and "1.5 mi" in C.MILE_DISTANCES



def test_main_distance_and_retired_choices():
    from xcapp import courses as C
    assert C.main_distance(["2 mi", "5K"], None) == 5000 and C.main_distance(["2 mi", "2.5 mi"], None) == C.DISTANCES["2.5 mi"]
    assert C.main_distance(["2 mi", "5K"], C.DISTANCES["2 mi"]) == C.DISTANCES["2 mi"]       # the saved one, while still ticked
    assert C.main_distance([], 5000) is None and C.distance_label(C.DISTANCES["3 mi"]) == "3 mi"   # older labels still read


def test_course_breakdown_flags_missing_hills():
    from xcapp import courses as C
    from xcapp.models import Course
    flat_by_default = Course(name="Foresthill", surface="Firm grass", turns="Few", distance_m=5000)
    assert C.course_seconds(flat_by_default) == 5 and C.breakdown(flat_by_default)[0] == ("hills not set", 0)
    hilly = Course(name="Foresthill", surface="Firm grass", turns="Few", distance_m=5000, elevation_gain_ft=400)   # 129 ft/mile
    assert C.breakdown(hilly)[0] == ("hills (tier 5)", 28) and C.course_seconds(hilly) == 33

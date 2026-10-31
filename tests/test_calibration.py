"""Race and course difficulty measured against the runners' own track benchmarks."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import calibration as K
from xcapp import courses as C
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Course, MeetConditions, RaceResult, Result, StaffUser
from xcapp.security import hash_password

BENCH_DAY, RACE_DAY = dt.date(2026, 9, 5), dt.date(2026, 10, 3)


def test_prediction_matches_daniels():
    v = A.vdot(3218.688, 9 * 60 + 30)                   # 9:30 two mile
    assert round(v, 1) == 68.0 and A.fmt_time(K.predicted_time(v, 5000)) == "15:17.8"
    assert K.predicted_time(v, 5000) == pytest.approx(K.predicted_time(v, 5000))
    assert A.vdot(5000, K.predicted_time(v, 5000)) == pytest.approx(v, abs=1e-6)


@pytest.fixture
def seeded():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    course = Course(name="Gibson Ranch", surface="Firm grass", turns="Few", distance_m=5000)   # app thinks 5 s/mile
    db.add(course)
    db.add(MeetConditions(meet="Gibson Invite", date=RACE_DAY, temp_f=55))
    db.flush()
    slowdowns = [0.040, 0.035, 0.045, 0.042, 0.038, 0.041, 0.20]    # the last runner had an off day
    for i, s in enumerate(slowdowns):
        a = Athlete(first_name=f"R{i}", last_name=f"Runner{i}", gender="Boys", grade=11, group="Varsity", link_token=f"k{i}")
        db.add(a)
        db.flush()
        two_mile = 9 * 60 + 40 + i * 10
        db.add(Result(athlete_id=a.id, event="2 Mile", time_s=two_mile, date=BENCH_DAY, season="2026 XC", course="Grizzly",
                      note="Grizzly 3200", kind="race"))
        db.add(Result(athlete_id=a.id, event="2 Mile", time_s=two_mile - 30, date=BENCH_DAY, season="2026 XC", course="Hills",
                      note="Hill time trial", kind="time trial"))   # not a track meet: not ticked
        pred = K.predicted_time(A.vdot(3218.688, two_mile), 5000)
        db.add(RaceResult(athlete_id=a.id, date=RACE_DAY, meet="Gibson Invite", race="Boys Varsity", course="Gibson Ranch",
                          distance_m=5000, distance_label="5K", time_s=pred * (1 + s)))
    db.commit()
    cid = course.id
    db.close()
    return cid


def test_report_measures_the_course_and_sets_aside_off_days(seeded):
    db = SessionLocal()
    keys = K.chosen(db)
    assert keys == [f"{BENCH_DAY.isoformat()}|Grizzly 3200"]            # ticked by its name; the hill trial isn't
    rep = K.report(db, "2026 XC", keys)
    race = rep.races[0]
    assert race["n"] == 6 and race["set_aside"] == 1
    assert race["observed"] == pytest.approx(4.05, abs=0.1)               # the median slowdown, off day left out
    c = rep.courses[0]
    assert c["current"] == 5 and c["n"] == 6
    pace = 5 * 60 + 2
    assert c["implied"] == pytest.approx(0.0405 * pace, abs=2.5)          # about 12 s/mile
    assert c["suggested"] == round(5 + 6 / 16 * (c["implied"] - 5))       # blended: 6 results of 16
    db.close()


def test_page_and_using_the_suggestion(seeded):
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/courses/check?season=2026+XC").text
    assert "Gibson Invite" in page and "set aside" in page and "Use " in page and "Grizzly 3200" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    db = SessionLocal()
    want = K.report(db, "2026 XC", K.chosen(db)).courses[0]["suggested"]
    db.close()
    c.post(f"/courses/{seeded}/use-suggested", data={"csrf": csrf, "season": "2026 XC"})
    db = SessionLocal()
    assert db.get(Course, seeded).adjust_s_per_mile == want
    db.close()
    c.post("/courses/check/benchmarks", data={"csrf": csrf, "season": "2026 XC"})   # nothing ticked
    assert "No runners have a ticked benchmark yet" in c.get("/courses/check?season=2026+XC").text

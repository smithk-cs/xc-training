import datetime as dt
import re

from xcapp import data_check as DC
from xcapp.models import Activity, Lap, PlannedWorkout

TUE = dt.date(2026, 10, 20)
PRACTICE = dt.datetime(2026, 10, 20, 22, 30)          # 3:30 PM Pacific (PDT), stored as UTC


def run(session, athlete, ext, start, miles=5.0, mins=40.0, source="garmin_file", day=None, laps=0, rpe=None, sport="running"):
    a = Activity(athlete_id=athlete.id, source=source, external_id=ext, start_utc=start, local_date=day or TUE,
                 duration_s=mins * 60, distance_m=miles * 1609.344, sport=sport, rpe=rpe)
    for i in range(laps):
        a.laps.append(Lap(idx=i, start_offset_s=i * 60, elapsed_s=60, timer_s=60, distance_m=400))
    session.add(a)
    session.flush()
    return a


def test_finds_copies_wrong_days_and_odd_runs(session, athlete):
    file = run(session, athlete, "f1", PRACTICE, laps=3)
    summary = run(session, athlete, "s1", PRACTICE + dt.timedelta(minutes=3), source="garmin_summary", rpe=6)  # 3 min off
    csv = run(session, athlete, "c1", PRACTICE + dt.timedelta(hours=7), source="garmin_csv")   # read as UTC: 7 h off
    second = run(session, athlete, "f2", PRACTICE - dt.timedelta(hours=8), miles=3, mins=25)  # a morning run: not a copy
    late = run(session, athlete, "f3", dt.datetime(2026, 10, 22, 5, 30), miles=4, mins=32, day=dt.date(2026, 10, 22))  # 10:30 PM Wed, filed Thu
    car = run(session, athlete, "f4", dt.datetime(2026, 10, 21, 1, 0), miles=12, mins=20, day=dt.date(2026, 10, 20))  # 6 PM Tue, 1:40/mi
    issues = DC.find_issues(session, TUE - dt.timedelta(days=6), TUE + dt.timedelta(days=3), today=TUE + dt.timedelta(days=3))
    pairs = {(p["keep"].id, p["drop"].id): p["why"] for p in issues["duplicates"]}
    assert pairs == {(file.id, summary.id): "overlapping times", (file.id, csv.id): "the same distance and time, 7 h apart (a time-zone slip)"}
    assert [(w["act"].id, w["should_be"]) for w in issues["wrong_day"]] == [(late.id, dt.date(2026, 10, 21))]
    odd = {o["act"].id: o["why"] for o in issues["odd"]}
    assert list(odd) == [car.id] and "faster than 4:00 a mile (1:40/mi)" in odd[car.id][0]
    assert second.id not in odd and issues["checked"] == 6


def test_remove_keeps_the_athletes_effort_and_fix_days(session, athlete):
    session.add(PlannedWorkout(date=TUE, title="Tempo", rep_distance_m=0, groups="All"))
    file = run(session, athlete, "f1", PRACTICE, laps=3)
    summary = run(session, athlete, "s1", PRACTICE + dt.timedelta(minutes=2), source="garmin_summary", rpe=7)
    summary.feel = "Heavy"
    DC.remove_duplicate(session, summary, file)
    assert session.get(Activity, summary.id) is None and file.rpe == 7 and file.feel == "Heavy"
    assert file.planned_workout_id is not None                       # the kept run is the day's workout
    late = run(session, athlete, "f3", dt.datetime(2026, 10, 22, 5, 30), day=dt.date(2026, 10, 22))
    assert DC.fix_days(session) == 1 and late.local_date == dt.date(2026, 10, 21)


def test_data_check_page(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp import config
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    monkeypatch.setenv("AS_OF_DATE", "2026-10-23")
    config.reload_settings()
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava")
    db.add(a)
    db.flush()
    keep = run(db, a, "f1", PRACTICE, laps=2).id
    drop = run(db, a, "s1", PRACTICE + dt.timedelta(minutes=1), source="garmin_summary").id
    late = run(db, a, "f3", dt.datetime(2026, 10, 22, 5, 30), day=dt.date(2026, 10, 22)).id
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert 'href="/data-check"' in c.get("/sync").text
    page = c.get("/data-check").text
    assert "1 duplicate</span>" in page and "1 on the wrong day" in page and f'name="id" value="{drop}" checked' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/data-check/remove", data={"csrf": csrf, "days": "28", "id": [str(keep)]})   # not a suggested copy: left alone
    c.post("/data-check/remove", data={"csrf": csrf, "days": "28", "id": [str(drop)]})
    c.post("/data-check/fix-days", data={"csrf": csrf, "days": "28", "id": [str(late)]})
    page = c.get("/data-check").text
    assert "all look right" in page
    db = SessionLocal()
    assert {x.id for x in db.query(Activity).all()} == {keep, late}
    assert db.get(Activity, late).local_date == dt.date(2026, 10, 21)
    db.close()


def test_clear_garmin_copies_are_removed_automatically(session, athlete):
    file = run(session, athlete, "f1", PRACTICE, laps=3)
    summary = run(session, athlete, "s1", PRACTICE, source="garmin_summary", rpe=6)        # the same run, sent twice
    other_file = run(session, athlete, "f2", PRACTICE + dt.timedelta(days=1), day=TUE + dt.timedelta(days=1))
    upload = run(session, athlete, "u1", PRACTICE + dt.timedelta(days=1, minutes=1), source="upload", day=TUE + dt.timedelta(days=1))
    shifted = run(session, athlete, "c1", PRACTICE + dt.timedelta(days=2, hours=7), source="garmin_summary", day=TUE + dt.timedelta(days=2))
    shifted_file = run(session, athlete, "f3", PRACTICE + dt.timedelta(days=2), day=TUE + dt.timedelta(days=2))
    short = run(session, athlete, "s9", PRACTICE + dt.timedelta(days=3), miles=2, source="garmin_summary", day=TUE + dt.timedelta(days=3))
    long = run(session, athlete, "f9", PRACTICE + dt.timedelta(days=3), miles=5, day=TUE + dt.timedelta(days=3))
    removed = DC.auto_remove(session, TUE, TUE + dt.timedelta(days=3))
    assert len(removed) == 1 and "Lucia Sorensen" in removed[0] and "garmin_summary, kept garmin_file" in removed[0]
    assert session.get(Activity, summary.id) is None and file.rpe == 6           # the athlete's effort moved over
    # Left for a coach: a manual upload, a time-zone slip, and copies whose distances don't match
    for a in (other_file, upload, shifted, shifted_file, short, long):
        assert session.get(Activity, a.id) is not None
    left = DC.find_issues(session, TUE, TUE + dt.timedelta(days=3), today=TUE + dt.timedelta(days=3))["duplicates"]
    assert len(left) == 3 and not any(DC.certain(p) for p in left)

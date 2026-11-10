"""Daniels' session limits (limits.py) and VDOT suggestions from workouts (vdot_suggest.py)."""
import datetime as dt
import json
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import limits as LM
from xcapp import vdot_suggest as VS
from xcapp.models import Activity, Athlete, Lap, PainReport, PlannedWorkout, StaffUser, VdotChange

MILE = A.METERS_PER_MILE


def person(name, goal, group="Varsity", v=50.0, gender="Boys"):
    return {"a": SimpleNamespace(name=name, group="", gender=gender), "vdot": v, "group": group, "goal": goal}


def workout(parts, groups="All", gender=""):
    return PlannedWorkout(id=1, date=dt.date(2026, 10, 20), title="t", rep_distance_m=1000, parts=json.dumps(parts),
                          groups=groups, gender=gender)


def test_interval_limit_by_weekly_miles():
    w = workout([{"sets": 1, "reps": 6, "dist": 1000, "zone": "Interval", "rest": "2 min"}])   # 6 km = 3.73 mi at I
    over = LM.check_one(w, [person("Ann", 25), person("Ben", 30), person("Cal", 60), person("Dee", 0)])
    assert [z["zone"] for z in over] == ["Interval"] and over[0]["count"] == 2           # Cal's 60 mi: 8% = 4.8 mi, fine
    rows = {r["goal"]: r for r in over[0]["groups"]}
    assert round(rows[25]["limit"], 2) == 2.0 and rows[25]["fit"] == "at most 3 × 1K" and rows[25]["names"] == ["Ann"]
    assert round(rows[30]["limit"], 2) == 2.4 and rows[30]["fit"] == "at most 3 × 1K"
    assert "8% of the week's miles, at most 6.2 mi" in over[0]["rule"]
    assert LM.summary(over) == "Over Daniels' Interval limit for 2 athletes"


def test_caps_timed_parts_long_runs_and_who_it_applies_to():
    big = workout([{"sets": 1, "reps": 12, "dist": 1000, "zone": "Interval", "rest": ""}])      # 12 km: over the 10 km cap
    assert LM.check_one(big, [person("Huge", 150)])[0]["groups"][0]["fit"] == "at most 10 × 1K"
    tempo = workout([{"sets": 1, "reps": 1, "mins": 30, "zone": "Threshold", "rest": ""}])
    miles = LM.zone_miles(tempo.part_list, 50)["Threshold"]
    assert abs(miles - 30 * 60 / A.pace_at(50, A.WORKOUT_ZONES["Threshold"])) < 1e-9
    assert LM.check_one(tempo, [person("Low", 30)]) and not LM.check_one(tempo, [person("High", 45)])   # 10% of the week
    long_run = workout([{"sets": 1, "reps": 1, "dist": round(9 * MILE), "mi": 9, "zone": "Easy", "rest": ""}])
    over = LM.check_one(long_run, [person("Short", 30), person("Long", 40)])                   # 25%: 7.5 and 10 mi
    assert over[0]["zone"] == "Long run" and over[0]["groups"][0]["names"] == ["Short"]
    easy_plus = workout([{"sets": 1, "reps": 1, "dist": round(4 * MILE), "mi": 4, "zone": "Easy", "rest": ""},
                         {"sets": 1, "reps": 1, "fill": True, "zone": "Easy", "rest": ""}])
    assert not LM.check_one(easy_plus, [person("Ann", 20)])
    varsity_only = workout([{"sets": 1, "reps": 8, "dist": 1000, "zone": "Interval", "rest": ""}], groups="Varsity")
    assert LM.check_one(varsity_only, [person("V", 25, group="Varsity"), person("JV", 25, group="JV")])[0]["count"] == 1
    girls_only = workout([{"sets": 1, "reps": 8, "dist": 1000, "zone": "Interval", "rest": ""}], gender="Girls")
    assert not LM.check_one(girls_only, [person("Boy", 25)])


# ---------- VDOT suggestions ----------

TODAY = dt.date(2026, 11, 10)


def setup_athlete(session, vdot=50, changed_days_ago=35):
    a = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="tok-sam",
                vdot_override=vdot, hr_max=200)
    session.add(a)
    session.flush()
    session.add(VdotChange(athlete_id=a.id, date=TODAY - dt.timedelta(days=changed_days_ago), vdot=vdot, previous=None,
                           by="coach", source="test"))
    session.flush()
    return a


def t_session(session, a, day, ratio, hr, zone="Threshold", n=4, vdot=50):
    pw = PlannedWorkout(date=day, title=f"{n} × 1 mile {zone}", rep_distance_m=MILE, groups="All",
                        parts=json.dumps([{"sets": 1, "reps": n, "dist": round(MILE), "zone": zone, "rest": "1 min"}]))
    session.add(pw)
    session.flush()
    pace = A.pace_at(vdot, A.WORKOUT_ZONES[zone]) * ratio
    act = Activity(athlete_id=a.id, source="garmin_file", external_id=f"x{day}{zone}", start_utc=dt.datetime.combine(day, dt.time(23)),
                   local_date=day, duration_s=3000, distance_m=8 * MILE, sport="running", planned_workout_id=pw.id, workout_locked=True)
    for i in range(n):
        act.laps.append(Lap(idx=i, start_offset_s=i * 400, elapsed_s=pace, timer_s=pace, distance_m=MILE, avg_hr=hr * 2,
                            is_rep=True, rep_num=i + 1))
    session.add(act)
    session.flush()


def test_ready_to_raise_after_four_weeks(session):
    a = setup_athlete(session)
    for i, (r, hr) in enumerate([(0.98, 88), (0.985, 87), (0.98, 87)]):
        t_session(session, a, TODAY - dt.timedelta(days=20 - 7 * i), r, hr)
    [h] = VS.suggestions(session, TODAY)
    assert (h["kind"], h["vdot"], h["to"]) == ("up", 50, 51) and "faster than target" in h["why"]
    assert len(h["sessions"]) == 3 and round(h["sessions"][0]["hr_pct"]) == 88
    # Not while pain is reported, and not before four weeks at this VDOT
    session.add(PainReport(athlete_id=a.id, date=TODAY - dt.timedelta(days=3), body_area="Shin", severity=4))
    session.flush()
    assert VS.suggestions(session, TODAY) == []


def test_waits_four_weeks_and_heart_rate_counts(session):
    a = setup_athlete(session, changed_days_ago=21)
    for i, (r, hr) in enumerate([(1.0, 90), (1.0, 89), (0.995, 87)]):     # on pace, heart rate down 3 points
        t_session(session, a, TODAY - dt.timedelta(days=18 - 7 * i), r, hr)
    assert VS.suggestions(session, TODAY) == []                               # only 21 days at this VDOT
    assert VS._judge([{"ratio": 1.0, "hr_pct": 90}, {"ratio": 1.0, "hr_pct": 89}, {"ratio": 0.995, "hr_pct": 87}], 30, False)[0] == "up"
    assert VS._judge([{"ratio": 1.0, "hr_pct": 88}, {"ratio": 1.0, "hr_pct": 88}, {"ratio": 1.0, "hr_pct": 88}], 30, False) is None


def test_worth_a_check_when_missing_paces_and_dismiss(session):
    a = setup_athlete(session, changed_days_ago=10)        # lowering doesn't wait four weeks
    for i, r in enumerate([1.04, 1.0, 1.05]):
        t_session(session, a, TODAY - dt.timedelta(days=8 - 3 * i), r, 92, zone="Interval")
    [h] = VS.suggestions(session, TODAY)
    assert (h["kind"], h["to"]) == ("down", 49) and "2 of the last 3" in h["why"]
    VS.dismiss(session, a.id, h["key"])
    assert VS.suggestions(session, TODAY) == []
    t_session(session, a, TODAY - dt.timedelta(days=1), 1.06, 93, zone="Interval")   # a newer session brings it back
    assert VS.suggestions(session, TODAY)[0]["kind"] == "down"


def test_vdot_page_apply(monkeypatch):
    monkeypatch.setenv("AS_OF_DATE", TODAY.isoformat())
    from xcapp import config
    config.reload_settings()
    from xcapp.db import SessionLocal
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = setup_athlete(db)
    for i, r in enumerate([0.98, 0.98, 0.97]):
        t_session(db, a, TODAY - dt.timedelta(days=20 - 7 * i), r, 88)
    db.commit()
    aid = a.id
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/vdot").text
    assert "Suggested VDOT changes" in page and "Ready to raise" in page and "Set VDOT 51" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/vdot/suggest", data={"csrf": csrf, "athlete_id": aid, "vdot": "51", "action": "apply", "team": "Boys"})
    db = SessionLocal()
    assert db.get(Athlete, aid).vdot_override == 51
    assert db.query(VdotChange).filter_by(athlete_id=aid, source="Suggested from workouts").count() == 1
    db.close()
    assert "Suggested VDOT changes" not in c.get("/vdot").text        # a fresh VDOT: four weeks before the next raise

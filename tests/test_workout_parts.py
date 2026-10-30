"""Workouts made of parts (sets x reps x distance at a pace), with each athlete's times from their VDOT and goal."""
import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Goal, PlannedWorkout, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam", vdot_override=60)
    db.add(a)
    db.add(Athlete(first_name="Pat", last_name="Reed", gender="Boys", grade=9, group="JV", link_token="t-pat"))
    db.flush()
    db.add(Goal(athlete_id=a.id, event="5K", season=A.season_for(A.team_today()), goal_s=16 * 60))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/workouts").text).group(1)


def test_parts_workout(app):
    c, csrf = login(app)
    page = c.get("/workouts").text
    assert 'id="add-part"' in page and "<option>Repetition</option>" in page
    day = (A.team_today() + dt.timedelta(days=1)).isoformat()
    form = {"csrf": csrf, "date": day, "title": "",
            "p_sets": ["1", "2", "1", "1"], "p_reps": ["1", "4", "1", "6"], "p_dist": ["3200", "400", "1600", ""],
            "p_zone": ["Easy", "Interval", "Threshold", "Repetition"], "p_rest": ["", "90 s jog", "", ""]}
    c.post("/workouts", data=form)
    page = c.get("/workouts").text
    assert "Saved 3,200 m Easy + 2 × 4 × 400 m Interval + 1,600 m Threshold." in page               # empty last row ignored
    assert "2 × 4 × 400 m Interval (90 s jog rest)" in page and "Paces by group" in page
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert w.rep_distance_m == 400 and json.loads(w.parts)[1] == {"sets": 2, "reps": 4, "dist": 400, "zone": "Interval", "rest": "90 s jog"}
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    assert round(A.target_pace(db, sam.id, w)) == round(A.pace_at(60, A.INTERVAL_PCT))                     # rep-matching target
    db.close()
    me = TestClient(app).get("/a/t-sam").text
    assert ">At VDOT 60</th>" in me and ">Goal VDOT " in me
    assert "<b>2 × 4 × 400 m</b> Interval" in me and "<b>1:21.0</b>" in me                       # 400 at VDOT 60 interval pace
    assert "5:26/mi" in me and "90 s jog rest" in me
    pat = TestClient(app).get("/a/t-pat").text
    assert "Your times appear once you have a VDOT" in pat
    c.post("/workouts", data={**form, "wid": str(w.id), "p_dist": ["3200", "50", "1600", ""]})
    assert "Check part 2: a distance runs from 100 to 20,000 meters." in c.get("/workouts").text
    c.post("/workouts", data={**form, "p_dist": ["", "", "", ""]})
    assert "Add at least one part" in c.get("/workouts").text


def test_older_workout_opens_as_one_part(app):
    c, csrf = login(app)
    db = SessionLocal()
    db.add(PlannedWorkout(date=A.team_today(), title="6 x 1K", rep_distance_m=1000, pace_event="5K", pace_factor=1.07, groups="All"))
    db.commit()
    wid = db.query(PlannedWorkout).one().id
    db.close()
    page = c.get(f"/workouts?edit={wid}").text
    assert 'name="p_dist" min="100" max="20000" step="1" value="1000"' in page and '<option value="m" selected>m</option>' in page and "<option selected>Threshold</option>" in page


def test_parts_by_time(app):
    c, csrf = login(app)
    day = (A.team_today() + dt.timedelta(days=1)).isoformat()
    form = {"csrf": csrf, "date": day, "title": "", "p_sets": ["1", "1", "1"], "p_reps": ["1", "4", "1"],
            "p_dist": ["14", "5", "1600"], "p_unit": ["min", "min", "m"], "p_zone": ["Easy", "Threshold", "Easy"],
            "p_rest": ["", "1 min easy", ""]}
    c.post("/workouts", data=form)
    page = c.get("/workouts").text
    assert "Saved 14 min Easy + 4 × 5 min Threshold + 1,600 m Easy." in page and "4 × 5 min Threshold (1 min easy rest)" in page
    assert "/mi</td>" in page                                                         # group preview shows pace for timed parts
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert json.loads(w.parts)[1] == {"sets": 1, "reps": 4, "mins": 5, "zone": "Threshold", "rest": "1 min easy"}
    assert w.rep_distance_m == 0                                                      # the main (threshold) part is timed
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    assert round(A.target_pace(db, sam.id, w)) == round(A.pace_at(60, A.THRESHOLD_PCT))
    db.close()
    me = TestClient(app).get("/a/t-sam").text
    tempo = A.pace_at(60, A.THRESHOLD_PCT)
    assert "<b>4 × 5 min</b> Threshold" in me and f"<b>{A.fmt_time(tempo)[:-2]}/mi</b>" in me and "about 1," in me
    assert A.minutes_text(1.5) == "1:30 min" and A.part_label({"sets": 1, "reps": 1, "mins": 14}) == "14 min"
    page = c.get(f"/workouts?edit={w.id}").text
    assert 'step="0.25" value="5"' in page and '<option value="min" selected>min</option>' in page
    c.post("/workouts", data={**form, "wid": str(w.id), "p_dist": ["14", "500", "1600"]})
    assert "Check part 2: a timed part runs from 0.25 to 180 minutes." in c.get("/workouts").text


def test_parts_in_miles(app):
    c, csrf = login(app)
    day = (A.team_today() + dt.timedelta(days=1)).isoformat()
    form = {"csrf": csrf, "date": day, "title": "", "p_sets": ["1", "1"], "p_reps": ["1", "3"],
            "p_dist": ["5", "1.5"], "p_unit": ["mi", "mi"], "p_zone": ["Moderate", "Threshold"], "p_rest": ["", "2 min jog"]}
    c.post("/workouts", data=form)
    page = c.get("/workouts").text
    assert "Saved 5 mi Moderate + 3 × 1.5 mi Threshold." in page
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert json.loads(w.parts)[1] == {"sets": 1, "reps": 3, "zone": "Threshold", "rest": "2 min jog", "dist": 2414, "mi": 1.5}
    assert w.rep_distance_m == 2414                                                   # matched to synced reps in meters
    db.close()
    me = TestClient(app).get("/a/t-sam").text
    tempo = A.pace_at(60, A.THRESHOLD_PCT)
    assert "<b>3 × 1.5 mi</b> Threshold" in me and A.fmt_time(tempo * 2414 / A.METERS_PER_MILE)[:-2] in me   # time per 1.5 mi rep
    page = c.get(f"/workouts?edit={w.id}").text
    assert 'step="0.01" value="1.5"' in page and '<option value="mi" selected>mi</option>' in page
    c.post("/workouts", data={**form, "wid": str(w.id), "p_dist": ["5", "30"]})
    assert "Check part 2: a distance in miles runs from 0.25 to 20 miles." in c.get("/workouts").text


def test_workout_leaves_the_athlete_page_once_run(app):
    from xcapp.models import Activity
    c, csrf = login(app)
    day = A.team_today()
    c.post("/workouts", data={"csrf": csrf, "date": day.isoformat(), "title": "Tempo day", "p_sets": "1", "p_reps": "4",
                              "p_dist": "1000", "p_unit": "m", "p_zone": "Threshold", "p_rest": ""})
    upcoming = '<div class="d">Today · Tempo day</div>'
    assert upcoming in TestClient(app).get("/a/t-sam").text
    db = SessionLocal()
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    w = db.query(PlannedWorkout).one()
    db.add(Activity(athlete_id=sam.id, source="garmin_file", external_id="run1", start_utc=dt.datetime(day.year, day.month, day.day, 23),
                    local_date=day, duration_s=2400, distance_m=8000, sport="running", planned_workout_id=w.id))
    db.commit()
    db.close()
    assert upcoming not in TestClient(app).get("/a/t-sam").text           # synced and linked: off Sam's upcoming list
    assert upcoming in TestClient(app).get("/a/t-pat").text               # Pat hasn't run it yet


def test_zones_and_older_names():
    assert [n for n, _ in A.PACE_ZONES] == ["Easy", "Moderate", "Threshold", "Interval", "Repetition"]
    assert [A.pace_at(60, p) for _, p in A.PACE_ZONES] == sorted((A.pace_at(60, p) for _, p in A.PACE_ZONES), reverse=True)  # slowest first
    old = PlannedWorkout(date=dt.date(2026, 10, 1), title="x", rep_distance_m=0, parts=json.dumps(
        [{"sets": 1, "reps": 1, "dist": 8000, "zone": "Long run"}, {"sets": 1, "reps": 4, "mins": 5, "zone": "Tempo"}]))
    assert [p["zone"] for p in old.part_list] == ["Easy", "Threshold"]                # saved before the rename
    assert A.workout_summary(old.part_list) == "8,000 m Easy + 4 × 5 min Threshold"


def test_team_notes_window_and_search(app):
    from xcapp.models import Activity
    c, csrf = login(app)
    today = A.team_today()

    def save(day, title, zone, note="", **team):
        c.post("/workouts", data={"csrf": csrf, "date": day.isoformat(), "title": title, "p_sets": "1", "p_reps": "4", "p_dist": "400",
                                  "p_unit": "m", "p_zone": zone, "p_rest": "60 s jog", "p_note": note,
                                  **{f"team_{g}": "on" for g in team}})
    save(today, "Girls hills", "Repetition", "On the grass loop", Girls=True)
    save(today, "Everyone", "Interval", Boys=True, Girls=True)                     # both ticked: everyone
    save(today - dt.timedelta(days=20), "Old threshold day", "Threshold")
    c.get("/workouts")                                                               # shows (and clears) the "Saved" messages
    db = SessionLocal()
    w = {x.title: x for x in db.query(PlannedWorkout).all()}
    assert (w["Girls hills"].gender, w["Everyone"].gender, w["Old threshold day"].gender) == ("Girls", "", "")
    assert w["Girls hills"].part_list[0]["note"] == "On the grass loop" and "note" not in w["Everyone"].part_list[0]
    db.close()

    sam = TestClient(app).get("/a/t-sam").text                                      # Sam is a boy
    assert "Girls hills" not in sam and "Today · Everyone" in sam

    page = c.get("/workouts").text
    assert "Workouts within 7 days of today" in page and "Girls hills" in page and "Old threshold day" not in page
    assert "Girls only" in page and "– On the grass loop" in page and 'href="/workouts/all">All workouts</a>' in page
    assert 'name="p_note"' in page and 'class="num nowrap p-points"' in page and 'id="ref-vdot"' in page
    every = c.get("/workouts/all").text
    assert "Old threshold day" in every and "Girls hills" in every
    found = c.get("/workouts/all?q=grass+loop").text
    assert "Girls hills" in found and "Everyone" not in found and "1 workout mentioning “grass loop”" in found
    assert "Old threshold day" in c.get("/workouts/all?q=threshold").text and "Girls hills" not in c.get("/workouts/all?q=threshold").text
    assert "No workouts mention “zzz”." in c.get("/workouts/all?q=zzz").text
    edit = c.get(f"/workouts?edit={w['Girls hills'].id}").text
    assert 'name="team_Girls" checked' in edit and 'name="team_Boys" checked' not in edit and 'value="On the grass loop"' in edit

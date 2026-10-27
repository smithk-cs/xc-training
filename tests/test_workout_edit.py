"""Coaches can edit a planned workout; runs already matched follow the change."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from fitgen import rep_workout
from xcapp.db import SessionLocal
from xcapp.fitfile import parse_fit
from xcapp.ingest import ingest
from xcapp.models import Activity, Athlete, PlannedWorkout, StaffUser
from xcapp.security import hash_password

D1, D2 = dt.date(2026, 10, 20), dt.date(2026, 10, 21)


def login(app, user):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c, csrf


def test_edit_workout():
    db = SessionLocal()
    for n, r in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=n, role=r, password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t")
    db.add(a)
    db.flush()
    pw = PlannedWorkout(date=D1, title="6 x 1K", rep_distance_m=1000, pace_event="5K", pace_factor=1.0)
    db.add(pw)
    db.flush()
    for day in (D1, D2):
        ingest(db, a, parse_fit(rep_workout(dt.datetime(day.year, day.month, day.day, 22, tzinfo=dt.timezone.utc),
                                            [(1000, 200, 180, 20)] * 3)), "upload")
    db.commit()
    wid, aid = pw.id, a.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c, _ = login(app, "coach1")
    page = c.get(f"/workouts?edit={wid}").text
    assert "Edit workout" in page and 'value="6 x 1K"' in page and f'name="wid" value="{wid}"' in page and "Save changes" in page
    assert f'href="/workouts?edit={wid}#plan">Edit</a>' in c.get("/workouts").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/workouts", data={"csrf": csrf, "wid": str(wid), "date": D2.isoformat(), "title": "5 x 1K tempo",
                              "rep_distance_m": "1000", "pace_event": "5K", "pace_factor": "1.07", "group_Varsity": "on"})
    assert "Updated 5 x 1K tempo." in c.get("/workouts").text
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert (w.date, w.title, w.pace_factor, w.groups) == (D2, "5 x 1K tempo", 1.07, "Varsity")
    matched = {x.local_date: x.planned_workout_id for x in db.query(Activity).filter_by(athlete_id=aid)}
    assert matched == {D1: None, D2: wid}                               # the match moved with the date
    db.close()
    page = c.get(f"/workouts?edit={wid}").text
    assert 'name="group_Varsity" checked' in page and 'value="1.070"' in page
    t, tcsrf = login(app, "trainer1")
    assert t.post("/workouts", data={"csrf": tcsrf, "wid": str(wid), "date": D1.isoformat(), "title": "x",
                                     "rep_distance_m": "400", "pace_event": "5K"}).status_code == 403

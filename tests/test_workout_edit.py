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
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t", ability_override="Gold")
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
    listing = c.get("/workouts").text
    assert f'class="row-link" data-href="/workouts?edit={wid}#plan"' in listing and ">Edit</a>" not in listing
    assert 'name="group_Varsity"' not in listing and 'name="group_Gold"' in listing      # ability groups only
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/workouts", data={"csrf": csrf, "wid": str(wid), "date": D2.isoformat(), "title": "5 x 1K tempo",
                              "rep_distance_m": "1000", "pace_event": "5K", "pace_factor": "1.07", "group_Gold": "on", "group_Varsity": "on"})
    assert "Updated 5 x 1K tempo." in c.get("/workouts").text
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert (w.date, w.title, w.pace_factor, w.groups) == (D2, "5 x 1K tempo", 1.07, "Gold")
    matched = {x.local_date: x.planned_workout_id for x in db.query(Activity).filter_by(athlete_id=aid)}
    assert matched == {D1: None, D2: wid}                               # the match moved with the date
    db.close()
    page = c.get(f"/workouts?edit={wid}").text
    assert 'name="group_Gold" checked' in page and 'value="1.070"' in page
    t, tcsrf = login(app, "trainer1")
    assert t.post("/workouts", data={"csrf": tcsrf, "wid": str(wid), "date": D1.isoformat(), "title": "x",
                                     "rep_distance_m": "400", "pace_event": "5K"}).status_code == 403


def test_workout_type_sets_the_pace_factor():
    from xcapp import analysis as A
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    from xcapp.web import create_app
    c, _ = login(create_app(), "coach1")
    page = c.get("/workouts").text
    assert '<option value="Threshold" >Threshold (× 1.07)</option>' in page and '<option value="Other" >Other…</option>' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    base = {"csrf": csrf, "date": D1.isoformat(), "rep_distance_m": "1600", "pace_event": "5K", "pace_factor": "1.00"}
    for kind, factor in (("Tempo", 1.10), ("Recovery", 1.35), ("Long", 1.25), ("Interval", 1.00)):
        c.post("/workouts", data={**base, "title": kind, "pace_type": kind})
    c.post("/workouts", data={**base, "title": "Custom", "pace_type": "Other", "pace_factor": "1.15"})
    db = SessionLocal()
    got = {w.title: w.pace_factor for w in db.query(PlannedWorkout)}
    db.close()
    assert got == {"Tempo": 1.10, "Recovery": 1.35, "Long": 1.25, "Interval": 1.00, "Custom": 1.15}
    assert [A.pace_type(f) for f in (1.07, 1.1, 1.15)] == ["Threshold", "Tempo", "Other"]
    page = c.get("/workouts").text
    assert "Tempo (5K × 1.10)" in page and "Other (5K × 1.15)" in page
    wid = re.search(r'data-href="/workouts\?edit=(\d+)#plan"', page).group(1)
    edit = c.get(f"/workouts?edit={wid}").text
    assert re.search(r'<option value="(\w+)" selected>', edit)


def test_workouts_in_miles():
    import datetime as dt
    import re
    from fastapi.testclient import TestClient
    from xcapp import analysis as A
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, PlannedWorkout, Result, StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach9", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Ivy", last_name="Stone", gender="Girls", grade=10, group="JV", link_token="t-ivy")
    db.add(a)
    db.flush()
    db.add(Result(athlete_id=a.id, date=A.team_today() - dt.timedelta(days=10), event="5K", time_s=20 * 60, kind="race", season="2026 XC"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach9", "password": "correct-horse-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/workouts").text).group(1)
    day = (A.team_today() + dt.timedelta(days=1)).isoformat()
    base = {"csrf": csrf, "date": day, "title": "Easy 5", "pace_event": "5K", "pace_type": "Recovery"}
    c.post("/workouts", data={**base, "rep_distance_m": "50", "distance_unit": "mi"})
    assert "Check the distance: 100 to 10,000 meters, or 0.25 to 30 miles." in c.get("/workouts").text
    c.post("/workouts", data={**base, "rep_distance_m": "5", "distance_unit": "mi"})
    page = c.get("/workouts").text
    assert '<td class="num nowrap">5 mi</td>' in page
    db = SessionLocal()
    w = db.query(PlannedWorkout).one()
    assert (round(w.rep_distance_m, 1), w.distance_unit, w.distance_value) == (8046.7, "mi", 5.0)
    db.close()
    edit = c.get(f"/workouts?edit={w.id}").text
    assert '<option value="mi" selected>miles</option>' in edit and 'value="5.0"' in edit
    me = TestClient(create_app()).get("/a/t-ivy").text
    per_mile = 20 * 60 / 5000 * 1609.344 * 1.35                                     # 5K pace x the recovery factor
    assert "5 mi · Recovery" in me and f'{A.fmt_time(per_mile)[:-2]}/mi</span>' in me
    assert f"about {A.fmt_time(per_mile * 5)[:-2]} for 5 mi" in me and "/400" not in me.split("Upcoming workouts")[1].split("</section>")[0]

"""Runs typed in by hand (manual_runs.py): checking what's typed, saving with a Manual tag, deleting, and the watch's
copy replacing the typed-in run. Names are made up."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import manual_runs as MR
from xcapp.db import SessionLocal
from xcapp.fitfile import ParsedActivity
from xcapp.ingest import ingest
from xcapp.models import Activity, Athlete, PainReport, StaffUser
from xcapp.security import hash_password

TODAY = dt.date(2026, 11, 18)


def test_build_checks_what_is_typed():
    r = MR.build("2026-11-17", "", "5", "40:00", "", today=TODAY)
    assert r.distance_m == pytest.approx(5 * A.METERS_PER_MILE) and r.duration_s == 2400 and r.external_id.startswith("manual:")
    assert A.local_time(r.start).time() == MR.DEFAULT_START and A.local_date(r.start) == dt.date(2026, 11, 17)
    assert MR.build("2026-11-17", "06:45", "3.1", "25", "151", today=TODAY).duration_s == 1500       # bare minutes
    assert A.local_time(MR.build("2026-11-17", "06:45", "3.1", "25", "", today=TODAY).start).hour == 6
    assert MR.parse_duration("1:05:00") == 3900
    for args, words in ((("2026-11-19", "", "5", "40:00"), "hasn’t happened"), (("2026-08-01", "", "5", "40:00"), "past 60 days"),
                        (("", "", "5", "40:00"), "Choose the day"), (("2026-11-17", "", "five", "40:00"), "as a number"),
                        (("2026-11-17", "", "50", "6:00:00"), "between 0.1 and 40"), (("2026-11-17", "", "5", "abc"), "should look like"),
                        (("2026-11-17", "", "5", "10:00"), "faster than 3 minutes"), (("2026-11-17", "", "1", "45:00"), "slower than 30"),
                        (("2026-11-17", "", "5", "40:00", "300"), "between 60 and 230")):
        with pytest.raises(ValueError, match=words):
            MR.build(*args, today=TODAY)


def test_watch_copy_replaces_the_typed_in_run(session):
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0")
    session.add(a)
    session.flush()
    typed, _ = ingest(session, a, MR.build("2026-11-17", "", "5", "40:00", "", today=TODAY), MR.SOURCE)
    typed.rpe, typed.feel, typed.athlete_note = 6, "Heavy", "windy"
    session.add(PainReport(athlete_id=a.id, date=dt.date(2026, 11, 17), body_area="Left shin", severity=3, activity_id=typed.id))
    other, _ = ingest(session, a, MR.build("2026-11-17", "07:00", "2", "16:00", "", today=TODAY), MR.SOURCE)   # a short second run
    assert typed.is_manual and typed.device == MR.DEVICE
    start = dt.datetime(2026, 11, 17, 23, 41, tzinfo=dt.timezone.utc)          # 3:41 PM in Sacramento: not within 90 s
    watch, outcome = ingest(session, a, ParsedActivity(external_id="g1", start=start, duration_s=2380, distance_m=8100), "upload")
    assert outcome == "created" and not watch.is_manual
    left = session.query(Activity).order_by(Activity.id).all()
    assert [x.id for x in left] == [other.id, watch.id]                                  # the 2-mile run stays
    assert (watch.rpe, watch.feel, watch.athlete_note) == (6, "Heavy", "windy")
    assert session.query(PainReport).one().activity_id == watch.id
    # A typed-in run at the same time as one that's already there isn't added twice
    _, outcome = ingest(session, a, ParsedActivity(external_id="manual:x", start=start, duration_s=2380, distance_m=8100), MR.SOURCE)
    assert outcome == "duplicate"


def _watch(session, a, when_utc, miles, ext="g1"):
    return ingest(session, a, ParsedActivity(external_id=ext, start=when_utc, duration_s=miles * 480,
                                             distance_m=miles * A.METERS_PER_MILE), "upload")[0]


def test_a_second_run_that_day_is_not_taken_for_the_typed_in_one(session):
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0")
    session.add(a)
    session.flush()
    ingest(session, a, MR.build("2026-11-17", "06:30", "4", "32:00", "", today=TODAY), MR.SOURCE)        # a morning run
    _watch(session, a, dt.datetime(2026, 11, 17, 23, 41, tzinfo=dt.timezone.utc), 4.3)                  # 3:41 PM, another run
    assert sum(x.distance_m for x in session.query(Activity).all()) / A.METERS_PER_MILE == pytest.approx(8.3)
    # The watch's copy of the morning run, 20 minutes off what the athlete remembered, does replace it
    _watch(session, a, dt.datetime(2026, 11, 17, 14, 50, tzinfo=dt.timezone.utc), 4.1, ext="g2")       # 6:50 AM
    left = session.query(Activity).all()
    assert len(left) == 2 and not any(x.is_manual for x in left)


def test_the_day_s_workout_moves_to_the_watch_run(session):
    from xcapp.models import PlannedWorkout
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t0")
    session.add(a)
    pw = PlannedWorkout(date=dt.date(2026, 11, 17), title="Tempo", rep_distance_m=8000, groups="All")
    session.add(pw)
    session.flush()
    typed, _ = ingest(session, a, MR.build("2026-11-17", "", "5", "40:00", "", today=TODAY), MR.SOURCE)
    assert typed.planned_workout_id == pw.id
    watch = _watch(session, a, dt.datetime(2026, 11, 17, 23, 41, tzinfo=dt.timezone.utc), 4.94)          # a little shorter
    assert session.query(Activity).one() is watch and watch.planned_workout_id == pw.id


def test_a_run_the_watch_already_sent_is_not_typed_in_again(client):
    db = SessionLocal()
    a = db.query(Athlete).one()
    day = A.team_today() - dt.timedelta(days=1)
    start = dt.datetime.combine(day, dt.time(7, 0), tzinfo=MR.settings().tz).astimezone(dt.timezone.utc).replace(tzinfo=None)
    db.add(Activity(athlete_id=a.id, source="upload", external_id="w1", start_utc=start, local_date=day,
                    duration_s=2400, distance_m=5 * A.METERS_PER_MILE))
    db.commit()
    db.close()
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/a/tok-ava").text).group(1)
    form = {"csrf": csrf, "date": day.isoformat(), "miles": "5.1", "duration": "41:00"}
    client.post("/a/tok-ava/runs/new", data=form)                                   # no start time: it's the watch's run
    page = client.get("/a/tok-ava").text
    assert "Your watch already sent a 5.0 mile run at 7:00 AM" in page and "add its start time" in page
    client.post("/a/tok-ava/runs/new", data={**form, "start": "07:20"})              # close to the watch's: still the same run
    assert "check its start time" in client.get("/a/tok-ava").text
    client.post("/a/tok-ava/runs/new", data={**form, "start": "16:00"})              # an afternoon run: a different one
    db = SessionLocal()
    assert db.query(Activity).count() == 2
    db.close()


def test_deleting_a_typed_in_run_gives_the_workout_to_another_run(client):
    from xcapp.models import PlannedWorkout
    db = SessionLocal()
    a = db.query(Athlete).one()
    day = A.team_today() - dt.timedelta(days=1)
    db.add(PlannedWorkout(date=day, title="Tempo", rep_distance_m=8000, groups="All"))
    db.commit()
    db.close()
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/a/tok-ava").text).group(1)
    for start, miles in (("06:30", "6"), ("16:00", "3")):
        client.post("/a/tok-ava/runs/new", data={"csrf": csrf, "date": day.isoformat(), "start": start, "miles": miles, "duration": "40:00"})
    db = SessionLocal()
    six = db.query(Activity).filter(Activity.distance_m > 5 * A.METERS_PER_MILE).one()
    three = db.query(Activity).filter(Activity.distance_m < 5 * A.METERS_PER_MILE).one()
    assert six.planned_workout_id and not three.planned_workout_id
    six_id, three_id = six.id, three.id
    db.close()
    client.post(f"/a/tok-ava/runs/{six_id}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.get(Activity, three_id).planned_workout_id is not None
    db.close()


@pytest.fixture
def client():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Reed", gender="Girls", grade=10, group="JV", link_token="tok-ava"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return TestClient(create_app(), follow_redirects=False)


def test_athlete_types_in_and_deletes_a_run(client):
    today = A.team_today()
    page = client.get("/a/tok-ava").text
    assert 'action="/a/tok-ava/runs/new"' in page and "Type in a run" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    bad = client.post("/a/tok-ava/runs/new", data={"csrf": csrf, "date": today.isoformat(), "miles": "4", "duration": "nope"})
    assert bad.headers["location"] == "/a/tok-ava#add-runs" and "should look like" in client.get("/a/tok-ava").text
    r = client.post("/a/tok-ava/runs/new", data={"csrf": csrf, "date": today.isoformat(), "miles": "4.5", "duration": "36:00"})
    db = SessionLocal()
    act = db.query(Activity).one()
    db.close()
    assert act.source == "manual" and r.headers["location"] == f"/a/tok-ava/runs/{act.id}#effort"
    run_page = client.get(f"/a/tok-ava/runs/{act.id}").text
    assert "✎ Manual" in run_page and "Added a 4.5 mile run" in run_page and "Delete this run" in run_page
    assert "✎ Manual" in client.get("/a/tok-ava").text and "✎ Manual" in client.get("/a/tok-ava/runs").text
    client.post(f"/a/tok-ava/runs/{act.id}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(Activity).count() == 0
    # A watch run can't be deleted this way
    a = db.query(Athlete).one()
    db.add(Activity(athlete_id=a.id, source="upload", external_id="w1", start_utc=dt.datetime(2026, 11, 1, 22), local_date=dt.date(2026, 11, 1),
                    duration_s=1800, distance_m=6000))
    db.commit()
    wid = db.query(Activity).one().id
    db.close()
    assert "Delete this run" not in client.get(f"/a/tok-ava/runs/{wid}").text
    assert client.post(f"/a/tok-ava/runs/{wid}/delete", data={"csrf": csrf}).status_code == 404


def test_coach_types_in_a_run(client):
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    db = SessionLocal()
    aid = db.query(Athlete).one().id
    db.close()
    page = client.get(f"/athletes/{aid}").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert f'action="/athletes/{aid}/runs/new"' in page
    client.post(f"/athletes/{aid}/runs/new", data={"csrf": csrf, "date": A.team_today().isoformat(), "start": "16:00",
                                                     "miles": "6", "duration": "48:00", "avg_hr": "150"})
    db = SessionLocal()
    act = db.query(Activity).one()
    db.close()
    assert act.is_manual and act.avg_hr == 150
    assert "✎ Manual" in client.get(f"/athletes/{aid}").text and "Typed in by hand" in client.get(f"/athletes/{aid}/runs/{act.id}").text
    client.post(f"/athletes/{aid}/runs/{act.id}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(Activity).count() == 0
    db.close()

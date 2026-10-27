"""Daily resting heart rate: from Garmin's daily summaries or typed in by the athlete, compared with the athlete's usual."""
import datetime as dt
import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_garmin import connect, http, mock  # noqa: F401  (fixtures)
from xcapp import analysis as A
from xcapp import datasets as DS
from xcapp import garmin as G
from xcapp.db import SessionLocal
from xcapp.models import Athlete, GarminBackfill, RestingHeartRate, StaffUser
from xcapp.security import hash_password

TODAY = dt.date(2026, 10, 27)


def fill(db, aid, usual=48, week=55, source="athlete"):
    """Four weeks at `usual`, then this week at `week`."""
    for k in range(34):
        d = TODAY - dt.timedelta(days=k)
        db.add(RestingHeartRate(athlete_id=aid, date=d, bpm=week if k < 7 else usual, source=source))


def test_summary_and_flags(session, athlete):
    assert A.resting_hr(session, athlete.id, TODAY) is None
    fill(session, athlete.id)
    session.flush()
    s = A.resting_hr(session, athlete.id, TODAY)
    assert (s["latest"].bpm, s["week"], s["usual"], s["up"], s["flag"], len(s["recent"])) == (55, 55.0, 48.0, 7.0, True, 14)
    flags = A.resting_hr_flags(session, TODAY)
    assert [(f["athlete"].id, f["up"]) for f in flags] == [(athlete.id, 7.0)]
    few = [RestingHeartRate(date=TODAY - dt.timedelta(days=k), bpm=50, source="athlete") for k in (0, 1)]
    assert A.resting_hr_summary(few, TODAY)["week"] is None          # fewer than 3 days this week: no average yet
    for r in session.scalars(select(RestingHeartRate)).all():
        r.bpm = 50
    session.flush()
    assert A.resting_hr_flags(session, TODAY) == []


def test_garmin_dailies_and_backfill(session, athlete, mock, http):
    connect(session, athlete, http)
    note = {"dailies": [
        {"userId": "garmin-user-1", "calendarDate": "2026-10-26", "restingHeartRateInBeatsPerMinute": 47, "steps": 9000},
        {"userId": "garmin-user-1", "calendarDate": "2026-10-27", "restingHeartRateInBeatsPerMinute": 49},
        {"userId": "garmin-user-1", "calendarDate": "2026-10-25"},                                       # no value
        {"userId": "garmin-user-1", "calendarDate": "2026-10-24", "restingHeartRateInBeatsPerMinute": 400}]}
    c = G.handle_notification(session, note, http)
    assert (c["resting_hr"], c["skipped"], c["errors"]) == (2, 2, 0)
    A.save_resting_hr(session, athlete.id, dt.date(2026, 10, 23), 52, "athlete")
    later = {"dailies": [{"userId": "garmin-user-1", "calendarDate": "2026-10-27", "restingHeartRateInBeatsPerMinute": 46},
                         {"userId": "garmin-user-1", "calendarDate": "2026-10-23", "restingHeartRateInBeatsPerMinute": 60}]}
    G.handle_notification(session, later, http)
    rows = {(r.date.day, r.bpm, r.source) for r in session.scalars(select(RestingHeartRate))}
    assert rows == {(26, 47, "garmin"), (27, 46, "garmin"), (23, 52, "athlete")}   # Garmin updates its own, never the athlete's

    kinds = lambda: {j.kind for j in session.scalars(select(GarminBackfill))}
    session.query(GarminBackfill).delete()
    G.queue_backfill(session, athlete.garmin, 30)
    assert kinds() == {"activities", "activityDetails"}                      # no health permission: no dailies
    athlete.garmin.permissions += ",HEALTH_EXPORT"
    session.query(GarminBackfill).delete()
    G.queue_backfill(session, athlete.garmin, 30)
    assert kinds() == {"activities", "activityDetails", "dailies"}


def test_pages_entry_export_and_never_to_ai():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    ava = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava")
    db.add(ava)
    db.flush()
    today = A.team_today()
    for k in range(1, 34):
        db.add(RestingHeartRate(athlete_id=ava.id, date=today - dt.timedelta(days=k), bpm=56 if k < 7 else 48, source="garmin"))
    db.commit()
    aid = ava.id
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)

    page = c.get("/a/tok-ava").text
    assert 'id="rhr-h">Resting heart rate' in page and "This morning’s resting heart rate" in page
    assert "beats higher than usual this week" in page and "daily resting heart rate" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/a/tok-ava/resting-hr", data={"csrf": csrf, "bpm": "300"})
    assert "between 30 and 120" in c.get("/a/tok-ava").text
    c.post("/a/tok-ava/resting-hr", data={"csrf": csrf, "bpm": "57"})
    page = c.get("/a/tok-ava").text
    assert "Saved: 57 beats per minute this morning." in page and "you entered it" in page
    assert "This morning’s resting heart rate" not in page                       # already entered today
    data = json.loads(c.get("/a/tok-ava/my-data.json").text)
    assert data["resting_heart_rate"][-1] == {"date": today.isoformat(), "bpm": 57, "source": "athlete"}

    db = SessionLocal()
    snap = DS.snapshot(db)
    assert "Resting" not in json.dumps(snap["watchlist"], default=str)            # the AI's watch list never has it
    db.close()

    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    home = c.get("/").text
    assert "Resting heart rate up" in home and f'<a href="/athletes/{aid}">Ava Doyle</a>' in home and "+8" in home
    detail = c.get(f"/athletes/{aid}").text
    assert "Resting heart rate 57 (" in detail and "athlete entered" in detail and "usual 48" in detail and "Up 8 bpm" in detail
    assert "Dailies" not in c.get("/privacy").text and "resting heart rate from Garmin’s daily summary" in c.get("/privacy").text

    c2 = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c2.get("/a/tok-ava").text).group(1)
    c2.post("/a/tok-ava/delete-garmin-data", data={"csrf": csrf, "confirm": "DELETE"})
    db = SessionLocal()
    assert [(r.bpm, r.source) for r in db.scalars(select(RestingHeartRate))] == [(57, "athlete")]   # Garmin's are gone
    db.close()

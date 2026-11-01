"""The team calendar: practices and events, plus meets, for boys, girls or everyone, by private link and phone feed."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import team_calendar as TC
from xcapp.db import SessionLocal
from xcapp.models import Athlete, CalendarEvent, MeetDivision, MeetEntry, StaffUser
from xcapp.security import hash_password

TODAY = A.team_today()
MON = A.week_monday(TODAY) + dt.timedelta(days=7)


def coach(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/calendar").text).group(1)


def test_calendar_end_to_end():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava"))
    m = MeetEntry(name="Section Finals", date=MON + dt.timedelta(days=5), location="Willow Hill", start_time="09:30")
    m.divisions.append(MeetDivision(name="Boys Varsity", gender="Boys", level="Varsity", distance="5K", position=0))
    db.add(m)
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c, csrf = coach(app)
    c.post("/calendar/events", data={"csrf": csrf, "title": "Practice", "kind": "Practice", "team": "", "date": MON.isoformat(),
                                     "start_time": "15:30", "end_time": "17:30", "location": "Jesuit track", "repeat": ["0", "2", "4"],
                                     "repeat_until": (MON + dt.timedelta(days=13)).isoformat()})
    c.post("/calendar/events", data={"csrf": csrf, "title": "Girls dinner", "kind": "Event", "team": "Girls",
                                     "date": (MON + dt.timedelta(days=3)).isoformat(), "start_time": "", "end_time": ""})
    c.post("/calendar/events", data={"csrf": csrf, "title": "Bad", "kind": "Event", "date": MON.isoformat(), "start_time": "3pm"})
    assert "Times look like 15:30." in c.get("/calendar").text
    db = SessionLocal()
    practice = db.query(CalendarEvent).filter_by(title="Practice").one()
    pid = practice.id
    week = TC.items(db, MON, MON + dt.timedelta(days=13), "All")
    assert [x["date"].weekday() for x in week if x["title"] == "Practice"] == [0, 2, 4, 0, 2, 4]
    boys = [x["title"] for x in TC.items(db, MON, MON + dt.timedelta(days=13), "Boys")]
    girls = [x["title"] for x in TC.items(db, MON, MON + dt.timedelta(days=13), "Girls")]
    assert "Section Finals" in boys and "Girls dinner" not in boys and "Girls dinner" in girls and "Section Finals" not in girls
    token = TC.token(db)
    db.close()
    c.post(f"/calendar/events/{pid}/skip", data={"csrf": csrf, "date": (MON + dt.timedelta(days=2)).isoformat()})   # cancel a Wednesday
    anon = TestClient(app)
    page = anon.get(f"/team-calendar/{token}?team=Boys").text
    assert "Section Finals" in page and "3:30 PM–5:30 PM" in page and "Girls dinner" not in page and f"webcal://" in page
    assert anon.get("/team-calendar/wrong-token").status_code == 404
    feed = anon.get(f"/team-calendar/{token}/all.ics")
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
    body = feed.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n") and "REFRESH-INTERVAL;VALUE=DURATION:PT1H" in body
    assert body.count("SUMMARY:Practice") == 5                                       # six days less the cancelled one
    assert "SUMMARY:Meet: Section Finals (Boys)" in body and "LOCATION:Willow Hill" in body
    assert f"DTSTART;VALUE=DATE:{MON + dt.timedelta(days=3):%Y%m%d}" in body          # the all-day dinner
    start = dt.datetime(MON.year, MON.month, MON.day, 15, 30, tzinfo=A.settings().tz).astimezone(dt.timezone.utc)
    assert f"DTSTART:{start:%Y%m%dT%H%M%SZ}" in body                                 # 3:30 p.m. Pacific, in UTC
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    assert "Ava" not in body and "Doyle" not in body                                  # nothing about athletes
    assert f"/team-calendar/{token}?team=Girls" in anon.get("/a/t-ava").text          # the athlete's own team calendar
    c.post("/calendar/new-link", data={"csrf": csrf})
    assert anon.get(f"/team-calendar/{token}/all.ics").status_code == 404             # old links stop working


def test_charts_offer_adjusted_times():
    from xcapp.models import Course, MeetConditions, RaceResult
    from xcapp.imports import sync_benchmark
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-s")
    db.add(a)
    db.add(Course(name="Gibson Ranch", surface="Firm grass", turns="Some", distance_m=5000))
    db.add(MeetConditions(meet="Gibson Invite", date=dt.date(2026, 10, 3), temp_f=75))
    db.flush()
    rr = RaceResult(athlete_id=a.id, date=dt.date(2026, 10, 3), meet="Gibson Invite", race="Boys Varsity", course="Gibson Ranch",
                    distance_m=5000, distance_label="5K", time_s=1000)
    db.add(rr)
    db.flush()
    sync_benchmark(db, rr)
    db.commit()
    db.close()
    from xcapp.web import create_app
    c, _ = coach(create_app())
    assert '<option value="adj">Adjusted for course and weather</option>' in c.get("/charts").text
    row = c.get("/api/charts/results.json").json()["results"][0]
    assert row["time_sec"] == 1000 and row["adjusted"] and row["time_adj_sec"] < 1000

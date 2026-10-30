"""Duplicate pain reports (same athlete, day and body area) are cleaned up: the most recent one is kept."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Athlete, PainReport


def test_keeps_the_most_recent_report(session):
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t1")
    b = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", link_token="t2")
    session.add_all([a, b])
    session.flush()
    day, t = dt.date(2026, 10, 20), dt.datetime(2026, 10, 20, 15)
    old = PainReport(athlete_id=a.id, date=day, body_area="Knee", severity=3, note="sore left side", saw_trainer=True, created_at=t)
    new = PainReport(athlete_id=a.id, date=day, body_area="Knee", severity=6, note="worse", created_at=t + dt.timedelta(hours=2))
    other_place = PainReport(athlete_id=a.id, date=day, body_area="Foot", severity=2, created_at=t)
    other_day = PainReport(athlete_id=a.id, date=day + dt.timedelta(days=1), body_area="Knee", severity=4, created_at=t)
    other_athlete = PainReport(athlete_id=b.id, date=day, body_area="Knee", severity=5, created_at=t)
    session.add_all([old, new, other_place, other_day, other_athlete])
    session.flush()
    assert A.dedupe_pain_reports(session) == 1
    left = session.query(PainReport).filter_by(athlete_id=a.id, date=day, body_area="Knee").all()
    assert left == [new] and new.severity == 6 and new.saw_trainer                 # newest kept, the trainer tick carried over
    assert new.note == "Earlier 3/10: sore left side · worse"                       # both notes kept, oldest first
    assert session.query(PainReport).count() == 4 and A.dedupe_pain_reports(session) == 0


def test_a_second_report_replaces_the_first():
    db = SessionLocal()
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    for sev in ("3", "7"):
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/t-ava").text).group(1)
        c.post("/a/t-ava/pain", data={"csrf": csrf, "body_area": "Knee", "severity": sev})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/t-ava").text).group(1)
    c.post("/a/t-ava/pain", data={"csrf": csrf, "body_area": "Foot", "severity": "2"})
    db = SessionLocal()
    got = sorted((p.body_area, p.severity) for p in db.query(PainReport).all())
    db.close()
    assert got == [("Foot", 2), ("Knee", 7)]

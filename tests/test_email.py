"""Email addresses for athletes and staff: set by hand or in a roster file, in the mail-merge CSV, never sent to the AI."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import ai
from xcapp import datasets as DS
from xcapp import imports as I
from xcapp.db import SessionLocal
from xcapp.models import Athlete, StaffUser
from xcapp.security import hash_password
import datetime as dt


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="admin1", role="admin", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam",
                   ai_consent_at=dt.datetime(2026, 10, 1)))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app, user):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c


def test_clean_email():
    assert I.clean_email("  Sam.Lowry@JesuitHighSchool.org ") == "sam.lowry@jesuithighschool.org" and I.clean_email("") == ""
    for bad in ("sam", "sam@", "sam@school", "a b@x.org", "x@y.org, z@y.org"):
        with pytest.raises(ValueError):
            I.clean_email(bad)


def test_athlete_and_staff_email(app):
    c = login(app, "coach1")
    db = SessionLocal()
    aid = db.query(Athlete).one().id
    db.close()
    page = c.get(f"/athletes/{aid}").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    base = {"csrf": csrf, "grade": "12", "group": "Varsity"}
    c.post(f"/athletes/{aid}/edit", data={**base, "email": "not-an-email"})
    assert "doesn&#39;t look like an email address" in c.get(f"/athletes/{aid}").text
    c.post(f"/athletes/{aid}/edit", data={**base, "email": "Sam@Example.org"})
    assert 'name="email" maxlength="254" value="sam@example.org"' in c.get(f"/athletes/{aid}").text
    assert "Sam Lowry,12,sam@example.org," in c.get("/athletes/links.csv").text
    assert json.loads(TestClient(app).get("/a/t-sam/my-data.json").text)["athlete"]["email"] == "sam@example.org"

    db = SessionLocal()
    I.import_roster(db, "name,grade,email\nSam Lowry,12,\n")                              # a blank email keeps the old one
    assert db.query(Athlete).one().email == "sam@example.org"
    I.import_roster(db, "name,grade,email\nSam Lowry,12,new@example.org\n")
    assert db.query(Athlete).one().email == "new@example.org"
    payload, _ = ai.build_payload(DS.snapshot(db))
    assert "example.org" not in json.dumps(payload)                                         # never sent to the AI
    db.rollback()
    db.close()

    adm = login(app, "admin1")
    staff = adm.get("/staff").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', staff).group(1)
    db = SessionLocal()
    cid = db.query(StaffUser).filter_by(username="coach1").one().id
    db.close()
    adm.post(f"/staff/{cid}/email", data={"csrf": csrf, "email": "coach@example.org"})
    assert 'value="coach@example.org" aria-label="Email for coach1"' in adm.get("/staff").text
    adm.post("/staff", data={"csrf": csrf, "username": "trainer2", "role": "trainer", "email": "at@example.org", "password": "correct-horse-2"})
    db = SessionLocal()
    assert db.query(StaffUser).filter_by(username="trainer2").one().email == "at@example.org"
    db.close()
    ccsrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/").text).group(1)
    assert c.post(f"/staff/{cid - 1}/email", data={"csrf": ccsrf, "email": "x@example.org"}).status_code == 403   # not their own

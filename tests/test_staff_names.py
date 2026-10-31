"""Staff names are first and last name; the full name (on entry sheets and PDFs) is built from them."""
import re

from fastapi.testclient import TestClient

from xcapp import db as D
from xcapp.db import SessionLocal
from xcapp.models import StaffUser
from xcapp.security import hash_password


def test_older_full_names_are_split_and_the_form_saves_both():
    db = SessionLocal()
    db.add(StaffUser(username="admin1", role="admin", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="coach1", role="coach", full_name="Mary Ann Smith", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="coach2", role="coach", full_name="Cher", password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    D.create_all()   # as on the next start after the upgrade
    db = SessionLocal()
    mary, cher = db.query(StaffUser).filter_by(username="coach1").one(), db.query(StaffUser).filter_by(username="coach2").one()
    assert (mary.first_name, mary.last_name) == ("Mary Ann", "Smith") and (cher.first_name, cher.last_name) == ("Cher", "")
    mid = mary.id
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "admin1", "password": "correct-horse-1"})
    page = c.get("/staff").text
    assert 'name="first_name" maxlength="60" value="Mary Ann"' in page and 'name="last_name" maxlength="60" value="Smith"' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post(f"/staff/{mid}/details", data={"csrf": csrf, "first_name": " Maryann ", "last_name": "Smith-Lee", "title": "Head Coach", "on_entries": "1"})
    c.post("/staff", data={"csrf": csrf, "username": "coach3", "first_name": "Dale", "last_name": "Brook", "role": "coach",
                           "password": "correct-horse-2"})
    db = SessionLocal()
    assert db.get(StaffUser, mid).full_name == "Maryann Smith-Lee"
    assert db.query(StaffUser).filter_by(username="coach3").one().full_name == "Dale Brook"
    db.close()

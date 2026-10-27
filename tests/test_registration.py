"""Meet entries: pick athletes for each race, then email the entries or download a PDF. Names are made up."""
import datetime as dt
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from xcapp.db import SessionLocal
from xcapp.models import Athlete, DivisionAthlete, MeetEntry, RaceResult, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", email="coach@example.org", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", email="at@example.org", password_hash=hash_password("correct-horse-1")))
    people = [("Sam", "Lowry", 12, "sam@example.org", 61), ("José", "O'Neil", 11, "", 66), ("Theo", "Marsh", 10, "theo@example.org", None),
              ("Pat", "Reed", 9, "", None)]
    for i, (f, l, g, e, v) in enumerate(people):
        db.add(Athlete(first_name=f, last_name=l, gender="Boys", grade=g, group="Varsity", link_token=f"t{i}", email=e, vdot_override=v))
    db.flush()
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    db.add(RaceResult(athlete_id=sam.id, date=dt.date(2026, 10, 10), meet="Hilltop", race="Boys Varsity", distance_m=5000,
                      distance_label="5,000 Meters", time_s=15 * 60 + 41.2, place=3))
    db.add(RaceResult(athlete_id=sam.id, date=dt.date(2025, 10, 10), meet="Old", race="Boys Varsity", distance_m=5000,
                      distance_label="5K", time_s=15 * 60, place=1))                    # last season: not a season best
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app, user="coach1"):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c


def ids():
    db = SessionLocal()
    out = {a.first_name: a.id for a in db.query(Athlete)}
    db.close()
    return out


def test_entries_email_and_pdf(app):
    c = login(app)
    assert '<a href="/registrations"' in c.get("/").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/registrations").text).group(1)
    r = c.post("/registrations", data={"csrf": csrf, "name": "Clovis Invitational", "date": "2026-10-31", "location": "Woodward Park",
                                       "director_name": "Dana Ruiz", "director_email": "Dana@Example.org", "starter": "1"})
    mid = int(r.headers["location"].rsplit("/", 1)[1])
    page = c.get(f"/registrations/{mid}").text
    assert "Boys Varsity · 5K" in page and "Boys JV · 5K" in page and "15:41.2" in page        # Sam's season best in the picker
    db = SessionLocal()
    m = db.get(MeetEntry, mid)
    varsity, jv = m.divisions[0].id, m.divisions[1].id
    db.close()
    i = ids()
    c.post(f"/registrations/{mid}/divisions/{varsity}", data={"csrf": csrf, "name": "Boys Varsity", "distance": "5K",
                                                              "athlete": [str(i["José"]), str(i["Sam"])]})
    c.post(f"/registrations/{mid}/divisions/{jv}", data={"csrf": csrf, "name": "Boys JV", "distance": "5K", "athlete": [str(i["Theo"])]})
    c.post(f"/registrations/{mid}/divisions", data={"csrf": csrf, "name": "Boys Frosh", "distance": "3 Miles"})
    page = c.get(f"/registrations/{mid}").text
    assert "Saved Boys JV: 1 athlete." in page
    assert re.search(r"José O&#39;Neil <span class=\"small muted\">grade 11</span>.*Sam Lowry <span class=\"small muted\">grade 12 · season best 15:41.2", page, re.S)
    # Moving Sam to JV takes him out of Varsity.
    c.post(f"/registrations/{mid}/divisions/{jv}", data={"csrf": csrf, "name": "Boys JV", "distance": "5K",
                                                         "athlete": [str(i["Theo"]), str(i["Sam"])]})
    page = c.get(f"/registrations/{mid}").text
    assert "Moved from another race: Sam Lowry." in page
    db = SessionLocal()
    assert sorted((e.division.name, e.athlete.first_name) for e in db.query(DivisionAthlete)) == [
        ("Boys JV", "Sam"), ("Boys JV", "Theo"), ("Boys Varsity", "José")]
    db.close()

    director = unquote(re.search(r'href="(mailto:dana[^"]+)"', page).group(1))
    assert director.startswith("mailto:dana@example.org?subject=") and "Boys JV · 5K (2 athletes)" in director
    assert "2. Sam Lowry, grade 12, season best 15:41.2" in director and "coach1 (coach@example.org)" in director
    athletes = unquote(re.search(r'href="(mailto:\?bcc=[^"]+)"', page).group(1))
    assert athletes.startswith("mailto:?bcc=sam@example.org,theo@example.org&")
    assert "No email address yet for: José O&#39;Neil" in page
    assert "mailto:?bcc=at@example.org,coach@example.org&" in unquote(page)

    pdf = c.get(f"/registrations/{mid}/entries.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["content-disposition"].startswith("inline")
    assert c.get(f"/registrations/{mid}/entries.pdf?download=1").headers["content-disposition"] == \
        'attachment; filename="Clovis-Invitational-entries-2026-10-31.pdf"'
    from pypdf import PdfReader  # noqa: E402  (only for reading the test PDF)
    import io
    text = PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
    for want in ("Clovis Invitational", "Saturday, October 31, 2026", "Woodward Park", "Boys JV", "Sam Lowry", "15:41.2", "José O’Neil".replace("’", "'"),
                 "3 athletes in 3 races", "Dana Ruiz"):
        assert want in text, want

    t = login(app, "trainer1")
    assert "Choose athletes" not in t.get(f"/registrations/{mid}").text and t.get(f"/registrations/{mid}/entries.pdf").status_code == 200
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/registrations").text).group(1)
    assert t.post(f"/registrations/{mid}/delete", data={"csrf": tcsrf}).status_code == 403
    c.post(f"/registrations/{mid}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(MeetEntry).count() == 0 and db.query(DivisionAthlete).count() == 0 and db.query(Athlete).count() == 4
    db.close()

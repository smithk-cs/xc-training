"""Reports: the official roster (sort, filter, columns, PDF) and one athlete's season overview. Names are made up."""
import datetime as dt
import io
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import reports as RP
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, RaceResult, StaffUser
from xcapp.security import hash_password

SEASON = A.season_for(A.team_today())


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", full_name="Dale Brook", title="Head Coach", password_hash=hash_password("correct-horse-1")))
    start, _ = RP.season_dates(SEASON)
    people = [("Sam", "Lowry", 12, [(5000, 15 * 60 + 50, 8, 8.0), (4000, 12 * 60 + 30, 5, 5.0), (5000, 15 * 60 + 20, 3, 3.0)]),
              ("Theo", "Marsh", 10, [(5000, 17 * 60 + 5, 40, 30.0)]),
              ("Pat", "Reed", 9, [])]
    for i, (f, l, g, races) in enumerate(people):
        a = Athlete(first_name=f, last_name=l, gender="Boys", grade=g, group="Varsity", link_token=f"t{i}")
        db.add(a)
        db.flush()
        for j, (m, t, place, pts) in enumerate(races):
            db.add(RaceResult(athlete_id=a.id, date=start + dt.timedelta(days=40 + 14 * j), meet=f"Meet {j + 1}", race="Boys Varsity",
                              distance_m=m, distance_label="5K" if m == 5000 else "4K", time_s=t, place=place, points=pts))
        if f == "Sam":
            for k in range(20):
                d = start + dt.timedelta(days=35 + k * 2)
                db.add(Activity(athlete_id=a.id, source="upload", external_id=f"r{k}", start_utc=dt.datetime(d.year, d.month, d.day, 15),
                                local_date=d, duration_s=2700, distance_m=11000, sport="running", rpe=5))
    db.add(RaceResult(athlete_id=db.query(Athlete).filter_by(first_name="Sam").one().id, date=start - dt.timedelta(days=200), meet="Old",
                      race="Boys", distance_m=5000, distance_label="5K", time_s=15 * 60 + 10))   # an older, faster PR
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c


def names(page):
    return re.findall(r'<td data-sort="[^"]*"><a href="/athletes/\d+">([^<]+)</a>', page)


def test_roster_report(app):
    c = login(app)
    assert '<a href="/reports"' in c.get("/").text and "Official roster" in c.get("/reports").text
    page = c.get(f"/reports/roster?season={SEASON}").text
    assert names(page) == ["Sam Lowry", "Theo Marsh", "Pat Reed"]                       # by last name
    assert ">Season best</th>" in page and "15:20.0" in page and "15:10.0" in page        # season best and older PR
    assert "Coaching staff" in page and "Dale Brook" in page
    page = c.get(f"/reports/roster?season={SEASON}&sort=grade").text
    assert names(page) == ["Pat Reed", "Theo Marsh", "Sam Lowry"]
    page = c.get(f"/reports/roster?season={SEASON}&sort=sb").text
    assert names(page) == ["Sam Lowry", "Theo Marsh", "Pat Reed"]                        # no time goes last
    page = c.get(f"/reports/roster?season={SEASON}&sort=avg_points&desc=1").text
    assert names(page) == ["Theo Marsh", "Sam Lowry", "Pat Reed"] and "sorted by avg points (high to low)" in page
    page = c.get(f"/reports/roster?season={SEASON}&grade=12&cols=grade&cols=races&title=Varsity+seniors").text
    assert names(page) == ["Sam Lowry"] and ">Races</th>" in page and ">VDOT</th>" not in page and "Varsity seniors" in page
    pdf = c.get(f"/reports/roster.pdf?season={SEASON}&title=Team+roster")
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
    for want in ("Team roster", "Official roster", "Sam Lowry", "15:20.0", "Coaching staff", "Dale Brook", "AVG PTS"):
        assert want in text, want


def test_season_overview(app):
    c = login(app)
    db = SessionLocal()
    sam = db.query(Athlete).filter_by(first_name="Sam").one().id
    pat = db.query(Athlete).filter_by(first_name="Pat").one().id
    db.close()
    assert c.get(f"/reports/athlete?athlete_id={sam}&season={SEASON.replace(' ', '+')}").headers["location"].startswith(f"/reports/athlete/{sam}")
    page = c.get(f"/reports/athlete/{sam}?season={SEASON}").text
    assert "season overview" in page and "<h1>Sam Lowry</h1>" in page
    assert '<span class="k">Races</span><span class="v">3</span>' in page
    assert "faster, first to latest race" in page and "Race progression" in page and page.count("<svg") >= 3
    assert "Meet 3" in page and '<span class="pill good">PR</span>' in page and "Weekly mileage" in page
    assert re.search(r'<span class="k">Miles</span><span class="v">(\d+(\.\d)?)</span>', page).group(1) == "136.7"
    empty = c.get(f"/reports/athlete/{pat}?season={SEASON}").text
    assert "No races recorded this season." in empty and "needs two races" in empty


def test_roster_pdf_centered_except_names():
    import io
    import types

    import pdfplumber

    from xcapp import reports as RP
    coach = types.SimpleNamespace(display_name="Keegan Smith", title="Head coach", phone="916-555-0100", email="coach@example.org")
    data = RP.roster_pdf([{"name": "Ava Doyle", "grade": 11, "group": "Gold", "vdot": 60}], ["grade", "group", "vdot"],
                         "Team roster", "2026 XC", "XC Training", None, [coach])
    page = pdfplumber.open(io.BytesIO(data)).pages[0]
    words = {w["text"]: w for w in page.extract_words()}
    mid = lambda w: (w["x0"] + w["x1"]) / 2   # noqa: E731
    centre = page.width / 2
    for text in ("roster", "TRAINING"):                                        # title and header band
        assert abs(mid(words[text]) - centre) < 60
    assert words["Ava"]["x0"] < 100 and words["Keegan"]["x0"] < 60             # names stay left
    assert words["coach"]["x0"] > 150 and words["Gold"]["x0"] > 250            # the rest is centered in its column

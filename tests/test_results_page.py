"""The Goals page: the 10 latest results, filterable, with every result on the All results page."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp.db import SessionLocal
from xcapp.models import Athlete, Result, StaffUser
from xcapp.security import hash_password


def test_latest_and_all_results():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    ava = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava")
    sam = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam")
    db.add_all([ava, sam])
    db.flush()
    for i in range(12):   # Ava: 12 5Ks in 2026 XC; Sam: 3 track miles in 2025
        db.add(Result(athlete_id=ava.id, event="5K", time_s=1100 + i, date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
                      season="2026 XC", kind="race"))
    for i in range(3):
        db.add(Result(athlete_id=sam.id, event="1 mile", time_s=280 + i, date=dt.date(2025, 4, 1) + dt.timedelta(days=i),
                      season="2025 Track", kind="track"))
    db.commit()
    ids = (ava.id, sam.id)
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})

    page = c.get("/results").text
    assert "<h1>Goals</h1>" in page and ">Goals</a>" in page and "Add a result" not in page and "Set a goal" in page
    latest = page[page.index("Latest results"):]
    assert latest.count("<td>Ava Doyle</td>") == 10 and "<td>Sam Lowry</td>" not in latest      # the 10 newest only
    assert "The 10 most recent." in latest and 'href="/results/all">All results ›</a>' in latest

    boys = c.get("/results?gender=Boys").text
    assert boys.count("<td>Sam Lowry</td>") == 3 and "<td>Ava Doyle</td>" not in boys[boys.index("Latest results"):]
    assert 'href="/results/all?gender=Boys">All results (filtered) ›</a>' in boys and "<option selected>Boys</option>" in boys
    assert c.get("/results?season=2025+Track&kind=track").text.count("<td>Sam Lowry</td>") == 3
    assert "No results match these filters." in c.get(f"/results?athlete={ids[1]}&event=5K").text

    every = c.get("/results/all").text
    assert "All results" in every and every.count("<td>Ava Doyle</td>") == 12 and every.count("<td>Sam Lowry</td>") == 3
    assert "15 results." in every
    girls = c.get(f"/results/all?athlete={ids[0]}").text
    assert girls.count("<td>Ava Doyle</td>") == 12 and "<td>Sam Lowry</td>" not in girls

    db = SessionLocal()
    rid = db.query(Result).filter_by(athlete_id=ids[1]).first().id
    db.close()
    csrf = re.search(r'name="csrf" value="([^"]+)"', every).group(1)
    r = c.post(f"/results/{rid}/delete", data={"csrf": csrf, "next": "/results/all?gender=Boys"})
    assert r.headers["location"] == "/results/all?gender=Boys"                                  # back to the same list
    assert c.post(f"/results/{rid}/delete", data={"csrf": csrf, "next": "https://evil.example"}).headers["location"] == "/results"

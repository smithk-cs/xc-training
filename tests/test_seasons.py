import datetime as dt
import re

from xcapp import analysis as A
from xcapp import seasons as SE


def test_usual_dates_cover_the_year_without_gaps():
    parts = SE.year_periods(2026)
    assert [(p["name"], p["start"], p["end"]) for p in parts] == [
        ("Summer Base", dt.date(2026, 6, 7), dt.date(2026, 8, 2)),
        ("Cross Country", dt.date(2026, 8, 3), dt.date(2026, 12, 5)),
        ("Winter Base", dt.date(2026, 12, 6), dt.date(2027, 2, 7)),
        ("Track & Field", dt.date(2027, 2, 8), dt.date(2027, 6, 5))]
    assert SE.year_periods(2027)[0]["start"] == dt.date(2027, 6, 6)
    assert SE.period_for(dt.date(2026, 11, 1))["label"] == "2026 Cross Country"
    assert SE.period_for(dt.date(2027, 1, 4))["label"] == "2026 Winter Base"
    assert SE.school_year_of(dt.date(2026, 7, 1)) == 2026 and SE.school_year_of(dt.date(2026, 5, 1)) == 2025
    assert A.season_for(dt.date(2026, 12, 5)) == "2026 XC" and A.season_for(dt.date(2026, 12, 6)) == "2027 Track"


def test_coach_sets_seasons_and_results_follow(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp import config
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, Result, StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    monkeypatch.setenv("AS_OF_DATE", "2026-11-01")
    config.reload_settings()
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Boys", grade=11, group="Varsity", link_token="t-se")
    db.add(a)
    db.flush()
    db.add(Result(athlete_id=a.id, event="5000", time_s=1000, date=dt.date(2026, 12, 12), season="2027 Track"))
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/calendar").text
    assert "Now: <b>2026 Cross Country</b>" in page and "2026–27" in page and "2027–28" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    form = {"csrf": csrf, "school_year": "2026", "name_summer": "Summer Base", "start_summer": "2026-06-08", "end_summer": "2026-08-02",
            "name_xc": "Cross Country", "start_xc": "2026-08-03", "end_xc": "2026-12-12",          # Nike Cross Nationals week
            "name_winter": "Winter Training", "start_winter": "2026-12-13", "end_winter": "2027-02-14",
            "name_track": "Track & Field", "start_track": "2027-02-15", "end_track": "2027-06-05"}
    c.post("/calendar/seasons", data=form)
    page = c.get("/calendar").text
    assert "1 result moved season." in page and 'value="Winter Training"' in page
    db = SessionLocal()
    assert db.query(Result).one().season == "2026 XC"
    db.close()
    assert SE.period_for(dt.date(2026, 12, 20))["label"] == "2026 Winter Training"
    assert '2026 Cross Country</a>' in c.get("/week").text
    bad = dict(form, start_winter="2026-12-01")
    c.post("/calendar/seasons", data=bad)
    assert "Winter Training starts before Cross Country ends." in c.get("/calendar").text
    db = SessionLocal()
    assert db.query(Result).one().season == "2026 XC"     # nothing saved
    db.close()

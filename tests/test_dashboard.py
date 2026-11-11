"""The dashboard: athletes sorted into 90%+, 80-89% and below 80% on mileage, points, days running and the total score."""
import datetime as dt
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from xcapp import dashboard as DB


def row(first, miles_pct=None, points_pct=None, days_pct=None, score_pct=None, goal=30.0, target=200.0, no_score=""):
    return {"a": SimpleNamespace(id=1, first_name=first, last_name="X", name=f"{first} X"), "group_name": "Varsity",
            "miles": 20.0, "miles_due": 22.0, "goal_miles": goal, "miles_pct": miles_pct,
            "points": 150.0, "points_due": 160.0, "target": target, "points_pct": points_pct,
            "run_days": 4, "days_due": 5, "target_days": 6, "days_pct": days_pct, "score_pct": score_pct, "no_score": no_score}


def names(b, tab, band):
    return [e["a"].first_name for e in b[tab]["bands"][band]]


def test_bands_by_the_number_shown():
    rows = [row("Ann", miles_pct=89.6, points_pct=79.4, days_pct=100, score_pct=120),
            row("Ben", miles_pct=85, points_pct=80, days_pct=80, score_pct=89.4),
            row("Cal", miles_pct=40, points_pct=None, days_pct=50, score_pct=90),
            row("Dee", goal=0, target=0, no_score="No VDOT yet (needs a race)", days_pct=None)]
    b = DB.board(rows)
    assert names(b, "miles", "top") == ["Ann"] and names(b, "miles", "mid") == ["Ben"] and names(b, "miles", "low") == ["Cal"]
    assert b["miles"]["bands"]["top"][0]["pct"] == 90
    assert names(b, "points", "mid") == ["Ben"] and names(b, "points", "low") == ["Ann"]
    assert [e["a"].first_name for e in b["points"]["none"]] == ["Cal", "Dee"]           # nothing due yet / no target
    assert b["points"]["none"][1]["detail"] == "No VDOT yet (needs a race)"
    assert b["miles"]["none"][0]["detail"].startswith("No weekly miles goal")
    assert names(b, "days", "top") == ["Ann"] and names(b, "days", "mid") == ["Ben"] and names(b, "days", "low") == ["Cal"]
    assert names(b, "score", "top") == ["Ann", "Cal"] and names(b, "score", "mid") == ["Ben"]   # highest first
    assert "4 of 5 run days" in b["days"]["bands"]["top"][0]["detail"]


def test_dashboard_page(monkeypatch):
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="tok-sam"))
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/dashboard").text
    for label in ("Mileage", "Points", "Days running", "Total score"):
        assert f">{label}</button>" in page
    for band in ("90% or more", "80–89%", "Below 80%"):
        assert page.count(f"<h2>{band}</h2>") == 4                       # one collapsible list per band on each tab
    assert page.count("<details") >= 12 and "Sam Reyes" in page
    assert "Team briefing" not in page                                  # the old tabs are gone
    last = c.get("/dashboard?start=" + (dt.date.today() - dt.timedelta(days=7)).isoformat()).text
    assert "This week</a>" in last
    # Same week arrows as the Week page: back from this week, forward only from an earlier one
    assert 'aria-label="Week before"' in page and 'aria-label="Week after"' not in page and "This week</a>" not in page
    assert 'aria-label="Week before"' in last and 'aria-label="Week after"' in last
    assert c.get("/dashboard?start=nonsense").status_code == 200

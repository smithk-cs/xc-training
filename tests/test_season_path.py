"""Season path: each week's plan against what happened (mileage, points, intensity, quality posted) and the form
checkpoints for each phase; and the school year at a glance."""
import datetime as dt
import json
import re

from fastapi.testclient import TestClient

from xcapp import season_path as SPATH
from xcapp.models import (Activity, Athlete, FormAnalysis, PlannedWorkout, SeasonWeek, StaffUser, WeekResult)
from xcapp.security import hash_password

SEASON = {"school_year": 2026, "kind": "xc", "label": "2026 Cross Country", "start": dt.date(2026, 8, 3), "end": dt.date(2026, 8, 30)}
TODAY = dt.date(2026, 8, 26)          # weeks of Aug 3, 10, 17 finished; Aug 24 is this week
W = [dt.date(2026, 8, 3), dt.date(2026, 8, 10), dt.date(2026, 8, 17), dt.date(2026, 8, 24)]


def build(db):
    a = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="", link_token="t-sam")
    b = Athlete(first_name="Ana", last_name="Lee", gender="Boys", grade=10, group="", link_token="t-ana")
    db.add_all([a, b])
    db.flush()
    for m, ph, pct in zip(W, (1, 2, 3, 3), (80, 90, 100, 100)):
        db.add(SeasonWeek(monday=m, phase=ph, miles_pct=pct))
    # Interval reps posted in the Phase I week (off phase); repetition in Phase II; nothing in Phase III week 1
    db.add(PlannedWorkout(date=W[0] + dt.timedelta(days=1), title="Too soon", rep_distance_m=1000, pace_event="5K",
                          parts=json.dumps([{"sets": 1, "reps": 5, "dist": 1000, "zone": "Interval", "rest": ""},
                                            {"sets": 1, "reps": 6, "dist": 100, "zone": "Repetition", "rest": "", "note": "Strides", "strides": True}])))
    db.add(PlannedWorkout(date=W[1] + dt.timedelta(days=1), title="200s", rep_distance_m=200, pace_event="5K",
                          parts=json.dumps([{"sets": 1, "reps": 12, "dist": 200, "zone": "Repetition", "rest": ""}])))
    # Saved weeks (as medals.ensure_weeks keeps them), worked out today so they aren't redone
    for m, (mi, pts) in zip(W[:3], ((30, 40), (27, 36), (36, 50))):
        for ath in (a, b):
            db.add(WeekResult(athlete_id=ath.id, monday=m, score=90, miles=mi, goal_miles=30, points=pts, target=40,
                              run_days=6, medal="", computed_on=TODAY))
    # Running time per zone: Phase I week mostly easy; Phase II week 30% fast (too much); Phase III week 15%
    for m, fast in zip(W[:3], (0.02, 0.30, 0.15)):
        for ath in (a, b):
            db.add(Activity(athlete_id=ath.id, source="upload", external_id=f"r{ath.id}-{m}", start_utc=dt.datetime.combine(m, dt.time(15)),
                            local_date=m, duration_s=3600, distance_m=12000, sport="running", zone_seconds=json.dumps(
                                {"Easy": 3600 * (1 - fast), "Interval": 3600 * fast, "walk": 300})))
    # Form: Sam filmed fresh in Phase I; Ana tired in Phase III
    db.add(FormAnalysis(athlete_id=a.id, created_at=dt.datetime(2026, 8, 4, 18), data="{}", condition="fresh"))
    db.add(FormAnalysis(athlete_id=b.id, created_at=dt.datetime(2026, 8, 18, 18), data="{}", condition="tired"))
    db.flush()


def test_path(session):
    build(session)
    p = SPATH.path(session, SEASON, TODAY)
    w = {x["monday"]: x for x in p["weeks"]}
    assert [x["phase"] for x in p["weeks"]] == [1, 2, 3, 3] and p["team"] == 2
    assert round(w[W[0]]["miles_pct"]) == 100 and round(w[W[1]]["miles_pct"]) == 90 and round(w[W[2]]["points_pct"]) == 125
    assert w[W[3]]["miles_pct"] is None and w[W[3]]["now"]                       # this week: not finished yet
    assert w[W[0]]["off"] == ["Interval"] and w[W[0]]["posted"] == {"Interval": 1}   # strides don't count
    assert w[W[1]]["off"] == [] and w[W[1]]["main_ok"] is True and w[W[2]]["main_ok"] is False
    assert round(w[W[0]]["quality_pct"]) == 2 and w[W[0]]["intensity"] == "ok"
    assert w[W[1]]["intensity"] == "high" and w[W[2]]["intensity"] == "ok"      # 30% in Phase II; 15% in Phase III
    blocks = {b["phase"]: b for b in p["blocks"]}
    assert blocks[1]["filmed"] == 1 and blocks[3]["tired"] == 1 and blocks[3]["weeks"] == 2
    assert blocks[3]["start"] == W[2] and blocks[3]["end"] == W[3] + dt.timedelta(days=6)
    assert SPATH.year(session, 2026)[1]["weeks"][:4] == [(W[0], 1), (W[1], 2), (W[2], 3), (W[3], 3)]


def test_page(session):
    build(session)
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/workouts/plan?season=2026-xc").text
    assert "School year at a glance" in page and "Season path: plan against what happened" in page
    assert "Form and technique by phase" in page and "Film every athlete fresh (the baseline)" in page
    assert "isn’t a Phase I pace" in page and 'class="ph ph3' in page

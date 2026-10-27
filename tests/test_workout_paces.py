"""Athletes see target paces for the coaches' planned workouts, from their current best and from their goal."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Goal, PlannedWorkout, Result

TODAY = dt.date(2026, 10, 27)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("AS_OF_DATE", TODAY.isoformat())
    from xcapp import config
    config.reload_settings()
    db = SessionLocal()
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="tok-sam")
    db.add(a)
    db.flush()
    db.add(Result(athlete_id=a.id, event="5K", time_s=930.0, date=dt.date(2026, 10, 24), season="2026 XC", kind="race"))
    db.add(Result(athlete_id=a.id, event="5K", time_s=990.0, date=dt.date(2025, 10, 25), season="2025 XC", kind="race"))
    db.add(Goal(athlete_id=a.id, event="5K", season="2026 XC", goal_s=900.0))
    db.add_all([
        PlannedWorkout(date=TODAY, title="6 x 1K @ 5K pace", rep_distance_m=1000, pace_event="5K", pace_factor=1.0),
        PlannedWorkout(date=TODAY + dt.timedelta(days=2), title="Tempo 3 x 1 mile", rep_distance_m=1609, pace_event="5K", pace_factor=1.07),
        PlannedWorkout(date=TODAY + dt.timedelta(days=3), title="8 x 400 @ mile pace", rep_distance_m=400, pace_event="1 Mile", pace_factor=1.0),
        PlannedWorkout(date=TODAY + dt.timedelta(days=1), title="JV hills", rep_distance_m=300, pace_event="5K", groups="JV"),
        PlannedWorkout(date=TODAY - dt.timedelta(days=1), title="Yesterday's reps", rep_distance_m=800, pace_event="5K"),
        PlannedWorkout(date=TODAY + dt.timedelta(days=9), title="Next week's race prep", rep_distance_m=1000, pace_event="5K"),
    ])
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def test_paces_from_current_time_and_goal(app):
    db = SessionLocal()
    a = db.query(Athlete).one()
    w = {pw.title: pw for pw in db.query(PlannedWorkout)}
    k1, tempo, mile = A.workout_paces(db, a, [w["6 x 1K @ 5K pace"], w["Tempo 3 x 1 mile"], w["8 x 400 @ mile pace"]])
    db.close()
    assert [A.fmt_time(k1["now"][k]) for k in ("rep", "mile", "lap")] == ["3:06.0", "4:59.3", "1:14.4"]   # this season's 15:30
    assert k1["now"]["source"] == "your best 5K, 15:30.0"
    assert [A.fmt_time(k1["goal"][k]) for k in ("rep", "mile", "lap")] == ["3:00.0", "4:49.7", "1:12.0"]  # the 15:00 goal
    assert k1["goal"]["source"] == "your goal 5K, 15:00.0"
    assert tempo["now"]["mile"] == pytest.approx(930 / 5000 * 1609.344 * 1.07)                          # threshold: x 1.07
    riegel = 930 * (1609.344 / 5000) ** 1.06                                                              # mile from the 5K
    assert mile["now"]["rep"] == pytest.approx(riegel / 1609.344 * 400)
    assert mile["now"]["source"] == "estimated from your best 5K, 15:30.0"


def test_athlete_page_section(app):
    page = TestClient(app).get("/a/tok-sam").text
    assert "Upcoming workouts" in page and page.index('id="wp-h"') < page.index("My training")   # near the top
    assert "Today · 6 x 1K @ 5K pace" in page and "1,000 m reps · Interval (5K pace)" in page
    assert "Thu, Oct 29 · Tempo 3 x 1 mile" in page and "Threshold (5K pace × 1.07)" in page
    assert "Fri, Oct 30 · 8 x 400 @ mile pace" in page and "Estimated from your best 5K, 15:30.0" in page
    assert '<span class="big">3:06.0</span>' in page and '<span class="big">3:00.0</span>' in page
    assert "4:59.3/mi · 1:14.4/400" in page
    for not_shown in ("JV hills", "Yesterday&#39;s reps", "Next week&#39;s race prep"):  # other group, past, > 7 days
        assert not_shown not in page


def test_missing_time_or_goal_and_no_workouts(app):
    db = SessionLocal()
    db.query(Goal).delete()
    db.query(Result).filter(Result.season == "2026 XC").delete()
    db.commit()
    db.close()
    page = TestClient(app).get("/a/tok-sam").text
    assert "No goal set yet. Ask your coach." in page
    assert "Your best 5K, 16:30.0" in page                           # last season's time is within 400 days
    db = SessionLocal()
    db.query(Result).delete()
    db.commit()
    db.close()
    assert "Ask your coach to record a race or time trial." in TestClient(app).get("/a/tok-sam").text
    db = SessionLocal()
    db.query(PlannedWorkout).delete()
    db.commit()
    db.close()
    page = TestClient(app).get("/a/tok-sam").text
    assert "Upcoming workouts" in page and "No workouts planned for the next 7 days yet." in page

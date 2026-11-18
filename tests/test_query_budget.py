"""SQL statements per page for a team of 40 with two weeks of runs: a fixed budget, so a query per athlete or per run
(an N+1) shows up as a failure instead of a slow page.

If a change needs more statements on a page, check first that the extra ones don't grow with the team (run the test
with N_ATHLETES doubled: the counts should stay put), then raise that page's number in BUDGET."""
import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from xcapp import analysis as A
from xcapp import db as D
from xcapp import points as P
from xcapp.models import Activity, Athlete, Goal, Injury, Lap, PainReport, PlannedWorkout, Result, StaffUser
from xcapp.security import hash_password

N_ATHLETES = 40
BUDGET = {"/": 40, "/dashboard": 40, "/week": 40, "/athletes/1": 60, "/athletes/1/more-runs": 15, "/a/tok-1": 70}


@pytest.fixture
def team():
    """40 athletes, a benchmark (so a VDOT) and a goal each, two team workouts a week, and a run every day for two
    weeks with laps; every other run has no saved zones (as runs from before they were saved)."""
    db = D.SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    today = A.team_today()
    first = A.week_monday(today) - dt.timedelta(days=7)
    workouts = {d: PlannedWorkout(date=d, title=f"6 x 1K {d:%a}", rep_distance_m=1000) for d in
                (first + dt.timedelta(days=k) for k in (1, 3, 8, 10)) if d <= today}
    db.add_all(workouts.values())
    for i in range(1, N_ATHLETES + 1):
        a = Athlete(first_name=f"Runner{i}", last_name=f"Test{i:02d}", gender="Boys" if i % 2 else "Girls", grade=10,
                    group="Varsity", link_token=f"tok-{i}")
        db.add(a)
        db.flush()
        db.add(Result(athlete_id=a.id, event="5K", time_s=1080 + 10 * i, date=today - dt.timedelta(days=30),
                      season=A.season_for(today)))
        db.add(Goal(athlete_id=a.id, event="5K", season=A.season_for(today), goal_s=1050 + 10 * i))
        if i % 10 == 0:
            db.add(Injury(athlete_id=a.id, onset_date=today - dt.timedelta(days=3), injury="Shin splints", body_area="Shin"))
            db.add(PainReport(athlete_id=a.id, date=today, body_area="Shin", severity=5))
        d = first
        while d <= today:
            pw = workouts.get(d)
            act = Activity(athlete_id=a.id, source="upload", external_id=f"r{i}-{d}", start_utc=dt.datetime.combine(d, dt.time(15)),
                           local_date=d, duration_s=2700, distance_m=9000, avg_hr=150, sport="running", device="Forerunner",
                           planned_workout_id=pw.id if pw else None, stream='{"t":[0,3],"d":[0,10],"h":[140,141]}',
                           zone_seconds="" if (d - first).days % 2 else json.dumps({"Easy": 2700}))
            for k in range(8 if pw else 3):
                rep = bool(pw) and k % 2 == 1
                act.laps.append(Lap(idx=k, start_offset_s=k * 300, elapsed_s=300, timer_s=300 if not rep else 210,
                                    distance_m=1000, avg_hr=170 if rep else 145, is_rep=rep, rep_num=k // 2 + 1 if rep else None,
                                    hr_drop_60s=30 if rep else None))
            db.add(act)
            d += dt.timedelta(days=1)
    db.commit()
    db.close()


def signed_in(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c


def count_statements(c, url) -> int:
    n = [0]

    def seen(*_):
        n[0] += 1
    event.listen(D.engine(), "before_cursor_execute", seen)
    try:
        r = c.get(url)
    finally:
        event.remove(D.engine(), "before_cursor_execute", seen)
    assert r.status_code == 200, url
    return n[0]


def test_pages_stay_within_their_query_budget(team):
    from xcapp.web import create_app
    c = signed_in(create_app())
    for url in BUDGET:
        c.get(url)   # the first view saves each workout run's Week-page summary; count a normal view
    counts = {url: count_statements(c, url) for url in BUDGET}
    assert all(counts[url] <= BUDGET[url] for url in BUDGET), counts


def test_athlete_page_loads_the_rest_of_the_runs_on_request(team):
    from xcapp.web import create_app
    c = signed_in(create_app())
    page = c.get("/athletes/1").text
    table = page.split('id="runs-table"')[1].split("</table>")[0]
    with D.SessionLocal() as db:
        n = len(db.scalars(select(Activity.id).where(Activity.athlete_id == 1)).all())
    assert table.count('href="/athletes/1/runs/') == 5 and f"Show all {n} runs" in page
    assert '<tbody id="more-runs" data-src="/athletes/1/more-runs" hidden></tbody>' in table
    more = c.get("/athletes/1/more-runs").text
    assert more.count('href="/athletes/1/runs/') == n - 5 and "<html" not in more


def test_saving_old_runs_zones_keeps_the_points(team):
    """The nightly fill saves the zones the pages were working out from the laps: the Week page doesn't change."""
    from xcapp.web import create_app
    c = signed_in(create_app())
    c.get("/week")
    before = c.get("/week").text
    db = D.SessionLocal()
    empty = len(db.scalars(select(Activity.id).where(Activity.zone_seconds == "")).all())
    assert empty and P.fill_zone_seconds(db) == empty
    db.commit()
    assert P.fill_zone_seconds(db) == 0                                      # once
    assert not db.scalars(select(Activity).where(Activity.zone_seconds == "")).all()
    db.close()
    assert c.get("/week").text == before

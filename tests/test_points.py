"""Training points: zones from smoothed pace, the plan as a target, the day cap, and the weekly score."""
import datetime as dt
import json

import pytest

from xcapp import analysis as A
from xcapp import points as P
from xcapp.models import Activity, Athlete, PlannedWorkout, TrainingGroup

V = 50
PACE = dict(P.zone_paces(V))          # seconds per mile at each zone
MON = dt.date(2026, 10, 19)


def steady(pace_s_per_mile, minutes, start=0.0, d0=0.0):
    """One-second samples of running at a steady pace."""
    speed = A.METERS_PER_MILE / pace_s_per_mile
    return [(start + t, d0 + speed * t) for t in range(int(minutes * 60) + 1)]


def test_rates_and_worked_example():
    assert P.RATES == {"Easy": 0.2, "Moderate": 0.4, "Threshold": 0.6, "Interval": 1.0, "Repetition": 1.5}
    assert P.ZONES == ["Easy", "Moderate", "Threshold", "Interval", "Repetition"]
    week = [40 * 0.2, 10 * 0.2 + 5 * 3 * 1.0 + 5 * 2 * 0.2 + 10 * 0.2, 40 * 0.2, 10 * 0.2 + 20 * 0.6 + 10 * 0.2, 35 * 0.2, 80 * 0.2]
    assert [round(x) for x in week] == [8, 21, 8, 16, 7, 16] and round(sum(week)) == 76
    assert round(P.week_score(61, 76, 5, 6)) == 81                                  # 0.8 x 80 + 0.2 x 83


def test_zone_for_uses_midpoints_and_walking():
    paces = P.zone_paces(V)
    assert [P.zone_for(p, paces) for _, p in paces] == P.ZONES
    cut = (PACE["Easy"] + PACE["Moderate"]) / 2
    assert P.zone_for(cut + 1, paces) == "Easy" and P.zone_for(cut - 1, paces) == "Moderate"
    assert P.zone_for(16 * 60, paces) is None and P.zone_for(PACE["Easy"] + 120, paces) == "Easy"   # walking vs slow jogging


def test_zone_seconds_smooths_and_skips_pauses():
    run = steady(PACE["Easy"], 10)
    tempo = steady(PACE["Threshold"], 10, start=run[-1][0] + 1, d0=run[-1][1])
    pause_end = tempo[-1][0] + 120                                                  # two minutes stopped: no samples
    more = steady(PACE["Easy"], 5, start=pause_end, d0=tempo[-1][1])
    z = P.zone_seconds(run + tempo + more, V)
    assert set(z) <= {"Easy", "Moderate", "Threshold"}
    assert z["Easy"] == pytest.approx(15 * 60, abs=40) and z["Threshold"] == pytest.approx(10 * 60, abs=40)
    assert P.moving_minutes(z) == pytest.approx(25, abs=0.1)                        # the pause doesn't count
    noisy = [(t, d + (3 if t % 2 else -3)) for t, d in steady(PACE["Easy"], 10)]   # GPS jitter of 6 m a second
    assert P.zone_seconds(noisy, V).get("Easy", 0) > 9 * 60
    walk = steady(18 * 60, 5)
    assert set(P.zone_seconds(walk, V)) == {"walk"} and P.zone_points(P.zone_seconds(walk, V)) == 0
    assert set(P.zone_seconds(steady(PACE["Threshold"], 5), None)) == {"Easy"}     # no VDOT yet: all easy


def test_plan_points():
    minutes, miles, pts = P.part_plan({"sets": 1, "reps": 5, "mins": 3, "zone": "Interval"}, V)
    assert (minutes, pts) == (15, 15.0) and miles == pytest.approx(15 * 60 / PACE["Interval"])
    minutes, miles, pts = P.part_plan({"sets": 2, "reps": 4, "dist": 400, "zone": "Repetition"}, V)
    assert miles == pytest.approx(3200 / A.METERS_PER_MILE) and pts == pytest.approx(minutes * 1.5)
    pw = PlannedWorkout(date=MON, title="x", rep_distance_m=0, parts=json.dumps(
        [{"sets": 1, "reps": 1, "mins": 10, "zone": "Easy"}, {"sets": 1, "reps": 1, "mins": 20, "zone": "Threshold"}]))
    assert P.workout_plan(pw, V)[1] == pytest.approx(2 + 12)


def make_week(session, runs, workouts=(), goal=30, rpe=None):
    """An athlete at VDOT 50 with a weekly miles goal for her group, and runs as (day offset, [(pace, minutes)])."""
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava", vdot_override=V)
    session.add(a)
    session.flush()
    group = A.abilities(session, MON + dt.timedelta(days=6))[a.id]["group"]
    g = session.query(TrainingGroup).filter_by(name=group).one()
    g.weekly_miles_girls = goal
    for day, parts in workouts:
        session.add(PlannedWorkout(date=MON + dt.timedelta(days=day), title="w", rep_distance_m=0, groups="All", parts=json.dumps(parts)))
    for i, (day, segs) in enumerate(runs):
        samples, t, d = [], 0.0, 0.0
        for pace, mins in segs:
            s = steady(pace, mins, start=t, d0=d)
            samples += s
            t, d = s[-1][0] + 1, s[-1][1]
        zones = P.zone_seconds(samples, V)
        session.add(Activity(athlete_id=a.id, source="upload", external_id=f"r{i}", local_date=MON + dt.timedelta(days=day),
                             start_utc=dt.datetime(2026, 10, 19, 23) + dt.timedelta(days=day), duration_s=t, distance_m=d,
                             avg_hr=150, sport="running", zone_seconds=json.dumps(zones), zone_vdot=V, rpe=rpe))
    session.flush()
    return a


TUESDAY = (1, [{"sets": 1, "reps": 1, "mins": 10, "zone": "Easy"}, {"sets": 1, "reps": 5, "mins": 3, "zone": "Interval"},
               {"sets": 1, "reps": 1, "mins": 10, "zone": "Easy"}])        # 2 + 15 + 2 = 19 points


def test_perfect_week(session):
    e, i = PACE["Easy"], PACE["Interval"]
    runs = [(0, [(e, 40)]), (1, [(e, 10), (i, 15), (e, 10)]), (2, [(e, 40)]), (3, [(e, 40)]), (4, [(e, 35)]), (5, [(e, 80)])]
    a = make_week(session, runs, [TUESDAY], goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    easy_miles = 30 - P.workout_plan(session.query(PlannedWorkout).one(), V)[0]
    assert w["target"] == pytest.approx(19 + easy_miles * PACE["Easy"] / 60 * 0.2)
    tue = w["days"][1]
    assert tue["planned"] == pytest.approx(19) and tue["points"] == pytest.approx(19, abs=0.5)
    assert w["run_days"] == 6 and not w["flags"]
    assert w["score"] == pytest.approx(100 * (0.8 * min(w["points"] / w["target"], 1) + 0.2))


def test_too_fast_easy_week(session):
    t = PACE["Threshold"]
    runs = [(0, [(t, 40)]), (1, [(t, 50)]), (2, [(t, 40)])]                       # everything at threshold, Tuesday too
    a = make_week(session, runs, [TUESDAY], goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    assert w["days"][0]["points"] == pytest.approx(40 * 0.2, abs=0.2)               # no plan that day: easy minutes only
    assert w["days"][1]["points"] == pytest.approx(19)                              # capped at the day's plan
    assert any("faster than easy pace" in f for f in w["flags"])


def test_missed_week_and_extra_cap(session):
    a = make_week(session, [(2, [(PACE["Easy"], 20)])], [TUESDAY], goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    assert w["run_days"] == 0 and w["days"][1]["points"] == 0                        # 20 minutes is under the 30-minute day
    assert w["score"] == pytest.approx(100 * 0.8 * w["points"] / w["target"])
    session.rollback()
    b = make_week(session, [(d, [(PACE["Easy"], 150)]) for d in range(7)], goal=10)
    w = P.team_week(session, MON, athletes=[b])[0]
    assert w["points"] == pytest.approx(1.1 * w["target"])                          # extra running counts up to 110%


def test_week_page_and_athlete_panel():
    import re

    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    today = A.team_today()
    monday = A.week_monday(today)
    a = make_week(db, [(0, [(PACE["Easy"], 40)])], goal=30)
    act = db.query(Activity).one()
    act.local_date = monday                                  # this week, for the athlete's own page
    boy = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam", vdot_override=60)
    db.add(boy)
    db.commit()
    aid = a.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    girls = c.get(f"/week?team=Girls&start={monday.isoformat()}").text
    assert "Girls week" in girls and f'href="/athletes/{aid}">Ava Doyle</a>' in girls and "Sam Lowry" not in girls
    assert re.search(r"<b>\d+%</b>", girls) and "of 30" in girls and "Effort" not in girls
    boys = c.get("/week?team=Boys").text
    assert "Boys week" in boys and "Sam Lowry" in boys and "No score (–)" in boys   # Sam's group has no weekly goal
    csrf = re.search(r'name="csrf" value="([^"]+)"', girls).group(1)
    c.post("/week/settings", data={"csrf": csrf, "team": "Girls", "start": monday.isoformat(), "target_days": "5", "min_minutes": "25"})
    page = c.get(f"/week?team=Girls&start={monday.isoformat()}").text
    assert "at least 25 minutes run (target 5)" in page and "of 5" in page
    c.post("/week/settings", data={"csrf": csrf, "team": "Girls", "target_days": "9", "min_minutes": "25"})
    assert "Target days run from 1 to 7" in c.get("/week?team=Girls").text
    me = TestClient(app).get("/a/t-ava").text
    assert "This week’s points" in me and "Hit it and you’re at 100%" in me
    page = c.get(f"/athletes/{aid}").text
    assert '<option value="2" selected>2: usual</option>' in page
    c.post(f"/athletes/{aid}/edit", data={"csrf": csrf, "grade": "11", "experience": "3"})
    assert "Experience level 3: experienced (10% more miles)." in c.get(f"/athletes/{aid}").text
    assert "experienced +10%" in c.get(f"/week?team=Girls&start={monday.isoformat()}").text and "of 33" in c.get(f"/week?team=Girls&start={monday.isoformat()}").text


def test_experience_changes_the_miles_goal_and_target(session):
    runs = [(0, [(PACE["Easy"], 40)])]
    a = make_week(session, runs, goal=30)
    usual = P.team_week(session, MON, athletes=[a])[0]
    assert usual["goal_miles"] == 30
    a.experience = 1
    new = P.team_week(session, MON, athletes=[a])[0]
    a.experience = 3
    pro = P.team_week(session, MON, athletes=[a])[0]
    assert new["goal_miles"] == pytest.approx(27) and pro["goal_miles"] == pytest.approx(33)
    easy_points_per_mile = PACE["Easy"] / 60 * 0.2
    assert usual["target"] - new["target"] == pytest.approx(3 * easy_points_per_mile)
    assert pro["target"] - usual["target"] == pytest.approx(3 * easy_points_per_mile)
    goal = A.mileage_goal(session, a.id, usual["group_name"], MON, "Girls", 3)
    assert goal["goal"] == pytest.approx(33)

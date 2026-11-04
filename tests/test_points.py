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
    assert w["target"] == pytest.approx(19)                                      # only the posted workout
    easy_miles = 30 - P.workout_plan(session.query(PlannedWorkout).one(), V)[0]
    assert w["why_target"]["easy"]["miles"] == pytest.approx(easy_miles) and w["why_target"]["easy"]["points"] == 0
    tue = w["days"][1]
    assert tue["planned"] == pytest.approx(19) and tue["points"] == pytest.approx(19, abs=0.5)
    assert [d["points"] for i, d in enumerate(w["days"]) if i != 1] == [0] * 6       # easy days: miles, no points
    assert w["run_days"] == 6 and not w["flags"]
    assert w["score"] == pytest.approx(100 * (0.8 * min(w["points"] / w["target"], 1) + 0.2))


def test_too_fast_easy_week(session):
    t = PACE["Threshold"]
    runs = [(0, [(t, 40)]), (1, [(t, 50)]), (2, [(t, 40)])]                       # everything at threshold, Tuesday too
    a = make_week(session, runs, [TUESDAY], goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    assert w["days"][0]["points"] == 0                                              # no posted workout that day: no points
    assert w["days"][1]["points"] == pytest.approx(19)                              # capped at the workout's plan
    assert any("faster than easy pace" in f for f in w["flags"])


def test_race_days_count_and_short_days_are_marked(session):
    from xcapp.models import RaceResult
    a = make_week(session, [(1, [(PACE["Easy"], 40)]), (4, [(PACE["Easy"], 25)]), (5, [(PACE["Interval"], 12), (PACE["Easy"], 8)])], goal=30)
    session.add(RaceResult(athlete_id=a.id, meet="Invite", date=MON + dt.timedelta(days=5), race="JV", distance_m=3218, time_s=720))
    session.flush()
    w = P.team_week(session, MON, athletes=[a])[0]
    assert w["run_days"] == 2                                                        # Tuesday and the race on Saturday
    assert [(d["counts"], d["race"]) for d in w["days"][4:6]] == [(False, False), (True, True)]
    assert w["no_score"] == "" or w["target"] is None
    session.query(PlannedWorkout).delete()
    session.flush()
    none = P.team_week(session, MON, athletes=[a])[0]
    assert none["score"] is None and none["no_score"] == "No team workout posted yet this week"


def test_every_day_a_workout_reaches_the_target(session):
    """A week with a team workout every run day: the target is the six workouts, and doing each one as planned is
    100%; the extra easy minutes after each count as miles, not points."""
    e, i = PACE["Easy"], PACE["Interval"]
    days = [(d, [{"sets": 1, "reps": 1, "mins": 10, "zone": "Easy"}, {"sets": 1, "reps": 4, "mins": 3, "zone": "Interval"}]) for d in range(6)]
    runs = [(d, [(e, 10), (i, 12), (e, 20)]) for d in range(6)]          # each workout, then 20 easy minutes more
    a = make_week(session, runs, days, goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    plan = w["days"][0]["planned"]
    assert plan == pytest.approx(10 * 0.2 + 12 * 1.0, abs=0.01)
    assert w["why_target"]["easy"]["miles"] > 0                              # 30 miles is more than the six workouts
    assert all(d["points"] == pytest.approx(plan, abs=0.3) for d in w["days"][:6])
    assert w["target"] == pytest.approx(6 * plan) and w["points"] == pytest.approx(w["target"], rel=0.01)
    # Running the workout harder than planned, for no longer, still earns only the plan
    session.rollback()
    b = make_week(session, [(0, [(e, 10), (PACE["Repetition"], 12)])], days[:1], goal=30)
    assert P.team_week(session, MON, athletes=[b])[0]["days"][0]["points"] == pytest.approx(plan, abs=0.3)


def test_missed_week_and_extra_cap(session):
    a = make_week(session, [(2, [(PACE["Easy"], 20)])], [TUESDAY], goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    assert w["run_days"] == 0 and w["days"][1]["points"] == 0                        # 20 minutes is under the 30-minute day
    assert w["score"] == pytest.approx(100 * 0.8 * w["points"] / w["target"])
    session.rollback()
    b = make_week(session, [(d, [(PACE["Easy"], 150)]) for d in range(7)], goal=10)
    w = P.team_week(session, MON, athletes=[b])[0]
    assert w["points"] == 0 and w["target"] is None and w["score"] is None           # no workouts posted: no points
    assert w["miles"] > 60 and w["run_days"] == 7                                     # the running still shows


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
    db.add(PlannedWorkout(date=monday + dt.timedelta(days=1), title="w", rep_distance_m=0, groups="All", gender="Girls", parts=json.dumps(TUESDAY[1])))
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
    assert re.search(r"<b>\d+%</b>", girls) and "5.0/day" in girls and "Effort" not in girls
    boys = c.get("/week?team=Boys").text
    assert "Boys week" in boys and "Sam Lowry" in boys and "No score (–)" in boys   # no workout posted for the boys
    csrf = re.search(r'name="csrf" value="([^"]+)"', girls).group(1)
    c.post("/week/settings", data={"csrf": csrf, "team": "Girls", "start": monday.isoformat(), "target_days": "5", "min_minutes": "25", "rest_day": "2"})
    page = c.get(f"/week?team=Girls&start={monday.isoformat()}").text
    assert "at least 25 minutes run (target 5)" in page and "of 5" in page
    assert '<option value="2" selected>Wed</option>' in page and "5 run days and 2 rest days (Wed first)" in page
    c.post("/week/settings", data={"csrf": csrf, "team": "Girls", "target_days": "9", "min_minutes": "25"})
    assert "Target days run from 1 to 7" in c.get("/week?team=Girls").text
    me = TestClient(app).get("/a/t-ava").text
    assert "This week’s points" in me and "Hit them all and you’re at 100%" in me
    page = c.get(f"/athletes/{aid}").text
    assert '<option value="2" selected>2: usual</option>' in page
    c.post(f"/athletes/{aid}/edit", data={"csrf": csrf, "grade": "11", "experience": "3"})
    assert "Experience level 3: experienced (10% more miles)." in c.get(f"/athletes/{aid}").text
    wk = c.get(f"/week?team=Girls&start={monday.isoformat()}").text
    assert "experienced +10%" in wk and "6.6/day" in wk                    # 33 miles over the 5 run days set above


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
    # Experience changes the miles goal (and the easy miles beside the workouts), not the points target
    assert usual["target"] == new["target"] == pro["target"]
    assert usual["why_target"]["easy"]["miles"] - new["why_target"]["easy"]["miles"] == pytest.approx(3)
    goal = A.mileage_goal(session, a.id, usual["group_name"], MON, "Girls", 3)
    assert goal["goal"] == pytest.approx(33)


def test_keep_pace_colours():
    assert [P.pace_status(x) for x in (None, 120, 90, 89.9, 80, 79.9)] == ["", "good", "good", "warn", "warn", "bad"]
    days = [{"date": MON + dt.timedelta(days=i), "miles": m, "points": p, "planned": pl, "plan_miles": pm} for i, (m, p, pl, pm) in enumerate(
        [(0, 0, None, None), (6, 10, None, None), (5, 20, 20, 5), (0, 0, None, None), (0, 0, None, None), (0, 0, None, None), (0, 0, None, None)])]
    week = {"days": days, "goal_miles": 35, "target": 70, "why_target": {"easy": {"miles": 30, "points": 50}}}
    P.plan_days(week, 6, 6)
    # 6 run days, one a workout, so 5 easy days share the 30 easy miles and 50 points; Sunday rests
    assert [d["kind"] for d in days] == ["easy", "easy", "workout", "easy", "easy", "easy", "rest"]
    assert [d["due_m"] for d in days] == [6, 6, 5, 6, 6, 6, 0] and sum(d["due_p"] for d in days) == 70
    P.keep_pace(week, MON + dt.timedelta(days=2))
    assert [round(d["miles_pct"]) if d["miles_pct"] is not None else None for d in days] == [None, 100, 100, None, None, None, None]
    assert [round(d["points_pct"]) if d["points_pct"] is not None else None for d in days] == [None, 100, 100, None, None, None, None]
    assert days[2]["why_miles"] == "5.0 of 5.0 mi (planned for the workout): 100%"
    assert days[1]["why_points"] == "10 of 10 pts (easy day: the miles the workouts leave ÷ 5 easy days): 100%"
    # The week so far: 11 of 17 mi due through Wednesday (6 + 6 + 5), 30 of 40 points
    # Miles: 35 over 6 run days is 5.8 a day, so 17.5 are due by Wednesday
    assert round(week["miles_pct"]) == 63 and P.pace_status(week["miles_pct"]) == "bad"
    assert week["why_miles"] == "11.0 of 17.5 mi due through Wed (5.8 mi a day over 6 run days, 35 for the week): 63%"
    assert week["miles_per_day"] == pytest.approx(35 / 6) and week["miles_due"] == pytest.approx(17.5)
    assert week["points_due"] == pytest.approx(40)                                   # Mon 10 + Tue 10 + Wed's workout 20
    P.keep_pace(week, MON - dt.timedelta(days=1))                                    # Sunday before: nothing due yet
    assert week["miles_due"] is None and week["points_due"] is None
    P.keep_pace(week, MON + dt.timedelta(days=2))                                    # back to Wednesday
    assert round(week["points_pct"]) == 75
    P.keep_pace(week, MON + dt.timedelta(days=5))            # Saturday: the whole goal is due already
    assert week["why_miles"] == "11.0 of 35.0 mi due through Sat (5.8 mi a day over 6 run days, 35 for the week): 31%"
    days[6].update(miles=6, points=10)                        # a run on the rest day is compared with an easy day
    P.keep_pace(week, MON + dt.timedelta(days=6))
    assert round(days[6]["miles_pct"]) == 100 and "rest day" in days[6]["why_miles"]
    assert week["why_points"].startswith("40 of 70 pts due the whole week")


def test_plan_days_other_rest_day_and_fewer_run_days():
    days = [{"date": MON + dt.timedelta(days=i), "miles": 0, "points": 0, "planned": None, "plan_miles": None} for i in range(7)]
    week = {"days": days, "why_target": {"easy": {"miles": 25, "points": 50}}}
    P.plan_days(week, 5, 2)                                   # 5 run days, Wednesday off: Sunday rests too
    assert [d["kind"] for d in days] == ["easy", "easy", "rest", "easy", "easy", "easy", "rest"]
    assert days[0]["due_m"] == 5 and days[0]["due_p"] == 10


def test_week_links_and_colours():
    import re

    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import MissedDay, PainReport, StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = make_week(db, [(0, [(PACE["Easy"], 60)])], [TUESDAY], goal=30)
    db.add(PainReport(athlete_id=a.id, date=MON, body_area="Knee", severity=5))
    db.add(MissedDay(athlete_id=a.id, date=MON + dt.timedelta(days=2), reason="Injury", body_area="Foot", side="Left", pain=4))
    db.commit()
    pid = db.query(PainReport).one().id
    mid = db.query(MissedDay).one().id
    wid = db.query(PlannedWorkout).one().id
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get(f"/week?team=Girls&start={MON.isoformat()}").text
    assert f'<a class="tag" href="/workouts?edit={wid}#plan"' in page
    assert f'<a class="pill bad" href="/injuries?pain={pid}#pain-{pid}"' in page and f'href="/injuries?missed={mid}#missed-{mid}"' in page
    assert 'class="day d2' in page and 'class="num pace-' in page and '<b class="pace-' in page
    inj = c.get(f"/injuries?pain={pid}&missed={mid}").text                 # older than four weeks, still shown when linked
    assert f'<tr id="pain-{pid}">' in inj and f'<tr id="missed-{mid}">' in inj



def test_score_on_track_colour():
    days = [{"date": MON + dt.timedelta(days=i), "points": p, "planned": None} for i, p in enumerate([10, 10, 0, 0, 0, 0, 0])]
    week = {"days": days, "target": 60, "run_days": 2, "why_target": {"easy": {"miles": 30, "points": 60}}}
    P.plan_days(week, 6, 6)                                  # 10 points a day Monday to Saturday, Sunday off
    P.score_pace(week, MON + dt.timedelta(days=1), 6)       # Tuesday: 20 of 20 points due, 2 of 2 days
    assert round(week["score_pct"]) == 100 and P.pace_status(week["score_pct"]) == "good"
    P.score_pace(week, MON + dt.timedelta(days=3), 6)       # Thursday, nothing more: 20 of 40 points, 2 of 4 days
    assert round(week["score_pct"]) == 50 and P.pace_status(week["score_pct"]) == "bad"
    assert week["why_score"].startswith("On track for 50%: points 20 of 40 due by Thu (50%)")
    P.score_pace(week, MON + dt.timedelta(days=9), 6)       # week over: the score itself (80% x 20/60 + 20% x 2/6)
    assert week["score_pct"] == pytest.approx(100 * (0.8 * 20 / 60 + 0.2 * 2 / 6))


def test_one_workout_a_day_counts_toward_the_target():
    import json
    from xcapp.ingest import pick_workout
    from xcapp.models import PlannedWorkout
    team = PlannedWorkout(id=1, groups="All", gender="", title="Team tempo")
    gold = PlannedWorkout(id=2, groups="Gold", gender="", title="Easy run · Gold")
    assert pick_workout([team, gold]) is gold and pick_workout([team]) is team and pick_workout([]) is None


def test_two_workouts_on_one_day_count_once(session):
    extra = (1, [{"sets": 1, "reps": 1, "mi": 7, "dist": 11265, "zone": "Easy"}])    # a second workout the same Tuesday
    a = make_week(session, [], [TUESDAY, extra], goal=30)
    w = P.team_week(session, MON, athletes=[a])[0]
    first = session.query(PlannedWorkout).order_by(PlannedWorkout.id).first()
    one = P.workout_plan(first, V)
    assert w["days"][1]["planned"] == pytest.approx(one[1])                          # not both workouts
    assert w["target"] == pytest.approx(one[1])                                       # only the one workout counted
    why = w["why_target"]
    assert len(why["workouts"]) == 1 and why["workouts"][0]["not_counted"] == ["w"] and why["easy"]["miles"] == pytest.approx(30 - one[0])

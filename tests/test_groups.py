"""VDOT, ability groups, workouts aimed at a group, and run type / team workout chosen by hand."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from fitgen import rep_workout
from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.ingest import rematch_day
from xcapp.models import Activity, Athlete, PlannedWorkout, Result, StaffUser, TrainingGroup
from xcapp.security import hash_password


def test_vdot_matches_daniels_tables():
    assert 55.8 <= A.vdot(5000, 18 * 60) <= 56.8      # Daniels: VDOT 56 is about an 18:03 5K
    assert 59.0 <= A.vdot(1609.344, 300) <= 60.2       # a 5:00 mile is about VDOT 59-60
    assert 49.0 <= A.vdot(5000, 20 * 60) <= 50.5       # a 20:00 5K is about VDOT 49.8
    assert A.vdot(5000, 16 * 60) > A.vdot(5000, 17 * 60) > A.vdot(5000, 18 * 60)


def add(db, first, gender, results, as_of=dt.date(2026, 10, 25)):
    a = Athlete(first_name=first, last_name="T", gender=gender, grade=11, group="JV", link_token="tok-" + first.lower())
    db.add(a)
    db.flush()
    for event, secs, days_ago in results:
        d = as_of - dt.timedelta(days=days_ago)
        db.add(Result(athlete_id=a.id, event=event, time_s=secs, date=d, season=A.season_for(d)))
    db.flush()
    return a


def test_groups_from_highest_vdot(session):
    names = [g.name for g in session.query(TrainingGroup).order_by(TrainingGroup.position)]
    assert names == ["Platinum", "Gold", "Silver", "Bronze", "Emerald", "Aquamarine", "Ivory"]
    boy = add(session, "Boy", "Boys", [("5K", 17 * 60 + 40, 10), ("1 Mile", 5 * 60 + 10, 60)])   # best is the 5K: VDOT 57
    girl = add(session, "Girl", "Girls", [("5K", 20 * 60, 20)])                                 # VDOT 50
    slow = add(session, "Slow", "Girls", [("5K", 23 * 60, 5)])                                  # about 42
    old = add(session, "Old", "Boys", [("5K", 15 * 60 + 30, 500)])                               # outside 365 days
    none = add(session, "None", "Boys", [])
    ab = A.abilities(session, dt.date(2026, 10, 25))
    assert (ab[boy.id]["group"], ab[boy.id]["vdot"]) == ("Emerald", 57) and ab[boy.id]["source"].startswith("5K 17:40.0")
    assert (ab[boy.id]["target_800"], ab[boy.id]["target_1600"], ab[boy.id]["color"]) == ("2:19", "5:11", "#50C878")
    assert (ab[girl.id]["group"], ab[girl.id]["vdot"]) == ("Aquamarine", 50) and ab[slow.id]["group"] == "Ivory"
    assert ab[slow.id]["target_1600"] == ""                                       # below the table's lowest row
    assert ab[old.id]["group"] == "" and ab[none.id]["vdot"] is None
    A.set_setting(session, "group_lookback_days", "730")
    assert A.abilities(session, dt.date(2026, 10, 25))[old.id]["group"] == "Silver"
    boy.ability_override = "Bronze"
    ab = A.abilities(session, dt.date(2026, 10, 25))
    assert (ab[boy.id]["group"], ab[boy.id]["auto_group"], ab[boy.id]["override"]) == ("Bronze", "Emerald", "Bronze")
    rows = {r["athlete"]: r for r in A.roster_rows(session, dt.date(2026, 10, 25))}
    assert rows["Girl T"]["ability_group"] == "Aquamarine" and rows["Girl T"]["vdot"] == 50


def test_team_table_sets_the_vdot_scale(session):
    """Running exactly the team sheet's 1600 target for a VDOT puts an athlete at that VDOT; a second slower does not."""
    targets = A.vdot_targets(session)
    assert len(targets) == 27 and targets[60] == ("2:13", "4:57")
    for n, (_, t16) in targets.items():
        secs = A._secs(t16)
        assert A.team_vdot(A.vdot(1600, secs), targets) == n, n
        if n > min(targets):
            assert A.team_vdot(A.vdot(1600, secs + 1), targets) == n - 1, n
    assert A.team_vdot(A.vdot(1600, 4 * 60), targets) >= 75               # faster than the top row
    assert A.team_vdot(A.vdot(5000, 25 * 60), targets) < 48               # slower than the bottom row


def test_workout_for_one_ability_group(session):
    gold = add(session, "Fast", "Boys", [("5K", 17 * 60 + 40, 10)])   # VDOT 57: Emerald
    bronze = add(session, "Newer", "Boys", [("5K", 21 * 60, 10)])      # about 47: Ivory
    session.add(PlannedWorkout(date=dt.date(2026, 10, 20), title="Emerald: 6 x 1K", rep_distance_m=1000, groups="Emerald"))
    session.flush()
    from xcapp.fitfile import parse_fit
    from xcapp.ingest import ingest
    start = dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc)
    a1, _ = ingest(session, gold, parse_fit(rep_workout(start, [(1000, 200, 185, 30), (1000, 200, 187, 0)])), "upload")
    a2, _ = ingest(session, bronze, parse_fit(rep_workout(start, [(1000, 230, 185, 30), (1000, 230, 187, 0)])), "upload")
    assert a1.planned is not None and a1.planned.title == "Emerald: 6 x 1K" and a2.planned_workout_id is None


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("AS_OF_DATE", "2026-10-25")
    from xcapp import config
    config.reload_settings()
    from xcapp.web import create_app
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    ava = add(db, "Ava", "Girls", [("5K", 19 * 60 + 30, 8)])
    db.add(PlannedWorkout(date=dt.date(2026, 10, 20), title="5 x 1K @ 5K pace", rep_distance_m=1000))
    db.add(PlannedWorkout(date=dt.date(2026, 10, 21), title="Hill repeats", rep_distance_m=200))
    db.add(PlannedWorkout(date=dt.date(2026, 9, 1), title="Old workout", rep_distance_m=400))
    db.commit()
    aid = ava.id
    db.close()
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/tok-ava").text).group(1)
    fit = rep_workout(dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc), [(1000, 200, 185, 30), (1000, 200, 187, 25), (1000, 200, 189, 0)])
    c.post("/a/tok-ava/upload", data={"csrf": csrf}, files={"file": ("run.fit", fit, "application/octet-stream")})
    db = SessionLocal()
    act_id = db.query(Activity).one().id
    db.close()
    return app, c, csrf, aid, act_id


def staff_login(app, user):
    c = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"csrf": tok, "username": user, "password": "correct-horse-1"}).status_code == 303
    return c


def test_athlete_sets_type_and_workout(web):
    app, c, csrf, aid, act_id = web
    page = c.get(f"/a/tok-ava/runs/{act_id}").text
    assert "Type of run" in page and "Hill repeats (Wed, Oct 21)" in page and "Old workout" not in page
    assert re.search(r'value="Workout" checked', page)                         # suggested from the matched workout
    me = c.get("/a/tok-ava").text
    assert '<summary class="group-btn" style="background:#7FDBCA; color:#1B1B1F">Aquamarine · VDOT 51</summary>' in me  # 19:30 girls' 5K
    assert "<b>Running group: Aquamarine</b>" in me
    assert ">2:31<" in me and ">5:44<" in me                                   # the team sheet's targets for VDOT 51
    r = c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "7", "pain_area": "None", "run_type": "Race", "workout": "none"})
    assert r.status_code == 303
    db = SessionLocal()
    act = db.get(Activity, act_id)
    assert (act.run_type, act.planned_workout_id, act.workout_locked) == ("Race", None, True)
    assert rematch_day(db, act.local_date) == 0 and act.planned_workout_id is None   # automatic matching leaves it alone
    old = db.query(PlannedWorkout).filter_by(title="Old workout").one().id
    db.close()
    c.post(f"/a/tok-ava/runs/{act_id}", data={"csrf": csrf, "rpe": "7", "pain_area": "None", "run_type": "Workout", "workout": str(old)})
    assert "within three days" in c.get(f"/a/tok-ava/runs/{act_id}").text
    s = c.get("/a/tok-ava/series.json").json()
    assert s["runs"][0]["type"] == "Race" and s["runs"][0]["workout"] == "Race"


def test_coach_sets_type_workout_group_and_cutoffs(web):
    app, c, csrf, aid, act_id = web
    co = staff_login(app, "coach1")
    tok = lambda url: re.search(r'name="csrf" value="([^"]+)"', co.get(url).text).group(1)
    page = co.get(f"/athletes/{aid}/runs/{act_id}").text
    assert "Type and workout" in page and "HR drop in 60 s" in page
    db = SessionLocal()
    hill = db.query(PlannedWorkout).filter_by(title="Hill repeats").one().id
    db.close()
    co.post(f"/athletes/{aid}/runs/{act_id}", data={"csrf": tok(f"/athletes/{aid}/runs/{act_id}"), "run_type": "Workout", "workout": str(hill)})
    db = SessionLocal()
    act = db.get(Activity, act_id)
    assert act.planned_workout_id == hill and act.workout_locked
    db.close()
    tr = staff_login(app, "trainer1")
    assert tr.post(f"/athletes/{aid}/runs/{act_id}", data={"csrf": "x", "run_type": "Easy"}).status_code == 403
    co.post(f"/athletes/{aid}/ability", data={"csrf": tok(f"/athletes/{aid}"), "ability_override": "Platinum"})
    assert "Platinum" in co.get("/athletes").text and "(pinned)" in co.get("/athletes").text
    db = SessionLocal()
    gs = {g.name: g.id for g in db.query(TrainingGroup)}
    db.close()
    form = {"csrf": tok("/groups"), "lookback": "365", "show_athletes": "on", "new_name": "Diamond", "new_boys": "64", "new_girls": "58"}
    for name, gid in gs.items():
        form.update({f"name_{gid}": "Elite" if name == "Platinum" else name, f"position_{gid}": "0", f"boys_{gid}": "0", f"girls_{gid}": "0"})
    form[f"girls_{gs['Silver']}"] = "52"
    co.post("/groups", data=form)
    page = co.get("/groups").text
    assert "Groups saved" in page and 'value="Diamond"' in page and 'value="Elite"' in page
    db = SessionLocal()
    assert db.get(Athlete, aid).ability_override == "Elite"                       # rename follows the pinned athlete
    db.close()
    form["show_athletes"] = ""
    form.pop("new_name")
    form[f"name_{gs['Platinum']}"] = "Elite"
    co.post("/groups", data={k: v for k, v in form.items() if v != ""})
    assert "Training group" not in c.get("/a/tok-ava").text                        # hidden from athletes when switched off
    bad = dict(form, **{f"boys_{gs['Gold']}": "fast"})
    co.post("/groups", data={k: v for k, v in bad.items() if v != ""})
    assert "Check the numbers" in co.get("/groups").text
    co.post("/groups", data={k: v for k, v in dict(form, **{f"color_{gs['Gold']}": "yellow"}).items() if v != ""})
    assert "Colors look like" in co.get("/groups").text
    co.post("/groups", data={k: v for k, v in dict(form, targets="60,2:13,4:57\n47,2:39\n").items() if v != ""})
    assert "line 2 should look like" in co.get("/groups").text
    co.post("/groups", data={k: v for k, v in dict(form, targets="74,1:50,4:06\n47,2:39,6:10\n").items() if v != ""})
    db = SessionLocal()
    assert A.vdot_targets(db) == {74: ("1:50", "4:06"), 47: ("2:39", "6:10")}
    db.close()

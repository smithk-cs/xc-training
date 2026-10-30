"""Weekly mileage goals per training group: set on the Groups page, shown to athletes and coaches."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, StaffUser, TrainingGroup
from xcapp.security import hash_password


def test_weekly_mileage_goals():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam", vdot_override=69)
    db.add(a)
    db.flush()
    today = A.team_today()
    monday = A.week_monday(today)
    for i, miles in enumerate([6, 4]):  # this week
        db.add(Activity(athlete_id=a.id, source="upload", external_id=f"n{i}", start_utc=dt.datetime(monday.year, monday.month, monday.day, 15),
                        local_date=monday, duration_s=miles * 420, distance_m=miles * 1609.344, sport="running"))
    last = monday - dt.timedelta(days=3)
    db.add(Activity(athlete_id=a.id, source="upload", external_id="old", start_utc=dt.datetime(last.year, last.month, last.day, 15),
                    local_date=last, duration_s=40 * 420, distance_m=40 * 1609.344, sport="running"))
    aid = a.id
    db.commit()
    groups = db.query(TrainingGroup).all()
    ids = (next(g.id for g in groups if g.name == "Gold"), next(g.id for g in groups if g.name == "Silver"))
    fields = {}
    for g in groups:
        fields.update({f"name_{g.id}": g.name, f"position_{g.id}": str(g.position), f"boys_{g.id}": str(g.min_vdot_boys),
                       f"girls_{g.id}": str(g.min_vdot_girls), f"color_{g.id}": g.color})
    db.close()
    from xcapp.web import create_app
    app = create_app()
    me = TestClient(app).get("/a/t-sam").text
    assert "This week’s miles" not in me                                             # no goal yet
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/groups").text).group(1)
    base = {"csrf": csrf, "lookback": "365", "show_athletes": "on", **fields}
    c.post("/groups", data={**base, f"miles_{ids[0]}": "200"})
    assert "Weekly mileage goals run from 0 to 150 miles." in c.get("/groups").text
    c.post("/groups", data={**base, f"miles_{ids[0]}": "35", f"miles_{ids[1]}": ""})
    page = c.get("/groups").text
    assert 'name="miles_%d" value="35"' % ids[0] in page and 'name="miles_%d" value=""' % ids[1] in page
    assert "goal 35 mi · 0 of 1 there" in page and "10.0 / 35" in page and "40.0 ✓" in page
    me = TestClient(app).get("/a/t-sam").text
    assert "This week’s miles" in me and "<span class=\"big\">10.0</span>" in me and "of 35 mi</span>" in me and "the Gold goal" not in me and "25.0" in me
    assert "left after today" not in me and 'class="facts one-line"' in me
    db = SessionLocal()
    goal = A.mileage_goal(db, aid, "Gold", today)
    db.close()
    assert goal["last"] == 40 and goal["done"] == 10


def test_chart_data_has_the_weekly_goal_and_coach_page_shows_goal_vdot():
    import re
    from xcapp.models import Goal
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam", vdot_override=69)
    db.add(a)
    db.flush()
    db.query(TrainingGroup).filter_by(name="Gold").one().weekly_miles = 42
    db.add(Goal(athlete_id=a.id, event="5K", season=A.season_for(A.team_today()), goal_s=15 * 60))
    aid = a.id
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()
    assert TestClient(app).get("/a/t-sam/series.json").json()["weekly_miles_goal"] == 42
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert c.get(f"/api/athletes/{aid}/series.json").json()["weekly_miles_goal"] == 42
    page = c.get(f"/athletes/{aid}").text
    assert re.search(r"<b>Goal VDOT \d+</b>", page) and f'href="/results?athlete={aid}#set-goal"' in page
    assert f'<option value="{aid}" selected>' in c.get(f"/results?athlete={aid}").text
    db = SessionLocal()
    from xcapp.models import AppSetting
    db.merge(AppSetting(key="show_group_to_athletes", value="0"))
    db.commit()
    db.close()
    assert TestClient(app).get("/a/t-sam/series.json").json()["weekly_miles_goal"] is None   # the goal names their group

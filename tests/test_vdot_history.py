"""Coach-set VDOT history: charted, green arrows for a week after a gain, mistakes removed; achievement badges."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, RaceResult, StaffUser, VdotChange
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c


def sam():
    db = SessionLocal()
    a = db.query(Athlete).one()
    out = a.id, a.vdot_override
    db.close()
    return out


def test_history_arrows_and_removing_mistakes(app):
    c = login(app)
    aid, _ = sam()
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/athletes/{aid}").text).group(1)
    db = SessionLocal()                                   # a VDOT of 60 set three weeks ago
    db.add(VdotChange(athlete_id=aid, date=A.team_today() - dt.timedelta(days=21), vdot=60, previous=None, by="coach1", source="VDOT page"))
    db.get(Athlete, aid).vdot_override = 60
    db.commit()
    db.close()
    assert 'class="up-arrows"' not in c.get("/vdot").text                                  # no gain this week
    c.post(f"/athletes/{aid}/vdot", data={"csrf": csrf, "vdot": "62"})
    vd = c.get("/vdot").text
    assert vd.count('aria-label="VDOT up 2 this week"') == 2                                # the row and the changes list
    row = vd.split("Sam Lowry</a></td>")[1].split("</tr>")[0]
    assert row.count("<svg") == 2                                                             # two arrows for two points
    assert "VDOT changes in the last 30 days (2)" in vd
    page = c.get(f"/athletes/{aid}").text
    assert 'id="vdot-history"' in page and "up 2 this week" in page and "<path d=" in page and "VDOT up 2" in page
    assert "VDOT up 2" in TestClient(app).get("/a/t-sam").text                              # the badge on the athlete's page
    c.post("/vdot/import", data={"csrf": csrf, "text": "Sam Lowry\t64"})
    db = SessionLocal()
    assert [(x.previous, x.vdot, x.source) for x in db.query(VdotChange).order_by(VdotChange.at)] == [
        (None, 60, "VDOT page"), (60, 62, "athlete page"), (62, 64, "pasted list")]
    assert A.vdot_gains(db, A.team_today()) == {aid: 4}
    assert A.vdot_gains(db, A.team_today() + dt.timedelta(days=8)) == {}                      # arrows go after a week
    latest = db.query(VdotChange).order_by(VdotChange.at.desc()).first().id
    middle = db.query(VdotChange).filter_by(vdot=62).one().id
    db.close()
    c.post(f"/athletes/{aid}/vdot-history/{latest}/delete", data={"csrf": csrf})            # undo the typo
    assert "Removed. Sam Lowry&#39;s VDOT is back to 62." in c.get(f"/athletes/{aid}").text and sam()[1] == 62
    c.post(f"/athletes/{aid}/vdot-history/{middle}/delete", data={"csrf": csrf, "next": "/vdot"})
    db = SessionLocal()
    assert sam()[1] == 60 and [(x.previous, x.vdot) for x in db.query(VdotChange)] == [(None, 60)]
    db.close()


def test_badges(app):
    aid, _ = sam()
    today = A.team_today()
    db = SessionLocal()
    db.add(RaceResult(athlete_id=aid, date=today - dt.timedelta(days=30), meet="Hilltop", race="Boys", distance_m=5000,
                      distance_label="5K", time_s=16 * 60))
    db.add(RaceResult(athlete_id=aid, date=today - dt.timedelta(days=3), meet="Lakeside", race="Boys", distance_m=5000,
                      distance_label="5K", time_s=15 * 60 + 41.5))
    for w in range(5, -1, -1):                                       # 30 miles in each rolling week, then 36 in the past 7 days
        for k in range(3):
            d = today - dt.timedelta(days=7 * w + 2 * k)
            miles = 12 if w == 0 else 10
            db.add(Activity(athlete_id=aid, source="upload", external_id=f"w{w}{k}", start_utc=dt.datetime(d.year, d.month, d.day, 15),
                            local_date=d, duration_s=miles * 420, distance_m=miles * 1609.344, sport="running"))
    db.commit()
    wins = {b["title"]: b["detail"] for b in A.achievements(db, db.get(Athlete, aid), today)}
    db.close()
    assert wins["New PR"] == "15:41.5 for 5K at Lakeside, 18.5 s faster than before."
    assert "Season-high week" in wins
    page = TestClient(app).get("/a/t-sam").text
    assert 'class="badge badge-pr"' in page and "New PR" in page

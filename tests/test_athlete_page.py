"""The athlete's own page: running-group button beside the name, and where the Garmin section sits."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Athlete, GarminConnection, TrainingGroup


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def test_group_button_and_explanation(app):
    c = TestClient(app)
    page = c.get("/a/tok-ava").text
    assert '<summary class="group-btn">Running group: not set</summary>' in page
    assert "They appear here once your coach records one." in page
    assert "Running group: <b>" not in page and 'id="group"' not in page          # no separate group section any more

    db = SessionLocal()
    a = db.query(Athlete).one()
    a.ability_override = db.query(TrainingGroup).filter_by(name="Gold").one().name
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert '<summary class="group-btn" style="background:#D4AF37; color:#1B1B1F">Gold</summary>' in page
    assert "<b>Running group: Gold</b>" in page and "Your coach placed you in this group." in page   # in the drop-down card

    db = SessionLocal()
    A.set_setting(db, "show_group_to_athletes", "0")
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert "group-btn" not in page and "group-card" not in page


def test_garmin_section_moves_down_once_connected(app):
    c = TestClient(app)
    page = c.get("/a/tok-ava").text
    assert page.index('id="wp-h"') < page.index('id="g-h"') < page.index('id="ac-h"') < page.index('id="pv-h"')  # workouts, then Garmin

    db = SessionLocal()
    a = db.query(Athlete).one()
    db.add(GarminConnection(athlete_id=a.id, garmin_user_id="g1", status="connected", connected_at=dt.datetime(2026, 10, 26)))
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert page.count('id="g-h"') == 1 and "✓ Connected" in page
    assert '<details class="panel fold" id="add-runs">' in page and page.index('id="ac-h"') < page.index('id="u-h"') < page.index('id="g-h"') < page.index('id="pv-h"')


def test_group_colors_look_like_their_names(app):
    from xcapp.db import create_all
    db = SessionLocal()
    groups = {g.name: g for g in db.query(TrainingGroup)}
    groups["Emerald"].color, groups["Ivory"].color, groups["Gold"].color = "#B6D7A8", "#ffffff", "#123456"  # old sheet, old sheet, coach's own
    db.commit()
    db.close()
    create_all()
    db = SessionLocal()
    colors = {g.name: g.color for g in db.query(TrainingGroup)}
    assert (colors["Emerald"], colors["Ivory"], colors["Gold"]) == ("#50C878", "#FFFFF0", "#123456")
    a = db.query(Athlete).one()
    a.ability_override = "Emerald"
    db.commit()
    db.close()
    page = TestClient(app).get("/a/tok-ava").text
    assert '<summary class="group-btn" style="background:#50C878; color:#1B1B1F">Emerald</summary>' in page


def test_garmin_notification_address_is_on_the_sync_log_for_admins_only(app):
    import re
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    for n, r in (("admin1", "admin"), ("coach1", "coach")):
        db.add(StaffUser(username=n, role=r, password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    for user, shown in (("admin1", True), ("coach1", False)):
        c = TestClient(app, follow_redirects=False)
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
        assert ("/garmin/webhook/hooksecret" in c.get("/sync").text) is shown
        assert "/garmin/webhook/" not in c.get("/").text


def test_pain_section_starts_collapsed(app):
    page = TestClient(app).get("/a/tok-ava").text
    assert '<details class="panel fold" id="pain">' in page and '<details class="panel fold" id="pain" open' not in page
    assert '<summary><h2 id="p-h">Something hurt today?</h2></summary>' in page and 'action="/a/tok-ava/pain"' in page


def test_athlete_page_layout(app_client=None):
    """Workouts then meets right under the announcements, both above the weekly numbers; resting heart rate folds once
    today's value is in; no benchmark times in My training; Garmin folds once connected."""
    import datetime as dt
    from fastapi.testclient import TestClient
    from xcapp import analysis as A
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, RestingHeartRate
    from xcapp.web import create_app
    db = SessionLocal()
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-lay")
    db.add(a)
    db.flush()
    db.add(RestingHeartRate(athlete_id=a.id, date=A.team_today() - dt.timedelta(days=1), bpm=48, source="athlete"))
    db.commit()
    aid = a.id
    db.close()
    c = TestClient(create_app())
    page = c.get("/a/t-lay").text
    assert page.index('id="wp-h"') < page.index('id="rhr-h"') < page.index('id="ac-h"')
    assert '<details class="panel fold" id="workouts" open>' in page and '<details class="panel fold" id="rhr" open>' in page
    assert "Enter today’s ›" in page and '<span class="k">This week</span>' not in page
    assert '<option value="results">' not in page and "<section class=\"panel\" aria-labelledby=\"g-h\">" in page
    db = SessionLocal()
    db.add(RestingHeartRate(athlete_id=aid, date=A.team_today(), bpm=47, source="athlete"))
    db.commit()
    db.close()
    page = c.get("/a/t-lay").text
    assert '<details class="panel fold" id="rhr">' in page and "Today 47 ✓" in page and "Enter today’s ›" not in page
    assert page.index('id="ac-h"') < page.index('id="rhr-h"')


def test_privacy_section_folds_once_ai_is_on():
    import datetime as dt
    from fastapi.testclient import TestClient
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-pv", email="ava@example.org"))
    db.commit()
    db.close()
    c = TestClient(create_app())
    page = c.get("/a/t-pv").text
    assert '<details class="panel fold garmin-fold" id="privacy" open>' in page and '<details class="sub-fold">' in page
    assert "AI analysis ✓ On" not in page
    db = SessionLocal()
    db.query(Athlete).filter_by(link_token="t-pv").one().ai_consent_at = dt.datetime(2026, 10, 1)
    db.commit()
    db.close()
    page = c.get("/a/t-pv").text
    assert '<details class="panel fold garmin-fold" id="privacy">' in page and "AI analysis ✓ On" in page


def test_compact_header_without_the_intro_sentence():
    from fastapi.testclient import TestClient
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-hd"))
    db.commit()
    db.close()
    page = TestClient(create_app()).get("/a/t-hd").text
    assert '<header class="hero compact">' in page and "This is your personal page" not in page
    assert "This page’s link is your sign-in: keep it to yourself." in page


def test_effort_reminder_is_at_the_top_and_assets_are_versioned():
    import datetime as dt
    from fastapi.testclient import TestClient
    from xcapp import analysis as A
    from xcapp.db import SessionLocal
    from xcapp.models import Activity, Announcement, Athlete
    from xcapp.web import create_app
    db = SessionLocal()
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ef")
    db.add(a)
    db.flush()
    d = A.team_today()
    db.add(Activity(athlete_id=a.id, source="upload", external_id="e1", start_utc=dt.datetime(d.year, d.month, d.day, 15), local_date=d,
                    duration_s=1800, distance_m=6000, sport="running"))
    db.add(Announcement(title="Team dinner", start_date=d, end_date=d))
    db.commit()
    db.close()
    page = TestClient(create_app()).get("/a/t-ef").text
    assert page.index("Add effort ›") < page.index("Team dinner") < page.index('id="wp-h"')
    assert '/static/app.css?v=' in page and '/static/athlete-charts.js?v=' in page

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
    assert page.index('id="wp-h"') < page.index('id="g-h"') < page.index("My training") < page.index('id="pv-h"')  # workouts, then Garmin

    db = SessionLocal()
    a = db.query(Athlete).one()
    db.add(GarminConnection(athlete_id=a.id, garmin_user_id="g1", status="connected", connected_at=dt.datetime(2026, 10, 26)))
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert page.count('id="g-h"') == 1 and "✓ Connected" in page
    assert page.index("My training") < page.index('id="u-h"') < page.index('id="g-h"') < page.index('id="pv-h"')


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

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
    assert '<a class="group-btn" href="#group">Running group: not set</a>' in page
    assert 'id="group"' in page and "They appear here once your coach records one." in page

    db = SessionLocal()
    a = db.query(Athlete).one()
    a.ability_override = db.query(TrainingGroup).filter_by(name="Gold").one().name
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert '<a class="group-btn" href="#group">Gold</a>' in page and "Your coach placed you in this group." in page

    db = SessionLocal()
    A.set_setting(db, "show_group_to_athletes", "0")
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert "group-btn" not in page and 'id="group"' not in page


def test_garmin_section_moves_down_once_connected(app):
    c = TestClient(app)
    page = c.get("/a/tok-ava").text
    assert page.index('id="g-h"') < page.index("My training") < page.index('id="pv-h"')   # top: connect night

    db = SessionLocal()
    a = db.query(Athlete).one()
    db.add(GarminConnection(athlete_id=a.id, garmin_user_id="g1", status="connected", connected_at=dt.datetime(2026, 10, 26)))
    db.commit()
    db.close()
    page = c.get("/a/tok-ava").text
    assert page.count('id="g-h"') == 1 and "✓ Connected" in page
    assert page.index("My training") < page.index('id="u-h"') < page.index('id="g-h"') < page.index('id="pv-h"')

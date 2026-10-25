import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "s" * 48)
    monkeypatch.setenv("TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("GARMIN_CLIENT_ID", "cid-123")
    monkeypatch.setenv("GARMIN_CLIENT_SECRET", "csecret-456")
    monkeypatch.setenv("GARMIN_WEBHOOK_SECRET", "hooksecret")
    monkeypatch.setenv("BASE_URL", "https://xc.test")
    monkeypatch.setenv("TEAM_TIMEZONE", "America/Los_Angeles")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "AS_OF_DATE", "GARMIN_FILE_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    from xcapp import config, db
    config.reload_settings()
    db.init_engine(os.environ["DATABASE_URL"])
    db.create_all()
    yield


@pytest.fixture
def session():
    from xcapp.db import SessionLocal
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def athlete(session):
    from xcapp.models import Athlete
    a = Athlete(first_name="Lucia", last_name="Sorensen", gender="Girls", grade=11, group="Varsity", link_token="tok-lucia")
    session.add(a)
    session.flush()
    return a

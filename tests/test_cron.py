"""The nightly job over HTTP, for hosting platforms whose timers call the app (POST /cron/nightly)."""
import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from xcapp import cli, config
from xcapp.db import SessionLocal
from xcapp.models import SyncLog

SECRET = "c" * 43
AUTH = {"Authorization": f"Bearer {SECRET}"}


def client(monkeypatch, secret=SECRET):
    if secret is None:
        monkeypatch.delenv("CRON_SECRET", raising=False)
    else:
        monkeypatch.setenv("CRON_SECRET", secret)
    config.reload_settings()
    from xcapp.web import create_app
    return TestClient(create_app(), follow_redirects=False)


def test_hidden_without_the_secret(monkeypatch):
    c = client(monkeypatch, secret=None)
    assert c.post("/cron/nightly", headers={"Authorization": "Bearer "}).status_code == 404
    c = client(monkeypatch)
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": SECRET}, {"Authorization": "Bearer café".encode()}):
        r = c.post("/cron/nightly", headers=headers)
        assert r.status_code == 404 and r.json() == {"error": "Not found."}
    assert c.get("/cron/nightly", headers=AUTH).status_code == 405


def test_runs_only_at_the_nightly_hour(monkeypatch):
    c = client(monkeypatch)
    monkeypatch.setattr(cli, "is_nightly_hour", lambda now=None: False)
    r = c.post("/cron/nightly", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"skipped": "not 4:00 in America/Los_Angeles"}
    db = SessionLocal()
    assert not db.scalars(select(SyncLog).where(SyncLog.kind == "nightly")).all()
    db.close()

    r = c.post("/cron/nightly?force=true", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ran": {"tokens_refreshed": 0, "briefing": "skipped"}}

    monkeypatch.setattr(cli, "is_nightly_hour", lambda now=None: True)
    assert c.post("/cron/nightly", headers=AUTH).json()["ran"]["briefing"] == "skipped"
    db = SessionLocal()
    assert len(db.scalars(select(SyncLog).where(SyncLog.kind == "nightly")).all()) == 2
    db.close()


def test_one_run_at_a_time(monkeypatch):
    c = client(monkeypatch)
    started, release = threading.Event(), threading.Event()

    def slow_nightly():
        started.set()
        release.wait(10)
        return {"tokens_refreshed": 0, "briefing": "skipped"}

    monkeypatch.setattr(cli, "nightly", slow_nightly)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=c.post("/cron/nightly?force=true", headers=AUTH)))
    t.start()
    assert started.wait(10)
    second = c.post("/cron/nightly?force=true", headers=AUTH)
    assert second.status_code == 409 and second.json() == {"error": "The nightly job is already running."}
    release.set()
    t.join(10)
    assert first["r"].status_code == 200
    assert c.post("/cron/nightly?force=true", headers=AUTH).status_code == 200  # the lock was released


def test_nightly_hour_follows_daylight_saving():
    utc = dt.timezone.utc
    assert cli.is_nightly_hour(dt.datetime(2026, 7, 1, 11, 0, 5, tzinfo=utc))       # 4 a.m. PDT
    assert not cli.is_nightly_hour(dt.datetime(2026, 7, 1, 12, 0, 5, tzinfo=utc))   # 5 a.m. PDT
    assert cli.is_nightly_hour(dt.datetime(2026, 1, 15, 12, 0, 5, tzinfo=utc))      # 4 a.m. PST
    assert not cli.is_nightly_hour(dt.datetime(2026, 1, 15, 11, 0, 5, tzinfo=utc))  # 3 a.m. PST


def test_secret_setting_and_generation(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "too-short")
    assert any(p.startswith("CRON_SECRET") for p in config.reload_settings().problems())
    monkeypatch.setenv("CRON_SECRET", SECRET)
    assert not config.reload_settings().problems()
    cli.main(["gen-keys"])
    line = [x for x in capsys.readouterr().out.splitlines() if x.startswith("CRON_SECRET=")][0]
    assert len(line.split("=", 1)[1]) >= 32

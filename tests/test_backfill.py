"""Past runs from Garmin (Summary Backfill): queued on connect and by coaches, sent at most once a minute, retried."""
import datetime as dt
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_garmin import MockGarmin, connect
from xcapp import config
from xcapp import garmin as G
from xcapp.models import Athlete, GarminBackfill, StaffUser, SyncLog
from xcapp.security import hash_password

T0 = dt.datetime(2026, 10, 26, 7, 0)


class Clock:
    """Stands in for the wall clock: sleeping just moves time forward."""
    def __init__(self, monkeypatch):
        self.t, self.slept = T0, []
        monkeypatch.setattr(G, "_now", lambda: self.t)

    def sleep(self, seconds):
        self.slept.append(round(seconds))
        self.t += dt.timedelta(seconds=seconds)


class Backfilling(MockGarmin):
    """Mock Garmin whose backfill answers come from a script: one (status, body) per request, then 202."""
    def __init__(self, answers=()):
        super().__init__()
        self.answers = list(answers)

    def __call__(self, req):
        if "/backfill/" in req.url.path:
            self.requests.append(req)
            status, body = self.answers.pop(0) if self.answers else (202, None)
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        return super().__call__(req)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def backfills(mock):
    return [(r.url.path.rsplit("/", 1)[-1], int(r.url.params[G.BACKFILL_START]), int(r.url.params[G.BACKFILL_END]))
            for r in mock.requests if "/backfill/" in r.url.path]


def messages(session):
    return [x.message for x in session.scalars(select(SyncLog).where(SyncLog.kind == "garmin").order_by(SyncLog.id))]


def jobs(session):
    return session.scalars(select(GarminBackfill).order_by(GarminBackfill.id)).all()


def utc_s(t):
    return int(t.replace(tzinfo=dt.timezone.utc).timestamp())


def test_connecting_queues_the_past_90_days_and_sends_them_paced(session, athlete, clock):
    mock = Backfilling()
    http = httpx.Client(transport=httpx.MockTransport(mock))
    connect(session, athlete, http)
    assert backfills(mock) == []                                        # nothing sent while Garmin is still registering
    assert [(j.kind, j.status, j.not_before) for j in jobs(session)] == [
        ("activities", "waiting", T0 + dt.timedelta(minutes=2)), ("activityDetails", "waiting", T0 + dt.timedelta(minutes=2))]
    assert "Queued a request to Garmin for Lucia Sorensen's runs from the past 90 days." in " ".join(messages(session))

    assert G.process_backfills(session, http, sleep=clock.sleep) == 2
    assert clock.slept == [120, 65]                                     # wait for registration, then one a minute
    assert backfills(mock) == [("activities", utc_s(T0 - dt.timedelta(days=90)), utc_s(T0)),
                               ("activityDetails", utc_s(T0 - dt.timedelta(days=90)), utc_s(T0))]
    assert all(r.headers["authorization"] == "Bearer AT1" for r in mock.requests if "/backfill/" in r.url.path)
    assert [j.status for j in jobs(session)] == ["sent", "sent"]
    log = " | ".join(messages(session))
    assert "Garmin accepted the request for Lucia Sorensen's activities Jul 28-Oct 26." in log
    assert "Lucia Sorensen's Garmin permissions: ACTIVITY_EXPORT,WORKOUT_IMPORT." not in log  # already known at connect


def test_not_registered_and_too_many_requests_are_retried(session, athlete, clock):
    mock = Backfilling([(403, {"errorMessage": "User not registered with consumer"}),
                        (429, {"errorMessage": "Too many request: Limit 100 per 1 minute"})])
    http = httpx.Client(transport=httpx.MockTransport(mock))
    connect(session, athlete, http)
    assert G.process_backfills(session, http, sleep=clock.sleep) == 4
    assert [k for k, _, _ in backfills(mock)] == ["activities", "activityDetails", "activities", "activityDetails"]
    assert [(j.status, j.attempts) for j in jobs(session)] == [("sent", 2), ("sent", 2)]
    assert clock.t - T0 >= dt.timedelta(minutes=7)                     # 2 minutes, then retried 5 minutes later
    log = " | ".join(messages(session))
    assert ("Garmin was not ready for Lucia Sorensen's activities Jul 28-Oct 26 (403. Garmin said: "
            '{"errorMessage":"User not registered with consumer"}); trying again in a few minutes.') in log
    assert "429. Garmin said:" in log


def test_gives_up_after_six_tries(session, athlete, clock):
    mock = Backfilling([(412, {"errorMessage": "nope"})] * 12)
    http = httpx.Client(transport=httpx.MockTransport(mock))
    connect(session, athlete, http)
    G.process_backfills(session, http, sleep=clock.sleep, budget_s=3600)
    assert [(j.status, j.attempts) for j in jobs(session)] == [("failed", 6), ("failed", 6)]
    errors = [x.message for x in session.scalars(select(SyncLog).where(SyncLog.level == "error"))]
    assert any(m.startswith("Gave up asking Garmin for Lucia Sorensen's activities") and "after 6 tries" in m for m in errors)
    assert G.process_backfills(session, http, sleep=clock.sleep) == 0


def test_long_ranges_split_already_requested_is_fine_and_budget_is_kept(session, athlete, clock, monkeypatch):
    monkeypatch.setenv("GARMIN_BACKFILL_DAYS", "0")                    # no automatic request
    config.reload_settings()
    mock = Backfilling([(409, None)])
    http = httpx.Client(transport=httpx.MockTransport(mock))
    connect(session, athlete, http)
    assert jobs(session) == []
    G.queue_backfill(session, athlete.garmin, 200)
    spans = [(j.kind, j.start_at, j.end_at) for j in jobs(session)]
    assert len(spans) == 6                                              # 90 + 90 + 20 days, for each kind
    acts = sorted((s, e) for k, s, e in spans if k == "activities")
    assert all(e - s <= dt.timedelta(days=90) for s, e in acts) and acts[-1][1] - acts[0][0] == dt.timedelta(days=200)
    assert all(a[1] == b[0] for a, b in zip(acts, acts[1:]))           # no gaps, no overlaps
    assert G.process_backfills(session, http, sleep=clock.sleep, budget_s=150) == 3  # sent at 0, 65 and 130 s; 195 s is past the budget
    assert jobs(session)[0].status == "sent" and "(already requested)." in " ".join(messages(session))


def test_permissions_are_read_again_and_disconnected_athletes_are_skipped(session, athlete, clock):
    class Unregistered(Backfilling):
        def __call__(self, req):
            if req.url.path == "/wellness-api/rest/user/permissions" and not self.answers_given:
                self.answers_given = True
                return httpx.Response(403, json={"errorType": "partner_registration_not_found"})
            return super().__call__(req)
        answers_given = False

    mock = Unregistered()
    http = httpx.Client(transport=httpx.MockTransport(mock))
    connect(session, athlete, http)
    log = " | ".join(messages(session))
    assert "Garmin said: {\"errorType\":\"partner_registration_not_found\"}" in log and athlete.garmin.permissions == ""
    G.process_backfills(session, http, sleep=clock.sleep)
    assert athlete.garmin.permissions == "ACTIVITY_EXPORT,WORKOUT_IMPORT"
    assert "Lucia Sorensen's Garmin permissions: ACTIVITY_EXPORT,WORKOUT_IMPORT." in " ".join(messages(session))

    G.queue_backfill(session, athlete.garmin, 10)
    athlete.garmin.status = "revoked"
    sent_before = len(backfills(mock))
    G.process_backfills(session, http, sleep=clock.sleep)
    assert len(backfills(mock)) == sent_before
    assert [j.last_result for j in jobs(session)[-2:]] == ["not connected to Garmin"] * 2


def test_coach_button(session, monkeypatch, clock):
    mock = Backfilling()
    monkeypatch.setattr(G, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(mock)))
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        session.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    ava = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava")
    mat = Athlete(first_name="Mateo", last_name="Garza", gender="Boys", grade=10, group="JV", link_token="tok-mat")
    session.add_all([ava, mat])
    session.flush()
    connect(session, ava, httpx.Client(transport=httpx.MockTransport(mock)))
    session.query(GarminBackfill).delete()
    session.commit()
    from xcapp.web import create_app
    app = create_app()

    def login(user):
        c = TestClient(app, follow_redirects=False)
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
        c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
        return c

    c = login("coach1")
    page = c.get(f"/athletes/{ava.id}").text
    assert "Load past runs" in page and 'value="90"' in page
    assert "Load past runs" not in c.get(f"/athletes/{mat.id}").text  # not connected
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post(f"/athletes/{ava.id}/garmin-backfill", data={"csrf": csrf, "days": "30"})
    assert r.status_code == 303 and backfills(mock) == []              # queued, not sent while the coach waits
    assert "Queued a request to Garmin for Ava Doyle&#39;s runs from the past 30 days." in c.get(f"/athletes/{ava.id}").text
    session.expire_all()
    assert [(j.kind, j.end_at - j.start_at) for j in jobs(session)] == [("activities", dt.timedelta(days=30)),
                                                                        ("activityDetails", dt.timedelta(days=30))]
    c.post(f"/athletes/{mat.id}/garmin-backfill", data={"csrf": csrf})
    assert "Mateo Garza isn&#39;t connected to Garmin." in c.get(f"/athletes/{mat.id}").text

    t = login("trainer1")
    page = t.get(f"/athletes/{ava.id}").text
    assert "Load past runs" not in page
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert t.post(f"/athletes/{ava.id}/garmin-backfill", data={"csrf": tcsrf}).status_code == 403


def test_athletes_and_parents_are_told(session, monkeypatch):
    monkeypatch.setenv("GARMIN_CLIENT_ID", "cid-123")
    config.reload_settings()
    session.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava"))
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app())
    assert "including my runs from the past 90 days" in c.get("/a/tok-ava").text
    policy = c.get("/privacy").text
    assert "their activities from the previous 90 days" in policy and "the app discards them as the data arrives" in policy

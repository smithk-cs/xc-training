"""Past runs from Garmin (Summary Backfill): requested on connect and by coaches, delivered through the webhook."""
import datetime as dt
import re

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_garmin import MockGarmin, connect
from xcapp import config
from xcapp import garmin as G
from xcapp.models import Athlete, StaffUser, SyncLog
from xcapp.security import hash_password


def backfills(mock):
    out = []
    for r in mock.requests:
        if "/backfill/" in r.url.path:
            q = dict(r.url.params)
            out.append((r.url.path.rsplit("/", 1)[-1], int(q["summaryStartTimeInSeconds"]), int(q["summaryEndTimeInSeconds"])))
    return out


def messages(session):
    return [x.message for x in session.scalars(select(SyncLog).where(SyncLog.kind == "garmin").order_by(SyncLog.id))]


def test_connecting_asks_for_the_past_90_days(session, athlete):
    mock = MockGarmin()
    connect(session, athlete, httpx.Client(transport=httpx.MockTransport(mock)))
    got = backfills(mock)
    assert [k for k, _, _ in got] == ["activities", "activityDetails"]
    for _, start, end in got:
        assert end - start == 90 * 86400 and abs(end - dt.datetime.now(dt.timezone.utc).timestamp()) < 60
        assert all(r.headers["authorization"] == "Bearer AT1" for r in mock.requests if "/backfill/" in r.url.path)
    assert "Asked Garmin for Lucia Sorensen's runs from the past 90 days." in " ".join(messages(session))


def test_long_ranges_are_split_and_repeats_are_fine(session, athlete, monkeypatch):
    monkeypatch.setenv("GARMIN_BACKFILL_DAYS", "0")  # no automatic request
    config.reload_settings()
    mock = MockGarmin()
    http = httpx.Client(transport=httpx.MockTransport(mock))
    connect(session, athlete, http)
    assert backfills(mock) == []
    G.request_backfill(session, athlete.garmin, http, 200)
    got = backfills(mock)
    assert len(got) == 6                                              # 90 + 90 + 20 days, for each kind
    acts = sorted((s, e) for k, s, e in got if k == "activities")
    assert all(e - s <= 90 * 86400 for s, e in acts) and acts[-1][1] - acts[0][0] == 200 * 86400
    assert all(a[1] == b[0] for a, b in zip(acts, acts[1:]))         # no gaps, no overlaps
    mock.backfill_status = 409                                        # Garmin: already requested
    msg = G.request_backfill(session, athlete.garmin, http, 30)
    assert msg.startswith("Asked Garmin") and "some were already requested" in msg


def test_a_refusal_is_logged_and_connecting_still_works(session, athlete):
    mock = MockGarmin()
    mock.backfill_status = 403
    connect(session, athlete, httpx.Client(transport=httpx.MockTransport(mock)))
    assert athlete.garmin.status == "connected"
    warn = [m for m in messages(session) if m.startswith("Garmin did not accept")]
    assert warn and "activities" in warn[0] and ": 403" in warn[0]


def test_coach_button(session, monkeypatch):
    mock = MockGarmin()
    monkeypatch.setattr(G, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(mock)))
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        session.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    ava = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava")
    mat = Athlete(first_name="Mateo", last_name="Garza", gender="Boys", grade=10, group="JV", link_token="tok-mat")
    session.add_all([ava, mat])
    session.flush()
    connect(session, ava, httpx.Client(transport=httpx.MockTransport(mock)))
    session.commit()
    mock.requests.clear()
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
    assert r.status_code == 303
    assert "Asked Garmin for Ava Doyle&#39;s runs from the past 30 days" in c.get(f"/athletes/{ava.id}").text
    assert [k for k, _, _ in backfills(mock)] == ["activities", "activityDetails"]
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


def test_garmins_reason_is_kept_in_the_sync_log(session, athlete):
    """A bare 412 told us nothing; Garmin's reply text says which precondition is missing."""
    class Explaining(MockGarmin):
        def __call__(self, req):
            self.requests.append(req)
            if req.url.path == "/wellness-api/rest/user/permissions":
                return httpx.Response(404, text="<html>\n  Not   Found\n</html>")
            if "/backfill/" in req.url.path:
                return httpx.Response(412, json={"errorMessage": "User has not granted HISTORICAL_DATA_EXPORT"})
            self.requests.pop()
            return super().__call__(req)

    connect(session, athlete, httpx.Client(transport=httpx.MockTransport(Explaining())))
    log = " | ".join(messages(session))
    assert "Could not read Lucia Sorensen's Garmin permissions: Garmin request /wellness-api/rest/user/permissions failed (404). Garmin said: <html> Not Found </html>" in log
    assert 'activities' in log and '412. Garmin said: {"errorMessage":"User has not granted HISTORICAL_DATA_EXPORT"}' in log
    assert athlete.garmin.status == "connected"

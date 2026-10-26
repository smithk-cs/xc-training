"""Garmin OAuth 2.0 PKCE (per Garmin's published spec) and notification handling, against a mock Garmin."""
import base64
import datetime as dt
import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from fitgen import rep_workout
from xcapp import garmin as G
from xcapp.models import Activity, GarminConnection, OAuthState, SyncLog
from xcapp.security import decrypt

START = dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc)
FIT = rep_workout(START, [(1000, 200, 185, 30), (1000, 200, 187, 25), (1000, 200, 189, 0)])


class MockGarmin:
    def __init__(self):
        self.requests = []
        self.n = 0

    def __call__(self, req: httpx.Request):
        self.requests.append(req)
        u = req.url
        if u.host == "diauth.garmin.com" and u.path == "/di-oauth2-service/oauth/token":
            self.n += 1
            return httpx.Response(200, json={"access_token": f"AT{self.n}", "refresh_token": f"RT{self.n}", "expires_in": 86400,
                                             "refresh_token_expires_in": 7775998, "token_type": "bearer"})
        if u.host == "apis.garmin.com":
            if req.headers.get("authorization", "").split(" ")[-1] not in {f"AT{i}" for i in range(1, self.n + 1)}:
                return httpx.Response(401)
            if u.path == "/wellness-api/rest/user/id":
                return httpx.Response(200, json={"userId": "garmin-user-1"})
            if u.path == "/wellness-api/rest/user/permissions":
                return httpx.Response(200, json=["ACTIVITY_EXPORT", "WORKOUT_IMPORT"])
            if u.path == "/files/activity/1":
                return httpx.Response(200, content=FIT)
            if u.path == "/wellness-api/rest/user/registration" and req.method == "DELETE":
                return httpx.Response(204)
        return httpx.Response(404)

    def form(self, i):
        return {k: v[0] for k, v in parse_qs(self.requests[i].content.decode()).items()}


@pytest.fixture
def mock():
    return MockGarmin()


@pytest.fixture
def http(mock):
    return httpx.Client(transport=httpx.MockTransport(mock))


def connect(session, athlete, http):
    url = G.start_authorization(session, athlete, "consent ok")
    state = parse_qs(urlparse(url).query)["state"][0]
    return url, G.complete_authorization(session, "the-code", state, http)


def test_pkce_matches_spec():
    v, c = G.pkce_pair()
    assert 43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9\-._~]+", v)
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_authorization_flow(session, athlete, http, mock):
    url, a = connect(session, athlete, http)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://connect.garmin.com/oauth2Confirm?")
    assert q["response_type"] == "code" and q["client_id"] == "cid-123" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://xc.test/garmin/callback"
    form = mock.form(0)
    assert form["grant_type"] == "authorization_code" and form["client_secret"] == "csecret-456" and form["code"] == "the-code"
    assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert mock.requests[1].url.path == "/wellness-api/rest/user/id" and mock.requests[1].headers["authorization"] == "Bearer AT1"
    conn = a.garmin
    assert conn.garmin_user_id == "garmin-user-1" and conn.permissions == "ACTIVITY_EXPORT,WORKOUT_IMPORT"
    assert conn.access_token_enc != "AT1" and decrypt(conn.access_token_enc) == "AT1"   # encrypted at rest
    assert session.get(OAuthState, q["state"]) is None                                   # one-time use
    with pytest.raises(G.GarminError):
        G.complete_authorization(session, "the-code", q["state"], http)


def test_expired_state_and_account_reuse(session, athlete, http):
    from xcapp.models import Athlete
    url = G.start_authorization(session, athlete, "")
    state = parse_qs(urlparse(url).query)["state"][0]
    session.get(OAuthState, state).created_at -= dt.timedelta(minutes=30)
    with pytest.raises(G.GarminError, match="expired"):
        G.complete_authorization(session, "c", state, http)
    connect(session, athlete, http)
    other = Athlete(first_name="Other", last_name="Kid", gender="Boys", grade=9, group="JV", link_token="tok-o")
    session.add(other)
    session.flush()
    with pytest.raises(G.GarminError, match="already linked"):
        connect(session, other, http)


def test_token_refresh(session, athlete, http, mock):
    _, a = connect(session, athlete, http)
    a.garmin.access_expires_at = G._now() + dt.timedelta(seconds=300)  # inside the 600 s margin
    assert G.access_token(session, a.garmin, http) == "AT2"
    form = mock.form(len(mock.requests) - 1)
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "RT1" and form["client_secret"] == "csecret-456"
    assert decrypt(a.garmin.refresh_token_enc) == "RT2"


def test_notifications(session, athlete, http):
    connect(session, athlete, http)
    file_note = {"activityFiles": [{"userId": "garmin-user-1", "summaryId": "1", "fileType": "FIT",
                                    "callbackURL": "https://apis.garmin.com/files/activity/1"}]}
    assert G.handle_notification(session, file_note, http)["created"] == 1
    assert G.handle_notification(session, file_note, http)["duplicate"] == 1
    act = session.query(Activity).one()
    assert act.source == "garmin_file" and sum(l.is_rep for l in act.laps) == 3

    evil = {"activityFiles": [{"userId": "garmin-user-1", "fileType": "FIT", "callbackURL": "https://evil.example/steal"}]}
    assert G.handle_notification(session, evil, http)["errors"] == 1
    assert "not Garmin" in session.query(SyncLog).order_by(SyncLog.id.desc()).first().message

    stranger = {"activityFiles": [{"userId": "nobody", "fileType": "FIT", "callbackURL": "https://apis.garmin.com/files/activity/1"}]}
    assert G.handle_notification(session, stranger, http)["skipped"] == 1

    push = {"activities": [
        {"userId": "garmin-user-1", "summaryId": "9", "activityId": 9, "activityType": "RUNNING", "startTimeInSeconds": 1792000000,
         "durationInSeconds": 1800, "distanceInMeters": 6000, "averageHeartRateInBeatsPerMinute": 140},
        {"userId": "garmin-user-1", "summaryId": "10", "activityType": "CYCLING", "startTimeInSeconds": 1792100000,
         "durationInSeconds": 3600, "distanceInMeters": 30000}]}
    c = G.handle_notification(session, push, http)
    assert (c["created"], c["skipped"]) == (1, 1)

    t0 = 1792200000
    samples = [{"startTimeInSeconds": t0 + s, "heartRate": 150, "totalDistanceInMeters": 3.0 * s} for s in range(0, 900)]
    details = {"activityDetails": [{"userId": "garmin-user-1", "summaryId": "11",
                                    "summary": {"activityType": "RUNNING", "startTimeInSeconds": t0, "durationInSeconds": 900, "distanceInMeters": 2700},
                                    "samples": samples, "laps": [{"startTimeInSeconds": t0}, {"startTimeInSeconds": t0 + 300}, {"startTimeInSeconds": t0 + 600}]}]}
    assert G.handle_notification(session, details, http)["created"] == 1
    det = session.query(Activity).filter_by(source="garmin_details").one()
    assert len(det.laps) == 3 and det.laps[1].distance_m == pytest.approx(900, abs=3)

    G.handle_notification(session, {"userPermissionsChange": [{"userId": "garmin-user-1", "permissions": ["WORKOUT_IMPORT"]}]}, http)
    conn = session.query(GarminConnection).one()
    assert conn.permissions == "WORKOUT_IMPORT"
    G.handle_notification(session, {"deregistrations": [{"userId": "garmin-user-1"}]}, http)
    assert conn.status == "revoked" and conn.access_token_enc == "" and conn.refresh_token_enc == ""
    assert G.handle_notification(session, file_note, http)["skipped"] == 1


def test_disconnect_calls_garmin(session, athlete, http, mock):
    _, a = connect(session, athlete, http)
    G.disconnect(session, a.garmin, http)
    assert any(r.method == "DELETE" and r.url.path == "/wellness-api/rest/user/registration" for r in mock.requests)
    assert a.garmin.status == "revoked" and a.garmin.access_token_enc == ""


def test_webhook_and_callback_routes(monkeypatch, mock):
    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import Athlete
    from xcapp.web import create_app
    monkeypatch.setattr(G, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(mock)))
    db = SessionLocal()
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=10, group="JV", link_token="tok-ava"))
    db.commit()
    c = TestClient(create_app(), follow_redirects=False)
    page = c.get("/a/tok-ava")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post("/a/tok-ava/connect", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/a/tok-ava"          # consent box not ticked
    r = c.post("/a/tok-ava/connect", data={"csrf": csrf, "consent": "yes"})
    # A hand-off page, not a redirect from the form: browsers apply form-action to every hop of a form's redirects,
    # and Garmin's sign-in can pass through other addresses.
    assert r.status_code == 200 and "Taking you to Garmin" in r.text
    to_garmin = html.unescape(re.search(r'id="go" class="button big-btn" href="([^"]+)"', r.text).group(1))
    assert to_garmin.startswith("https://connect.garmin.com/oauth2Confirm?")
    assert f"window.location.replace({json.dumps(to_garmin)})" in r.text.replace("\\u0026", "&")
    state = parse_qs(urlparse(to_garmin).query)["state"][0]
    r = c.get(f"/garmin/callback?code=abc&state={state}")
    assert r.status_code == 303 and r.headers["location"] == "/a/tok-ava"
    page = c.get("/a/tok-ava").text
    assert "Garmin is connected" in page, re.findall(r"<div class=\"flash[^>]*>([^<]*)", page)

    body = json.dumps({"activityFiles": [{"userId": "garmin-user-1", "fileType": "FIT",
                                          "callbackURL": "https://apis.garmin.com/files/activity/1"}]})
    assert c.post("/garmin/webhook/wrong", content=body).status_code == 404
    assert c.post("/garmin/webhook/hooksecret", content=body, headers={"garmin-client-id": "someone-else"}).status_code == 403
    assert c.post("/garmin/webhook/hooksecret", content="not json").status_code == 400
    assert c.post("/garmin/webhook/hooksecret", content=body, headers={"garmin-client-id": "cid-123"}).status_code == 200
    db2 = SessionLocal()
    assert db2.query(Activity).count() == 1
    db2.close()
    db.close()


def test_pasted_line_breaks_are_trimmed_from_settings(session, monkeypatch):
    """A secret pasted with a line break (seen live: client_id=%0A69c0...) made Garmin refuse the connection."""
    from xcapp import config
    from xcapp.models import Athlete
    monkeypatch.setenv("GARMIN_CLIENT_ID", "\n69c0e91d-test \r\n")
    monkeypatch.setenv("GARMIN_CLIENT_SECRET", " csecret-456\n")
    monkeypatch.setenv("SECURE_COOKIES", " false\n")
    s = config.reload_settings()
    assert (s.garmin_client_id, s.garmin_client_secret, s.secure_cookies) == ("69c0e91d-test", "csecret-456", False)
    a = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=10, group="JV", link_token="tok-trim")
    session.add(a)
    session.flush()
    url = G.start_authorization(session, a, "test")
    assert parse_qs(urlparse(url).query)["client_id"] == ["69c0e91d-test"] and "%0A" not in url

"""Race Day live switching (live.py): tokens for camera phones and the director, putting a phone on air, the YouTube
stream key (stored encrypted, never shown) and starting/stopping the stream. Cloudflare's API is replaced by a fake that
records each call."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import live as LV

from test_raceday import TEAMS, login, make_board, page_csrf


class FakeCloudflare:
    def __init__(self):
        self.calls, self.presets, self.people = [], [], []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/presets":
            if method == "GET":
                return [{"name": n} for n in self.presets]
            self.presets.append(body["name"])
            return {"id": "p-" + body["name"]}
        if path == "/meetings" and method == "POST":
            return {"id": "room-1"}
        if path.endswith("/participants") and method == "POST":
            if any(p["custom_participant_id"] == body["custom_participant_id"] for p in self.people):
                raise LV.LiveError("Cloudflare said: participant already exists")
            self.people.append({"id": f"pid{len(self.people)}", **body})
            return {"token": "tok-" + body["custom_participant_id"]}
        if path.endswith("/participants") and method == "GET":
            return self.people
        if path.endswith("/token"):
            return {"token": "fresh-" + path.split("/")[-2]}
        if path == "/recordings" and method == "POST":
            return {"id": "rec-9", "status": "INVOKED"}
        if path.startswith("/recordings/"):
            return {"status": "STOPPED"}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake(monkeypatch):
    for k, v in (("REALTIME_ACCOUNT_ID", "acct"), ("REALTIME_APP_ID", "app"), ("REALTIME_API_TOKEN", "secret-token")):
        monkeypatch.setenv(k, v)
    from xcapp import config
    config.reload_settings()
    f = FakeCloudflare()
    monkeypatch.setattr(LV, "api", f)
    monkeypatch.setattr(LV, "_presets_ready", False)
    return f


@pytest.fixture
def app():
    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.commit()
    db.close()
    return create_app()


def setup_race(coach, tap):
    page = coach.get(f"/rd/{tap}")
    csrf = page_csrf(page.text)
    coach.post(f"/rd/{tap}/race", json={"csrf": csrf, "name": "Varsity", "teams": TEAMS, "checkpoints": ["1 Mile", "Finish"]})
    return csrf


def test_live_off_until_configured(app):
    coach = login(app, "coach1")
    tap, watch = make_board(coach)
    st = coach.get(f"/rd/{tap}/state").json()
    assert st["live"] == {"configured": False, "on_air": "", "streaming": False, "since": None, "has_key": False}
    r = TestClient(app).post(f"/rd/{tap}/live/token", json={"role": "camera", "device": "phone1", "name": "1 Mile"})
    assert r.status_code == 400 and "isn't set up" in r.json()["error"]


def test_cameras_director_and_stream(app, fake):
    coach = login(app, "coach1")
    tap, watch = make_board(coach)
    csrf = setup_race(coach, tap)
    anon = TestClient(app)

    # A camera phone at the course (no account) gets a camera token; the room and roles are made once
    r = anon.post(f"/rd/{tap}/live/token", json={"role": "camera", "device": "phone1", "name": "1 Mile · Coach K"})
    assert r.json() == {"token": "tok-camera-phone1"}
    assert sorted(fake.presets) == ["recorder_preset_v2", "xc_camera", "xc_director"]
    added = [b for m, p, b in fake.calls if p.endswith("/participants") and m == "POST"]
    assert added[0]["preset_name"] == "xc_camera" and added[0]["name"] == "1 Mile · Coach K"
    cam = next(b for m, p, b in fake.calls if p == "/presets" and m == "POST" and b["name"] == "xc_camera")
    assert cam["permissions"]["media"]["video"]["can_produce"] == "ALLOWED" and cam["config"]["max_video_streams"]["mobile"] == 1
    rec = next(b for m, p, b in fake.calls if p == "/presets" and m == "POST" and b["name"] == "recorder_preset_v2")
    assert rec["permissions"]["is_recorder"] is True and rec["permissions"]["hidden_participant"] is True
    # The same phone again: a fresh token, no second room
    assert anon.post(f"/rd/{tap}/live/token", json={"role": "camera", "device": "phone1", "name": "x"}).json()["token"] == "fresh-pid0"
    assert sum(1 for m, p, _ in fake.calls if p == "/meetings") == 1
    # Only a signed-in coach can direct, switch, or stream
    assert anon.post(f"/rd/{tap}/live/token", json={"role": "director", "device": "phone9", "csrf": csrf}).status_code == 403
    assert anon.post(f"/rd/{tap}/live/air", json={"device": "phone1", "csrf": csrf}).status_code == 403
    assert anon.post(f"/rd/{tap}/live/stream", json={"action": "start", "csrf": csrf}).status_code == 403
    assert anon.post(f"/rd/{tap}/live/key", json={"key": "abcd", "csrf": csrf}).status_code == 403
    assert coach.post(f"/rd/{tap}/live/token", json={"role": "director", "device": "dir1", "csrf": csrf}).json()["token"] == "tok-director-dir1"
    assert anon.post(f"/rd/{tap}/live/token", json={"role": "camera", "device": "bad id!"}).status_code == 400

    # Putting a phone on air shows on every page, families included
    assert coach.post(f"/rd/{tap}/live/air", json={"device": "phone1", "csrf": csrf}).json()["live"]["on_air"] == "phone1"
    assert anon.get(f"/rw/{watch}/state").json()["live"]["on_air"] == "phone1"

    # The stream needs the YouTube key; the key is kept encrypted and never sent back
    r = coach.post(f"/rd/{tap}/live/stream", json={"action": "start", "csrf": csrf})
    assert r.status_code == 400 and "stream key" in r.json()["error"]
    assert coach.post(f"/rd/{tap}/live/key", json={"key": "bad key!", "csrf": csrf}).status_code == 400
    assert coach.post(f"/rd/{tap}/live/key", json={"key": "abcd-efgh-ijkl-mnop", "csrf": csrf}).json()["live"]["has_key"] is True
    from xcapp.db import SessionLocal
    from xcapp.models import RaceBoard
    db = SessionLocal()
    stored = db.query(RaceBoard).one().yt_key
    db.close()
    assert stored and "abcd-efgh" not in stored
    assert "abcd-efgh" not in json.dumps(coach.get(f"/rd/{tap}/state").json())

    r = coach.post(f"/rd/{tap}/live/stream", json={"action": "start", "csrf": csrf})
    assert r.json()["live"]["streaming"] is True and r.json()["live"]["since"]
    start = next(b for m, p, b in fake.calls if p == "/recordings" and m == "POST")
    assert start["rtmp_out_config"]["rtmp_url"] == "rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop"
    assert start["url"] == f"https://xc.test/rw/{watch}/program" and start["meeting_id"] == "room-1"
    assert start["max_seconds"] == 4 * 3600
    coach.post(f"/rd/{tap}/live/stream", json={"action": "start", "csrf": csrf})          # already streaming: no second job
    assert sum(1 for m, p, _ in fake.calls if p == "/recordings" and m == "POST") == 1

    # The program page Cloudflare films: public by the watch code, allowed to reach RealtimeKit
    prog = anon.get(f"/rw/{watch}/program")
    assert prog.status_code == 200 and "realtimekit-2.0.4.js" in prog.text and "raceday-program.js" in prog.text
    assert "wss://*.realtime.cloudflare.com" in prog.headers["content-security-policy"]
    assert anon.get(f"/rw/{tap}/program").status_code == 404

    r = coach.post(f"/rd/{tap}/live/stream", json={"action": "stop", "csrf": csrf})
    assert r.json()["live"]["streaming"] is False
    assert ("PUT", "/recordings/rec-9", {"action": "stop"}) in fake.calls

    # Deleting the board stops a running stream
    coach.post(f"/rd/{tap}/live/stream", json={"action": "start", "csrf": csrf})
    html = coach.get("/race-day").text
    bid = re.search(r'action="/race-day/(\d+)/delete"', html).group(1)
    n = sum(1 for m, p, _ in fake.calls if m == "PUT")
    coach.post(f"/race-day/{bid}/delete", data={"csrf": page_csrf(html)})
    assert sum(1 for m, p, _ in fake.calls if m == "PUT") == n + 1


def test_api_errors_are_readable(monkeypatch):
    import httpx
    for k, v in (("REALTIME_ACCOUNT_ID", "acct"), ("REALTIME_APP_ID", "app"), ("REALTIME_API_TOKEN", "secret-token")):
        monkeypatch.setenv(k, v)
    from xcapp import config
    config.reload_settings()
    seen = {}

    def fake_request(method, url, json=None, timeout=None, headers=None):
        seen.update(url=url, auth=headers["Authorization"])
        return httpx.Response(401, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]})
    monkeypatch.setattr(httpx, "request", fake_request)
    with pytest.raises(LV.LiveError, match="Authentication error"):
        LV.api("GET", "/presets")
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/realtime/kit/app/presets"
    assert seen["auth"] == "Bearer secret-token"

"""Privacy: AI analysis only with explicit consent (and easy withdrawal), data download, deletion, and the policy page."""
import datetime as dt
import json
import re
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from xcapp import ai as AI
from xcapp import datasets as DS
from xcapp import garmin as G
from xcapp.db import SessionLocal
from xcapp.models import (Activity, Athlete, DeletionRequest, GarminConnection, Injury, MissedDay, StaffUser)
from xcapp.security import encrypt, hash_password


class FakeClient:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=self.text)], stop_reason="end_turn",
                                     usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("AS_OF_DATE", "2026-10-25")
    from xcapp import config
    config.reload_settings()
    from xcapp.web import create_app
    deletes = []
    monkeypatch.setattr(G, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda req: (deletes.append(req.url.path), httpx.Response(204))[1])))
    db = SessionLocal()
    db.add(StaffUser(username="admin1", role="admin", password_hash=hash_password("correct-horse-1")))
    ava = Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava")
    mat = Athlete(first_name="Mateo", last_name="Garza", gender="Boys", grade=10, group="JV", link_token="tok-mateo")
    db.add_all([ava, mat])
    db.flush()
    for k, (who, day, src, miles) in enumerate([(ava, 20, "garmin_file", 6), (ava, 21, "upload", 5), (ava, 22, "garmin_summary", 5),
                                                (mat, 20, "garmin_file", 9), (mat, 22, "garmin_file", 9)]):
        db.add(Activity(athlete_id=who.id, source=src, external_id=f"x{k}", start_utc=dt.datetime(2026, 10, day, 22),
                        local_date=dt.date(2026, 10, day), distance_m=miles * 1609.344, duration_s=miles * 480, sport="running", rpe=5))
    db.add(GarminConnection(athlete_id=ava.id, garmin_user_id="g-ava", access_token_enc=encrypt("AT-secret"), refresh_token_enc=encrypt("RT-secret"),
                            access_expires_at=dt.datetime(2030, 1, 1), status="connected"))
    db.add(Injury(athlete_id=ava.id, onset_date=dt.date(2026, 10, 1), injury="Ankle sprain", body_area="Ankle", notes="TRAINER-ONLY-NOTE"))
    db.add(MissedDay(athlete_id=ava.id, date=dt.date(2026, 10, 23), reason="Injury", body_area="Knee", side="Left", pain=3,
                     description="aches on stairs"))
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/tok-ava").text).group(1)
    return c, csrf, deletes


def test_ai_is_off_until_explicit_consent(web):
    c, csrf, _ = web
    page = c.get("/a/tok-ava").text
    assert "AI analysis" in page and "Turn on AI analysis" in page and 'href="/privacy#ai"' in page
    db = SessionLocal()
    with pytest.raises(AI.AiError, match="No athletes have agreed"):
        AI.build_payload(DS.snapshot(db, dt.date(2026, 10, 25)))
    run = AI.generate_briefing(db, client=FakeClient("{}"), as_of=dt.date(2026, 10, 25))
    assert run.status == "failed" and "No athletes have agreed" in run.error
    db.close()
    c.post("/a/tok-ava/ai-consent", data={"csrf": csrf, "action": "give", "agree": "yes"})          # parent box not ticked
    assert "tick both boxes" in c.get("/a/tok-ava").text
    c.post("/a/tok-ava/ai-consent", data={"csrf": csrf, "action": "give", "agree": "yes", "parent": "yes"})
    assert "Turn off AI analysis" in c.get("/a/tok-ava").text
    db = SessionLocal()
    payload, code_map = AI.build_payload(DS.snapshot(db, dt.date(2026, 10, 25)))
    assert [v["name"] for v in code_map.values()] == ["Ava Doyle"] and len(payload["athletes"]) == 1
    assert payload["team"]["athletes"] == 1 and payload["team"]["kpis"]["roster"] == 1           # team numbers from consenting athletes only
    assert all(w["athlete"] == "R01" for w in payload["team"]["injuries_last_150_days"])
    text = json.dumps(payload)
    assert "Mateo" not in text and "aches on stairs" not in text and "TRAINER-ONLY-NOTE" not in text
    fake = FakeClient("R01 is fine.")
    run = AI.ask(db, "Compare Ava Doyle with Mateo Garza, and how is mateo?", client=fake, as_of=dt.date(2026, 10, 25))
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Mateo" not in sent and "Garza" not in sent and "an athlete who has not agreed to AI analysis" in sent and "R01" in sent
    assert run.answer == "Ava Doyle is fine."
    db.close()
    c.post("/a/tok-ava/ai-consent", data={"csrf": csrf, "action": "withdraw"})
    assert "Turn on AI analysis" in c.get("/a/tok-ava").text
    db = SessionLocal()
    with pytest.raises(AI.AiError):
        AI.build_payload(DS.snapshot(db, dt.date(2026, 10, 25)))
    db.close()


def test_download_my_data(web):
    c, csrf, _ = web
    r = c.get("/a/tok-ava/my-data.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["athlete"]["name"] == "Ava Doyle" and len(d["runs"]) == 3 and d["garmin"]["status"] == "connected"
    assert d["days_missed"][0]["description"] == "aches on stairs" and d["injuries"][0]["injury"] == "Ankle sprain"
    assert "AT-secret" not in r.text and "RT-secret" not in r.text and "TRAINER-ONLY-NOTE" not in r.text
    assert c.get("/a/nope/my-data.json").status_code == 404


def test_delete_garmin_data_and_request_everything(web):
    c, csrf, deletes = web
    c.post("/a/tok-ava/delete-garmin-data", data={"csrf": csrf, "confirm": "nope"})
    assert "Type DELETE" in c.get("/a/tok-ava").text
    c.post("/a/tok-ava/delete-garmin-data", data={"csrf": csrf, "confirm": "delete"})
    db = SessionLocal()
    ava = db.query(Athlete).filter_by(first_name="Ava").one()
    assert [a.source for a in db.query(Activity).filter_by(athlete_id=ava.id)] == ["upload"]      # only Garmin runs removed
    assert db.query(GarminConnection).count() == 0 and db.query(Activity).count() == 3           # Mateo untouched
    assert "/wellness-api/rest/user/registration" in deletes                                    # Garmin told
    db.close()
    c.post("/a/tok-ava/delete-request", data={"csrf": csrf})
    c.post("/a/tok-ava/delete-request", data={"csrf": csrf})
    assert "within 30 days" in c.get("/a/tok-ava").text
    db = SessionLocal()
    assert db.query(DeletionRequest).filter_by(status="open").count() == 1
    db.close()
    ad = TestClient(c.app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', ad.get("/login").text).group(1)
    ad.post("/login", data={"csrf": tok, "username": "admin1", "password": "correct-horse-1"})
    assert "Deletion requests" in ad.get("/").text
    tok = re.search(r'name="csrf" value="([^"]+)"', ad.get(f"/athletes/{ava.id}").text).group(1)
    ad.post(f"/athletes/{ava.id}/delete", data={"csrf": tok, "confirm_name": "Ava Doyle"})
    db = SessionLocal()
    req = db.query(DeletionRequest).one()
    assert (req.status, req.athlete_name, req.completed_by) == ("done", "Ava Doyle", "admin1") and db.query(MissedDay).count() == 0
    db.close()
    assert "Deletion requests" not in ad.get("/").text


def test_policy_page(web, monkeypatch):
    c, _, _ = web
    page = c.get("/privacy").text                                             # public: no sign-in
    for must in ("How we use Garmin data", "Who else receives or processes Garmin data", "We do not sell or share Garmin data",
                 "AI transparency statement", "Turn off AI analysis", "Download my data", "Delete my Garmin data",
                 "never used to train, evaluate or improve", "Anthropic PBC", "connect-support@developer.garmin.com",
                 "[privacy contact email]", "[hosting provider]"):
        assert must in page, must
    monkeypatch.setenv("PRIVACY_CONTACT", "xc-privacy@example.org")
    monkeypatch.setenv("HOSTING_PROVIDER", "Example Hosting Co.")
    monkeypatch.setenv("ORG_NAME", "Jesuit High School")
    from xcapp import config
    config.reload_settings()
    from xcapp.web import create_app
    page = TestClient(create_app()).get("/privacy").text
    assert "xc-privacy@example.org" in page and "Example Hosting Co." in page and "[privacy contact email]" not in page
    assert "Let’s Encrypt</b> (security certificate)" in page                 # default certificate issuer, listed separately

    # On Cloudflare the certificate comes from the host, so it is not listed as a separate recipient.
    monkeypatch.setenv("HOSTING_PROVIDER", "Cloudflare, Inc. (runs the app) and Supabase, Inc. (stores the database)")
    monkeypatch.setenv("CERTIFICATE_PROVIDER", "Cloudflare")
    config.reload_settings()
    page = TestClient(create_app()).get("/privacy").text
    assert "Cloudflare, Inc. (runs the app) and Supabase, Inc. (stores the database)</b> (hosting)" in page
    assert "(security certificate)" not in page and "own server" not in page

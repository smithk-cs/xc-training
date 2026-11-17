"""Phone notifications (push.py, push_web.py): the encryption a phone can undo, the signed VAPID token, sending and
cleaning up, and the athlete and coach pages. Names and addresses are made up."""
import datetime as dt
import json
import re
import struct

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import push as PU
from xcapp import push_web as PW
from xcapp import reminders as RM
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, PushSubscription, StaffUser
from xcapp.security import hash_password

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


class Phone:
    """A browser's side of a subscription: its keys, and how it decrypts what arrives (RFC 8291)."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.p256dh = PU.b64u(self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    def decrypt(self, body: bytes) -> bytes:
        salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
        assert rs == 4096
        as_public, cipher = body[21:21 + idlen], body[21 + idlen:]
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))

        def hk(salt_, ikm, info, n):
            return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt_, info=info).derive(ikm)
        ikm = hk(self.auth, shared, b"WebPush: info\x00" + PU.unb64u(self.p256dh) + as_public, 32)
        plain = AESGCM(hk(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(
            hk(salt, ikm, b"Content-Encoding: nonce\x00", 12), cipher, None)
        assert plain.endswith(b"\x02")
        return plain[:-1]


def test_encryption_round_trip_and_vapid_token(session):
    phone = Phone()
    body = PU.encrypt_payload(b'{"title":"Hi"}', phone.p256dh, PU.b64u(phone.auth))
    assert phone.decrypt(body) == b'{"title":"Hi"}'
    key = PU.vapid_key(session)
    assert PU.vapid_key(session).private_numbers() == key.private_numbers()      # kept, not remade
    assert len(PU.unb64u(PU.public_key(session))) == 65
    header = PU.vapid_header(key, ENDPOINT, now=1_000_000)
    t, k = re.fullmatch(r"vapid t=([^,]+), k=(.+)", header).groups()
    head, claims, sig = t.split(".")
    assert json.loads(PU.unb64u(claims))["aud"] == "https://fcm.googleapis.com"
    raw = PU.unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), PU.unb64u(k))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))              # raises if the signature is wrong


def make_athletes(db):
    out = []
    for i, (first, gender) in enumerate((("Sam", "Boys"), ("Ava", "Girls"))):
        a = Athlete(first_name=first, last_name="Lowry", gender=gender, grade=11, group="Varsity", link_token=f"t{i}")
        db.add(a)
        out.append(a)
    db.flush()
    return out


def test_send_delivers_and_removes_gone_phones(session):
    sam, ava = make_athletes(session)
    phones = [Phone(), Phone(), Phone()]
    for n, (who, phone) in enumerate(zip((sam, sam, ava), phones)):
        session.add(PushSubscription(athlete_id=who.id, endpoint=f"{ENDPOINT}/{n}", p256dh=phone.p256dh, auth=PU.b64u(phone.auth)))
    session.flush()
    got = []

    def service(req):
        n = int(str(req.url).rsplit("/", 1)[1])
        assert req.headers["content-encoding"] == "aes128gcm" and req.headers["authorization"].startswith("vapid t=")
        got.append(json.loads(phones[n].decrypt(req.content)))
        return httpx.Response({0: 201, 1: 410, 2: 500}[n])
    with httpx.Client(transport=httpx.MockTransport(service)) as http:
        r = PU.send(session, [sam.id, ava.id], "Team", "Practice at 3:30", "https://x.example/a/t0", http=http)
    assert r == {"sent": 1, "gone": 1, "failed": 1}
    assert got[0] == {"title": "Team", "body": "Practice at 3:30", "url": "https://x.example/a/t0"}
    session.flush()
    left = {s.endpoint: s for s in session.query(PushSubscription).all()}
    assert set(left) == {f"{ENDPOINT}/0", f"{ENDPOINT}/2"} and left[f"{ENDPOINT}/0"].last_ok_at is not None


def test_endpoint_allowlist():
    assert PW.endpoint_ok(ENDPOINT)
    assert PW.endpoint_ok("https://web.push.apple.com/QGx")
    assert PW.endpoint_ok("https://updates.push.services.mozilla.com/wpush/v2/x")
    for bad in ("http://fcm.googleapis.com/x", "https://evil.example/x", "https://fcm.googleapis.com.evil.example/x",
                "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x", "https://169.254.169.254/x", "not a url"):
        assert not PW.endpoint_ok(bad), bad


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    make_athletes(db)
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def test_athlete_turns_on_tests_and_turns_off(app, monkeypatch):
    sent = []
    monkeypatch.setattr(PU, "send", lambda db, ids, title, body, url, http=None: sent.append((ids, title, body, url))
                        or {"sent": 1, "gone": 0, "failed": 0})
    c = TestClient(app, follow_redirects=False)
    page = c.get("/a/t0").text
    assert 'id="push-panel"' in page and 'rel="manifest" href="/a/t0/manifest.webmanifest"' in page
    csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
    manifest = c.get("/a/t0/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json") and manifest.json()["start_url"] == "/a/t0"
    sw = c.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript") and "showNotification" in sw.text
    phone = Phone()
    fields = {"csrf": csrf, "endpoint": ENDPOINT, "p256dh": phone.p256dh, "auth": PU.b64u(phone.auth), "device": "iPhone"}
    assert c.post("/a/t0/push", data={**fields, "endpoint": "https://evil.example/x"}).status_code == 400
    assert c.post("/a/t0/push", data={**fields, "auth": "short"}).status_code == 400
    assert c.post("/a/t0/push", data={**fields, "csrf": "wrong"}).status_code == 400
    assert c.post("/a/t0/push", data=fields).json() == {"ok": True}
    assert c.post("/a/t0/push", data=fields).json() == {"ok": True}                # the same phone again: still one
    db = SessionLocal()
    subs = db.query(PushSubscription).all()
    assert len(subs) == 1 and subs[0].device == "iPhone"
    db.close()
    assert "Phone notifications <span" in c.get("/a/t0").text                       # shows as on
    assert c.post("/a/t0/push/test", data={"csrf": csrf}).json()["ok"]
    assert sent[0][0] == [subs[0].athlete_id] and sent[0][3].endswith("/a/t0")
    assert c.post("/a/t0/push/off", data={"csrf": csrf, "endpoint": ENDPOINT}).json() == {"ok": True}
    db = SessionLocal()
    assert db.query(PushSubscription).count() == 0
    db.close()


def test_coach_sends_to_a_team(app, monkeypatch):
    db = SessionLocal()
    sam, ava = db.query(Athlete).order_by(Athlete.id).all()
    for n, a in enumerate((sam, ava)):
        db.add(PushSubscription(athlete_id=a.id, endpoint=f"{ENDPOINT}/{n}", p256dh=Phone().p256dh, auth=PU.b64u(b"0123456789abcdef")))
    db.commit()
    ids = {"sam": sam.id, "ava": ava.id}
    db.close()
    sent = []
    monkeypatch.setattr(PU, "send", lambda db, ids_, title, body, url, http=None: sent.append((ids_, title, body, url))
                        or {"sent": 1, "gone": 0, "failed": 0})
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/reminders").text
    assert "Send a notification" in page and "Whole team (2)" in page and "Girls (1)" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/reminders/notify", data={"csrf": csrf, "message": "  Practice   moved to 3:30 ", "team": "Girls"})
    assert r.status_code == 303
    assert len(sent) == 1 and sent[0][0] == [ids["ava"]] and sent[0][2] == "Practice moved to 3:30"
    assert sent[0][1] and sent[0][3].endswith("/a/t1")          # the team name as the title, and her own page
    assert "Sent to 1 athlete" in c.get("/reminders").text
    c.post("/reminders/notify", data={"csrf": csrf, "message": "Hi all", "title": "Meet day", "team": "All"})
    assert [s[0] for s in sent[1:]] == [[ids["sam"]], [ids["ava"]]] and sent[1][1] == "Meet day"
    c.post("/reminders/notify", data={"csrf": csrf, "message": "  ", "team": "All"})
    assert "Write a message first" in c.get("/reminders").text and len(sent) == 3


def test_daily_run_notifies_phones(session, monkeypatch):
    sam, ava = make_athletes(session)
    today = A.team_today()
    A.set_setting(session, "reminders_enabled", "1")
    for k in (1, 2):
        d = today - dt.timedelta(days=k)
        session.add(Activity(athlete_id=sam.id, source="upload", external_id=f"x{k}", start_utc=dt.datetime(d.year, d.month, d.day, 15),
                             local_date=d, duration_s=1800, distance_m=6000, sport="running"))
    for a in (sam, ava):
        session.add(PushSubscription(athlete_id=a.id, endpoint=f"{ENDPOINT}/{a.id}", p256dh=Phone().p256dh, auth=PU.b64u(b"0123456789abcdef")))
    session.flush()
    sent = []
    monkeypatch.setattr(PU, "send", lambda db, ids, title, body, url, http=None: sent.append((ids, title, body))
                        or {"sent": 1, "gone": 0, "failed": 0})
    out = RM.run(session, today)
    assert out["pushed"] == len(sent) >= 1
    who = {ids[0] for ids, _, _ in sent}
    assert sam.id in who
    title, body = next((t, b) for ids, t, b in sent if ids == [sam.id])
    assert "to finish" in title and "Rate how hard 2 runs felt" in body

"""Phone notifications (Web Push), free: an athlete turns them on on their own page (on an iPhone, once the page is on
the home screen), and the app sends through their phone's own push service (Apple's, Google's or Mozilla's).

- VAPID keys identify the app to the push services. They're made on first use and kept in the database (the private
  key encrypted with TOKEN_KEY), so there's nothing to set up.
- Each message is encrypted for the phone it goes to (RFC 8291, "aes128gcm"), so the push service can't read it.
- A subscription the push service says is gone (404 or 410: notifications turned off, app removed) is deleted.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .config import settings
from .security import decrypt, encrypt

KEY_SETTING = "push_vapid_private"
TTL = 24 * 3600                           # a phone that's off for a day still gets it when it comes back


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_key(db: Session) -> ec.EllipticCurvePrivateKey:
    """The app's VAPID private key, made and saved (encrypted) the first time it's needed."""
    saved = A.get_setting(db, KEY_SETTING, "")
    if saved:
        return serialization.load_pem_private_key(decrypt(saved).encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    A.set_setting(db, KEY_SETTING, encrypt(pem.decode()))
    return key


def public_key(db: Session) -> str:
    """The VAPID public key, as the athlete page's browser needs it (applicationServerKey)."""
    return b64u(_raw_public(vapid_key(db).public_key()))


def vapid_header(key: ec.EllipticCurvePrivateKey, endpoint: str, now: float | None = None) -> str:
    """Authorization header for one push service: a signed (ES256) token naming its origin."""
    u = urlsplit(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": settings().base_url.rstrip("/")},
                             separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={b64u(_raw_public(key.public_key()))}"


def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def encrypt_payload(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
                    sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """The message encrypted for one phone (RFC 8291 / RFC 8188 aes128gcm, one record)."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(sender.public_key())
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)        # \x02: the last (and only) record
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + body


def subscriptions(db: Session, athlete_ids):
    from .models import PushSubscription
    return db.scalars(select(PushSubscription).where(PushSubscription.athlete_id.in_(list(athlete_ids)))).all()


def send(db: Session, athlete_ids, title: str, body: str, url: str, http: httpx.Client | None = None) -> dict:
    """Notify every phone these athletes turned notifications on for: {"sent", "gone", "failed"} (gone: removed)."""
    from .models import utcnow
    subs = subscriptions(db, athlete_ids)
    out = {"sent": 0, "gone": 0, "failed": 0}
    if not subs:
        return out
    key = vapid_key(db)
    payload = json.dumps({"title": title[:80], "body": body[:240], "url": url}, separators=(",", ":")).encode()
    client = http or httpx.Client(timeout=15)
    try:
        for sub in subs:
            try:
                r = client.post(sub.endpoint, content=encrypt_payload(payload, sub.p256dh, sub.auth), headers={
                    "Authorization": vapid_header(key, sub.endpoint), "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream", "TTL": str(TTL), "Urgency": "normal"})
            except httpx.HTTPError:
                out["failed"] += 1
                continue
            if r.status_code in (404, 410):
                db.delete(sub)
                out["gone"] += 1
            elif r.status_code < 300:
                sub.last_ok_at = utcnow()
                out["sent"] += 1
            else:
                out["failed"] += 1
    finally:
        if http is None:
            client.close()
    return out

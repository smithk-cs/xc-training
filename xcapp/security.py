"""Passwords (scrypt), encryption of Garmin tokens at rest (Fernet), CSRF tokens, and keeping secret links out of the
access log."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Passwords must be at least 10 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except Exception:
        return False


def new_link_token() -> str:
    return secrets.token_urlsafe(24)


def generate_token_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = settings().token_key
    if not key:
        raise RuntimeError("TOKEN_KEY is not set; Garmin tokens cannot be stored safely.")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored Garmin token could not be decrypted; was TOKEN_KEY changed?") from exc


def csrf_token(session: dict) -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        session["csrf"] = tok
    return tok


def csrf_ok(session: dict, submitted: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


def session_mark(password_hash: str) -> str:
    """A short fingerprint of a staff member's current password, kept in their session at sign-in. Every page checks
    it still matches, so changing a password signs out every other device at once."""
    return hashlib.sha256(("session:" + password_hash).encode()).hexdigest()[:20]


def csv_safe(value):
    """A spreadsheet cell that can't run as a formula: text starting with = + - @ (or a tab or return) gets a leading
    apostrophe, which Excel and Sheets show as plain text. Numbers, including negative ones, are left alone."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(value)
            return value
        except ValueError:
            return "'" + value
    return value


# Addresses where part of the path is itself the key (an athlete's link, the Garmin webhook's secret, the team calendar's
# and Race Day's links, a newsletter photo's key), and the one-time code Garmin sends back to /garmin/callback.
_SECRET_PATH = re.compile(r"^(/(?:a|garmin/webhook|team-calendar|rd|rw)/|/newsletter/photo/[^/?#]+/)[^/?#]+")
_SECRET_QUERY = re.compile(r"^(/garmin/callback\?).+")


def redact_path(path: str) -> str:
    """The address with its secret part replaced by "…": /a/Xy12.../runs becomes /a/…/runs."""
    return _SECRET_QUERY.sub(r"\1…", _SECRET_PATH.sub(r"\1…", path))


class RedactSecrets(logging.Filter):
    """On the uvicorn.access logger: requests are still logged, without the secret part of their address. Its
    records' args are (client, method, path with query, HTTP version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = args[:2] + (redact_path(args[2]),) + args[3:]
        return True


def redact_access_log() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecrets) for f in access.filters):
        access.addFilter(RedactSecrets())

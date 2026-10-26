"""Garmin Connect Developer Program integration.

Authorization follows Garmin's published "OAuth2.0 PKCE Specification": authorize at
connect.garmin.com/oauth2Confirm, exchange at diauth.garmin.com/.../oauth/token (client secret required),
then GET /wellness-api/rest/user/id and /user/permissions with a Bearer token. Access tokens are refreshed
600 seconds before they expire, as Garmin recommends, and "Disconnect" calls DELETE /user/registration.

Activity notifications (push or ping) are parsed in one place, `handle_notification`. Garmin gives the exact
notification field names to approved developers; the names used here are the commonly documented ones
(summaryId, activityType, startTimeInSeconds, callbackURL, ...). Check them against your Garmin docs.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .fitfile import MAX_FIT_BYTES, ParsedActivity, ParsedLap, parse_fit
from .ingest import ingest, log
from .models import Athlete, GarminBackfill, GarminConnection, OAuthState
from .security import decrypt, encrypt

STATE_MAX_AGE = dt.timedelta(minutes=15)
REFRESH_MARGIN = dt.timedelta(seconds=600)


class GarminError(Exception):
    pass


def make_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]  # 43-128 chars of A-Z a-z 0-9 - _
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ---------- connecting an athlete ----------

def start_authorization(db: Session, athlete: Athlete, consent_note: str) -> str:
    s = settings()
    if not s.garmin_configured:
        raise GarminError("Garmin is not set up yet. Add GARMIN_CLIENT_ID and GARMIN_CLIENT_SECRET.")
    cutoff = _now() - STATE_MAX_AGE
    for old in db.scalars(select(OAuthState).where(OAuthState.created_at < cutoff)).all():
        db.delete(old)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    db.add(OAuthState(state=state, verifier=verifier, athlete_id=athlete.id, consent_note=consent_note))
    db.flush()
    q = {
        "response_type": "code", "client_id": s.garmin_client_id, "code_challenge": challenge,
        "code_challenge_method": "S256", "redirect_uri": s.garmin_redirect_uri, "state": state,
    }
    return f"{s.garmin_authorize_url}?{urlencode(q)}"


def _token_request(http: httpx.Client, data: dict) -> dict:
    s = settings()
    payload = {"client_id": s.garmin_client_id, "client_secret": s.garmin_client_secret, **data}
    r = http.post(s.garmin_token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        raise GarminError(f"Garmin token request failed ({r.status_code}).{_reply(r)}")
    body = r.json()
    if not body.get("access_token"):
        raise GarminError("Garmin did not return an access token.")
    return body


def _store_tokens(conn: GarminConnection, body: dict) -> None:
    now = _now()
    conn.access_token_enc = encrypt(body["access_token"])
    if body.get("refresh_token"):
        conn.refresh_token_enc = encrypt(body["refresh_token"])
    conn.access_expires_at = now + dt.timedelta(seconds=int(body.get("expires_in") or 86400))
    if body.get("refresh_token_expires_in"):
        conn.refresh_expires_at = now + dt.timedelta(seconds=int(body["refresh_token_expires_in"]))


def _reply(r: httpx.Response) -> str:
    """The start of Garmin's reply, for the Sync log: a refusal usually says why in its body."""
    text = " ".join((r.text or "").split())[:200]
    return f" Garmin said: {text}" if text else ""


def _api_get(http: httpx.Client, token: str, path: str):
    r = http.get(settings().garmin_api_base.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise GarminError(f"Garmin request {path} failed ({r.status_code}).{_reply(r)}")
    return r.json()


def _permissions_list(body) -> list[str]:
    if isinstance(body, list):
        return [str(x) for x in body]
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list):
                return [str(x) for x in v]
    return []


def complete_authorization(db: Session, code: str, state: str, http: httpx.Client) -> Athlete:
    st = db.get(OAuthState, state) if state else None
    if st is None:
        raise GarminError("This Garmin link has expired. Please start again from your athlete page.")
    db.delete(st)
    if _now() - st.created_at > STATE_MAX_AGE:
        raise GarminError("This Garmin link has expired. Please start again from your athlete page.")
    athlete = db.get(Athlete, st.athlete_id)
    if athlete is None:
        raise GarminError("Athlete not found.")
    body = _token_request(http, {
        "grant_type": "authorization_code", "code": code, "code_verifier": st.verifier,
        "redirect_uri": settings().garmin_redirect_uri,
    })
    token = body["access_token"]
    user_id = str(_api_get(http, token, "/wellness-api/rest/user/id").get("userId") or "")
    if not user_id:
        raise GarminError("Garmin did not return a user id.")
    try:
        perms = _permissions_list(_api_get(http, token, "/wellness-api/rest/user/permissions"))
    except GarminError as exc:
        perms = []
        log(db, "garmin", f"Could not read {athlete.name}'s Garmin permissions: {exc}", "warn")
    other = db.scalar(select(GarminConnection).where(GarminConnection.garmin_user_id == user_id))
    if other is not None and other.athlete_id != athlete.id:
        raise GarminError("That Garmin account is already linked to another athlete on this team.")
    conn = athlete.garmin or other
    if conn is None:
        conn = GarminConnection(garmin_user_id=user_id)
        conn.athlete = athlete  # sets athlete.garmin too
        db.add(conn)
    conn.garmin_user_id = user_id
    conn.status = "connected"
    conn.permissions = ",".join(perms)
    conn.consent_note = st.consent_note
    conn.consent_at = _now()
    conn.connected_at = _now()
    _store_tokens(conn, body)
    db.flush()
    log(db, "garmin", f"{athlete.name} connected Garmin (permissions: {conn.permissions or 'unknown'}).")
    if perms and "ACTIVITY_EXPORT" not in perms:
        log(db, "garmin", f"{athlete.name} did not share activities with the team app.", "warn")
    elif settings().garmin_backfill_days > 0:  # past runs make load ratios useful from day one
        queue_backfill(db, conn, settings().garmin_backfill_days, delay=BACKFILL_AFTER_CONNECT)
    return athlete


# ---------- backfill (past runs) ----------

# Garmin's Summary Backfill: ask for a past date range; Garmin answers 202 and later delivers the activities to the
# same notification endpoints as new runs (Activities, and Activity Details for laps and heart rate). A range already
# requested comes back 409. Seen live: right after connecting, Garmin answers 403 "User not registered with consumer"
# for a short while, and backfill is limited per minute (429 "Limit 100 per 1 minute"). So requests wait in a queue
# (GarminBackfill), start a couple of minutes after connecting, go out at most once a minute, and are retried.
BACKFILL_PATH = "/wellness-api/rest/backfill/{kind}"
BACKFILL_KINDS = ("activities", "activityDetails")
BACKFILL_START, BACKFILL_END = "summaryStartTimeInSeconds", "summaryEndTimeInSeconds"
BACKFILL_MAX_DAYS = 90                              # longest range in one request
BACKFILL_AFTER_CONNECT = dt.timedelta(minutes=2)    # let Garmin finish registering the athlete
BACKFILL_SPACING_S = 65                             # seconds between requests to Garmin
BACKFILL_RETRY = dt.timedelta(minutes=5)
BACKFILL_MAX_ATTEMPTS = 6


def queue_backfill(db: Session, conn: GarminConnection, days: int, delay: dt.timedelta = dt.timedelta(0)) -> str:
    """Queue requests for the past `days` of runs, in ranges Garmin accepts. Returns a message for the coach."""
    days = max(1, min(int(days), 365))
    end = _now()
    start = end - dt.timedelta(days=days)
    for kind in BACKFILL_KINDS:  # activities first: mileage matters most for load ratios
        chunk_end = end
        while chunk_end > start:
            chunk_start = max(start, chunk_end - dt.timedelta(days=BACKFILL_MAX_DAYS))
            db.add(GarminBackfill(connection_id=conn.id, kind=kind, start_at=chunk_start, end_at=chunk_end,
                                  not_before=end + delay))
            chunk_end = chunk_start
    msg = (f"Queued a request to Garmin for {conn.athlete.name}'s runs from the past {days} days. "
           "They arrive over the next few minutes to hours.")
    log(db, "garmin", msg)
    return msg


def _send_backfill(db: Session, job: GarminBackfill, http: httpx.Client) -> None:
    """Send one queued request to Garmin and record what happened."""
    conn = job.connection
    span = f"{job.kind} {job.start_at:%b %-d}-{job.end_at:%b %-d}"
    if conn is None or conn.status != "connected":
        job.status, job.last_result = "failed", "not connected to Garmin"
        return
    name = conn.athlete.name
    job.attempts += 1
    try:
        token = access_token(db, conn, http)
        if not conn.permissions:  # read right after connecting, Garmin may not have known the athlete yet
            try:
                conn.permissions = ",".join(_permissions_list(_api_get(http, token, "/wellness-api/rest/user/permissions")))
                log(db, "garmin", f"{name}'s Garmin permissions: {conn.permissions or 'none'}.")
            except GarminError:
                pass
        r = http.get(settings().garmin_api_base.rstrip("/") + BACKFILL_PATH.format(kind=job.kind),
                     params={BACKFILL_START: int(job.start_at.replace(tzinfo=dt.timezone.utc).timestamp()),
                             BACKFILL_END: int(job.end_at.replace(tzinfo=dt.timezone.utc).timestamp())},
                     headers={"Authorization": f"Bearer {token}"})
        code, said = r.status_code, _reply(r)
    except (GarminError, httpx.HTTPError) as exc:
        code, said = "no answer", f" {exc}"
    if code in (200, 202, 409):
        job.status, job.last_result = "sent", str(code)
        log(db, "garmin", f"Garmin accepted the request for {name}'s {span}" + (" (already requested)." if code == 409 else "."))
    elif job.attempts < BACKFILL_MAX_ATTEMPTS:
        job.last_result = f"{code}.{said}".rstrip(".")
        job.not_before = _now() + BACKFILL_RETRY
        log(db, "garmin", f"Garmin was not ready for {name}'s {span} ({job.last_result}); trying again in a few minutes.", "warn")
    else:
        job.status, job.last_result = "failed", f"{code}.{said}".rstrip(".")
        log(db, "garmin", f"Gave up asking Garmin for {name}'s {span} after {job.attempts} tries ({job.last_result}).", "error")


def process_backfills(db: Session, http: httpx.Client, sleep=time.sleep, budget_s: float = 840) -> int:
    """Send queued requests that are due, at most one per BACKFILL_SPACING_S, waiting for ones due within the budget.
    Commits after each request. Returns how many requests went to Garmin."""
    deadline = _now() + dt.timedelta(seconds=budget_s)
    sent = 0
    while True:
        job = db.scalar(select(GarminBackfill).where(GarminBackfill.status == "waiting")
                        .order_by(GarminBackfill.not_before, GarminBackfill.id).limit(1))
        if job is None or job.not_before > deadline:
            return sent
        wait = (job.not_before - _now()).total_seconds()
        if sent:
            wait = max(wait, BACKFILL_SPACING_S)
        if wait > 0:
            if _now() + dt.timedelta(seconds=wait) > deadline:
                return sent
            sleep(wait)
        _send_backfill(db, job, http)
        db.commit()
        sent += 1


def access_token(db: Session, conn: GarminConnection, http: httpx.Client) -> str:
    if conn.status == "revoked":
        raise GarminError("This athlete disconnected Garmin.")
    if conn.access_expires_at is None or conn.access_expires_at - REFRESH_MARGIN <= _now():
        refresh = decrypt(conn.refresh_token_enc)
        if not refresh:
            raise GarminError("No refresh token stored; the athlete needs to reconnect.")
        try:
            body = _token_request(http, {"grant_type": "refresh_token", "refresh_token": refresh})
        except GarminError:
            conn.status = "error"
            raise
        _store_tokens(conn, body)
        conn.status = "connected"
        db.flush()
    return decrypt(conn.access_token_enc)


def disconnect(db: Session, conn: GarminConnection, http: httpx.Client) -> None:
    """Remove consent at Garmin (required when the app offers a disconnect) and wipe tokens."""
    try:
        token = access_token(db, conn, http)
        r = http.delete(settings().garmin_api_base.rstrip("/") + "/wellness-api/rest/user/registration",
                        headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400 and r.status_code != 401:
            log(db, "garmin", f"Garmin deregistration returned {r.status_code}.", "warn")
    except GarminError as exc:
        log(db, "garmin", f"Could not reach Garmin to deregister: {exc}", "warn")
    conn.status = "revoked"
    conn.access_token_enc = ""
    conn.refresh_token_enc = ""
    db.flush()


def refresh_expiring(db: Session, http: httpx.Client) -> int:
    n = 0
    soon = _now() + dt.timedelta(hours=12)
    for conn in db.scalars(select(GarminConnection).where(GarminConnection.status == "connected")).all():
        if conn.access_expires_at is None or conn.access_expires_at <= soon:
            try:
                conn.access_expires_at = _now()  # force a refresh now
                access_token(db, conn, http)
                n += 1
            except GarminError as exc:
                log(db, "garmin", f"Token refresh failed for {conn.athlete.name}: {exc}", "error")
    return n


# ---------- downloads ----------

def allowed_download(url: str) -> bool:
    u = urlparse(url)
    if "@" in u.netloc or u.scheme not in ("https", "http"):
        return False
    netloc = u.netloc.lower()
    for h in settings().garmin_file_hosts:
        if "://" in h:
            if f"{u.scheme}://{netloc}" == h.rstrip("/").lower():
                return True
        elif u.scheme == "https" and (u.hostname or "").lower() == h:
            return True
    return False


def download(http: httpx.Client, token: str, url: str) -> bytes:
    if not allowed_download(url):
        raise GarminError("Refusing to download from a host that is not Garmin's.")
    with http.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as r:
        if r.status_code != 200:
            raise GarminError(f"Garmin file download failed ({r.status_code}).")
        chunks, size = [], 0
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > MAX_FIT_BYTES:
                raise GarminError("Garmin file is larger than 25 MB.")
            chunks.append(chunk)
    return b"".join(chunks)


# ---------- converting Garmin JSON into activities ----------

def _is_running_type(activity_type: str | None) -> bool:
    return "RUNNING" in str(activity_type or "").upper()


def summary_to_parsed(summary: dict) -> ParsedActivity:
    start = dt.datetime.fromtimestamp(int(summary["startTimeInSeconds"]), tz=dt.timezone.utc)
    ident = summary.get("activityId") or summary.get("summaryId")
    return ParsedActivity(
        external_id=f"garmin:{ident}", start=start, duration_s=float(summary.get("durationInSeconds") or 0),
        distance_m=float(summary.get("distanceInMeters") or 0),
        sport=str(summary.get("activityType") or "RUNNING").lower(),
        avg_hr=summary.get("averageHeartRateInBeatsPerMinute"), max_hr=summary.get("maxHeartRateInBeatsPerMinute"),
        device=("Garmin " + summary["deviceName"]) if summary.get("deviceName") and not str(summary["deviceName"]).lower().startswith("garmin") else str(summary.get("deviceName") or ""),
    )


def details_to_parsed(details: dict) -> ParsedActivity:
    """Activity Details: a summary plus per-second samples and lap start times."""
    p = summary_to_parsed(details.get("summary") or details)
    t0 = int(p.start.timestamp())
    samples = sorted(
        (s for s in details.get("samples") or [] if s.get("startTimeInSeconds") is not None),
        key=lambda s: s["startTimeInSeconds"])
    p.hr_samples = [(float(s["startTimeInSeconds"] - t0), int(s["heartRate"])) for s in samples if s.get("heartRate")]
    lap_starts = sorted(int(l["startTimeInSeconds"]) for l in details.get("laps") or [] if l.get("startTimeInSeconds") is not None)
    if lap_starts and samples:
        end_t = t0 + int(p.duration_s or (samples[-1]["startTimeInSeconds"] - t0))
        bounds = lap_starts + [end_t]

        def dist_at(t):
            best = None
            for s in samples:
                if s["startTimeInSeconds"] <= t and s.get("totalDistanceInMeters") is not None:
                    best = s["totalDistanceInMeters"]
            return float(best or 0)
        for a, b in zip(bounds, bounds[1:]):
            if b <= a:
                continue
            hrs = [int(s["heartRate"]) for s in samples if a <= s["startTimeInSeconds"] < b and s.get("heartRate")]
            p.laps.append(ParsedLap(
                start=dt.datetime.fromtimestamp(a, tz=dt.timezone.utc), elapsed_s=float(b - a), timer_s=float(b - a),
                distance_m=max(0.0, dist_at(b) - dist_at(a)),
                avg_hr=(sum(hrs) / len(hrs)) if hrs else None, max_hr=max(hrs) if hrs else None,
            ))
    return p


# ---------- notifications ----------

def _connection(db: Session, user_id) -> GarminConnection | None:
    if not user_id:
        return None
    return db.scalar(select(GarminConnection).where(GarminConnection.garmin_user_id == str(user_id)))


def handle_notification(db: Session, payload: dict, http: httpx.Client) -> dict:
    counts = {"created": 0, "upgraded": 0, "duplicate": 0, "skipped": 0, "errors": 0}
    if not isinstance(payload, dict):
        raise GarminError("Notification body must be a JSON object.")
    for key, items in payload.items():
        if not isinstance(items, list):
            continue
        for item in items:
            try:
                _handle_item(db, key, item, http, counts)
            except Exception as exc:  # one bad item must not block the rest
                counts["errors"] += 1
                log(db, "garmin", f"{key}: could not process item for user {item.get('userId') if isinstance(item, dict) else '?'}: {exc}", "error")
    return counts


def _save(db, conn, parsed, source, counts):
    if not parsed.is_running and not _is_running_type(parsed.sport):
        counts["skipped"] += 1
        return
    _, outcome = ingest(db, conn.athlete, parsed, source)
    counts[outcome] += 1
    conn.last_sync_at = _now()


def _handle_item(db: Session, key: str, item: dict, http: httpx.Client, counts: dict) -> None:
    if not isinstance(item, dict):
        counts["skipped"] += 1
        return
    conn = _connection(db, item.get("userId"))
    if key == "deregistrations":
        if conn:
            conn.status = "revoked"
            conn.access_token_enc = ""
            conn.refresh_token_enc = ""
            log(db, "garmin", f"{conn.athlete.name} removed the team app in Garmin Connect.")
        return
    if conn is None:
        counts["skipped"] += 1
        log(db, "garmin", f"{key}: notification for an unknown Garmin user was ignored.", "warn")
        return
    if key == "userPermissionsChange":
        perms = item.get("permissions") or []
        conn.permissions = ",".join(str(p) for p in perms)
        if "ACTIVITY_EXPORT" not in perms:
            log(db, "garmin", f"{conn.athlete.name} stopped sharing activities.", "warn")
        return
    if conn.status == "revoked":
        counts["skipped"] += 1
        return
    if key == "activityFiles":
        if str(item.get("fileType", "FIT")).upper() != "FIT":
            counts["skipped"] += 1
            return
        if item.get("activityType") and not _is_running_type(item.get("activityType")):
            counts["skipped"] += 1
            return
        data = download(http, access_token(db, conn, http), item["callbackURL"])
        _save(db, conn, parse_fit(data), "garmin_file", counts)
    elif key in ("activities", "activityDetails"):
        if item.get("callbackURL"):
            if not allowed_download(item["callbackURL"]):
                raise GarminError("Refusing to fetch from a host that is not Garmin's.")
            r = http.get(item["callbackURL"], headers={"Authorization": f"Bearer {access_token(db, conn, http)}"})
            if r.status_code != 200:
                raise GarminError(f"Garmin {key} callback failed ({r.status_code}).")
            rows = r.json()
            rows = rows if isinstance(rows, list) else [rows]
        else:
            rows = [item]
        for row in rows:
            summary = row.get("summary") or row
            if not _is_running_type(summary.get("activityType")):
                counts["skipped"] += 1
                continue
            if key == "activityDetails":
                _save(db, conn, details_to_parsed(row), "garmin_details", counts)
            else:
                _save(db, conn, summary_to_parsed(summary), "garmin_summary", counts)
    else:
        counts["skipped"] += 1

"""Phone notifications on the web side (push.py does the sending): the service worker, each athlete's home-screen
manifest, their Turn on / Turn off / test buttons, and the coach's Send a notification box on the Reminder emails page."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import push as PU
from .config import settings
from .ingest import log
from .models import Athlete, PushSubscription

HERE_STATIC = Path(__file__).parent / "static"
# The push services browsers use. A subscription naming any other address is refused, so the app can't be made to
# send requests somewhere else.
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com")
PUSH_SUFFIXES = (".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")
MAX_PHONES = 5                       # per athlete


def endpoint_ok(endpoint: str) -> bool:
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and not u.username and not u.password and u.port in (None, 443) and len(endpoint) <= 2000
            and (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIXES)))


def _key_ok(text: str, size: int) -> bool:
    try:
        return len(PU.unb64u(text)) == size
    except (ValueError, TypeError):
        return False


def register(app, h: SimpleNamespace) -> None:
    flash, back, get_db, form_checked = h.flash, h.back, h.get_db, h.form_checked
    coach_or_admin, athlete_by_token = h.coach_or_admin, h.athlete_by_token

    @app.get("/sw.js")
    def service_worker():
        return Response((HERE_STATIC / "sw.js").read_bytes(), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/a/{token}/manifest.webmanifest")
    def manifest(token: str, db: Session = Depends(get_db, scope="function")):
        athlete_by_token(db, token)
        s = settings()
        body = {"name": s.team_name, "short_name": "XC", "start_url": f"/a/{token}", "scope": "/", "display": "standalone",
                "background_color": "#ffffff", "theme_color": "#A6192E",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
        return Response(json.dumps(body), media_type="application/manifest+json")

    @app.post("/a/{token}/push")
    async def push_on(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        endpoint, p256dh, auth = (str(f.get(k, "")).strip() for k in ("endpoint", "p256dh", "auth"))
        if not endpoint_ok(endpoint) or not _key_ok(p256dh, 65) or not _key_ok(auth, 16):
            return JSONResponse({"ok": False, "error": "This browser’s notification service isn’t one the app knows."}, 400)
        device = str(f.get("device", "")).strip()[:120]
        sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
        if sub is None:
            mine = db.scalars(select(PushSubscription).where(PushSubscription.athlete_id == a.id)
                              .order_by(PushSubscription.created_at)).all()
            for old in mine[:max(0, len(mine) - MAX_PHONES + 1)]:      # keep the newest few phones
                db.delete(old)
            sub = PushSubscription(endpoint=endpoint)
            db.add(sub)
        sub.athlete_id, sub.p256dh, sub.auth, sub.device = a.id, p256dh, auth, device
        db.flush()
        return {"ok": True}

    @app.post("/a/{token}/push/off")
    async def push_off(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        endpoint = str(f.get("endpoint", "")).strip()
        q = select(PushSubscription).where(PushSubscription.athlete_id == a.id)
        for sub in db.scalars(q.where(PushSubscription.endpoint == endpoint) if endpoint else q).all():
            db.delete(sub)
        return {"ok": True}

    @app.post("/a/{token}/push/test")
    async def push_test(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a = athlete_by_token(db, token)
        r = PU.send(db, [a.id], settings().team_name, f"Notifications are on, {a.first_name}. You’ll hear from your coaches here.",
                    f"{settings().base_url.rstrip('/')}/a/{token}")
        return {"ok": r["sent"] > 0, **r}

    @app.post("/reminders/notify")
    async def notify_team(request: Request, user=Depends(coach_or_admin, scope="function"),
                          db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        message = " ".join(str(f.get("message", "")).split())[:240]
        title = " ".join(str(f.get("title", "")).split())[:80] or settings().team_name
        team = str(f.get("team", "All"))
        if not message:
            flash(request, "Write a message first.", "error")
            return back("/reminders#notify")
        q = select(Athlete).where(Athlete.active.is_(True), Athlete.is_test.is_(False))
        if team in ("Boys", "Girls"):
            q = q.where(Athlete.gender == team)
        base = settings().base_url.rstrip("/")
        out = {"sent": 0, "gone": 0, "failed": 0}
        people = 0
        for a in db.scalars(q).all():
            r = PU.send(db, [a.id], title, message, f"{base}/a/{a.link_token}")
            out = {k: out[k] + r[k] for k in out}
            people += 1 if r["sent"] else 0
        log(db, "email", f"{user.username} sent a phone notification to {people} athletes ({team}): {message[:60]}")
        flash(request, f"Sent to {people} athlete{'' if people == 1 else 's'} ({out['sent']} phone{'' if out['sent'] == 1 else 's'})."
              + (f" {out['failed']} couldn’t be reached." if out["failed"] else "")
              + (f" {out['gone']} phone{' has' if out['gone'] == 1 else 's have'} turned notifications off and {'was' if out['gone'] == 1 else 'were'} removed." if out["gone"] else ""),
              "ok" if people else "error")
        return back("/reminders#notify")


def counts(db: Session) -> dict:
    """Athletes with notifications on, all and by team, for the coach's box."""
    rows = db.execute(select(Athlete.id, Athlete.gender).join(PushSubscription, PushSubscription.athlete_id == Athlete.id)
                      .where(Athlete.active.is_(True), Athlete.is_test.is_(False)).distinct()).all()
    return {"All": len(rows), "Boys": sum(1 for _, g in rows if g == "Boys"), "Girls": sum(1 for _, g in rows if g == "Girls")}

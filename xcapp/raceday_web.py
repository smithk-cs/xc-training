"""Race Day pages: coaches manage boards; the race app opens by code (tap code for coaches at the course, watch code
for families, read only). See raceday.py."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import live as LV
from . import raceday as RD
from .config import settings
from .ingest import log
from .models import Course, RaceBoard, RaceBoardRun, StaffUser
from .security import csrf_ok, session_mark

# The race pages show a YouTube livestream (watch view) and use the phone's camera (tap view); everything else stays
# as strict as the rest of the app.
RACE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob: mediastream:; frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
            "connect-src 'self' https://*.realtime.cloudflare.com wss://*.realtime.cloudflare.com https://*.cloudflarestorage.com; "
            "worker-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
# Live switching: phones and the program page connect to Cloudflare RealtimeKit (see live.py)
# Race Day films (camera and microphone) and checks the coach's spot (location), on this site only
RACE_PERMISSIONS = "camera=(self), microphone=(self), geolocation=(self), fullscreen=(self \"https://www.youtube-nocookie.com\" \"https://www.youtube.com\"), payment=(), usb=()"


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, form_checked, get_db = h.render, h.flash, h.back, h.form_checked, h.get_db
    any_staff, coach_or_admin = h.any_staff, h.coach_or_admin

    def manager(request: Request, db: Session) -> StaffUser | None:
        """The signed-in coach or admin who may set up races and run the clock (the Race Day section turned on)."""
        uid = request.session.get("uid")
        u = db.get(StaffUser, uid) if uid else None
        if u is not None and request.session.get("pv") != session_mark(u.password_hash):
            return None   # signed in before a password change
        return u if u is not None and u.role in ("admin", "coach") and u.sees("raceday") else None

    def board_or_404(db: Session, code: str, watch: bool = False) -> RaceBoard:
        b = RD.board_by_code(db, code, watch)
        if b is None:
            raise HTTPException(404, "This race link has changed. Ask a coach for the new one.")
        return b

    def json_out(data, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    # ---------- coaches: boards ----------

    @app.get("/race-day", response_class=HTMLResponse)
    def raceday_admin(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        boards = db.scalars(select(RaceBoard).order_by(RaceBoard.id.desc())).all()
        runs = {b.id: db.scalars(select(RaceBoardRun).where(RaceBoardRun.board_id == b.id).order_by(RaceBoardRun.id.desc())).all()
                for b in boards}
        base = str(request.base_url).rstrip("/")
        spots = {b.id: [(RD.loads(b.checkpoints, [])[i], (RD.loads(b.coaches, []) + [""] * 20)[i])
                        for i in RD.spot_order(RD.loads(b.checkpoints, []), b.spot_order)] for b in boards}
        return render(request, "raceday_admin.html", user, boards=boards, runs=runs, base=base, loads=RD.loads, spots=spots)

    @app.post("/race-day/new")
    async def raceday_new(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        label = str(f.get("label", "")).strip()[:120] or "Race Day"
        db.add(RaceBoard(label=label, tap_code=RD.new_code(), watch_code=RD.new_code()))
        log(db, "admin", f"{user.username} made the Race Day board {label}.")
        flash(request, f"Made {label}. Open it to set up the first race.")
        return back("/race-day")

    @app.post("/race-day/{bid}/codes")
    async def raceday_codes(request: Request, bid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        b = db.get(RaceBoard, bid)
        if b is None:
            raise HTTPException(404, "Not found.")
        LV.stop_stream(b)   # the stream films a page under the old watch link
        b.tap_code, b.watch_code = RD.new_code(), RD.new_code()
        flash(request, "Made new links. The old ones stop working.")
        return back("/race-day")

    @app.post("/race-day/{bid}/delete")
    async def raceday_delete(request: Request, bid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        b = db.get(RaceBoard, bid)
        if b is not None:
            LV.stop_stream(b)
            db.delete(b)
            log(db, "admin", f"{user.username} deleted the Race Day board {b.label}.")
            flash(request, f"Deleted {b.label}.")
        return back("/race-day")

    # ---------- the race app ----------

    def app_page(request: Request, db: Session, board: RaceBoard, mode: str) -> HTMLResponse:
        m = manager(request, db) if mode == "tap" else None
        courses = [{"id": c.id, "name": c.name, "map_url": c.map_url}
                   for c in db.scalars(select(Course).order_by(Course.name)).all()] if m else []
        cfg = {"mode": mode, "api": f"/rw/{board.watch_code}" if mode == "watch" else f"/rd/{board.tap_code}",
               "canManage": m is not None, "label": board.label, "courses": courses,
               "staff": sorted({u.display_name for u in db.scalars(select(StaffUser)).all()}) if m else [],
               "watchLink": f"/rw/{board.watch_code}" if m else ""}
        resp = render(request, "raceday_app.html", None, board=board, mode=mode, cfg=cfg)
        resp.headers["Content-Security-Policy"] = RACE_CSP
        resp.headers["Permissions-Policy"] = RACE_PERMISSIONS
        return resp

    @app.get("/rd/{code}", response_class=HTMLResponse)
    def raceday_tap(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        return app_page(request, db, board_or_404(db, code), "tap")

    @app.get("/rw/{code}", response_class=HTMLResponse)
    def raceday_watch(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        return app_page(request, db, board_or_404(db, code, watch=True), "watch")

    @app.get("/rd/{code}/state")
    def raceday_state(request: Request, code: str, race: str = "", db: Session = Depends(get_db, scope="function")):
        b = board_or_404(db, code)
        return json_out({**RD.state(db, b, race or None), "can_manage": manager(request, db) is not None, "live": LV.status(b)})

    @app.get("/rw/{code}/state")
    def raceday_watch_state(code: str, race: str = "", db: Session = Depends(get_db, scope="function")):
        b = board_or_404(db, code, watch=True)
        return json_out({**RD.state(db, b, race or None), "can_manage": False, "live": LV.status(b)})

    @app.get("/rw/{code}/program", response_class=HTMLResponse)
    def raceday_program(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        """What goes out on the YouTube stream: the phone on air, the race clock and team scores. Cloudflare's recorder
        opens this page with its own join token."""
        b = board_or_404(db, code, watch=True)
        resp = render(request, "raceday_program.html", None, board=b, cfg={"api": f"/rw/{b.watch_code}", "label": b.label})
        resp.headers["Content-Security-Policy"] = RACE_CSP
        resp.headers["Permissions-Policy"] = RACE_PERMISSIONS
        return resp

    async def body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > 300_000:
            raise HTTPException(413, "Too large.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "Body must be JSON.")
        if not isinstance(data, dict):
            raise HTTPException(400, "Body must be a JSON object.")
        return data

    @app.post("/rd/{code}/log")
    async def raceday_log(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        """A phone's taps at its spot. Anyone with the tap code may send them."""
        b = board_or_404(db, code)
        try:
            RD.save_log(db, b, await body(request))
        except ValueError as exc:
            return json_out({"error": str(exc)}, 400)
        return json_out({"ok": True, "now": RD.now_ms()})

    def managed(request: Request, db: Session, data: dict) -> StaffUser:
        m = manager(request, db)
        if m is None:
            raise HTTPException(403, "Sign in to the app as a coach to set up races and run the clock.")
        if not csrf_ok(request.session, data.get("csrf")):
            raise HTTPException(400, "Your session expired. Reload the page and try again.")
        return m

    @app.post("/rd/{code}/race")
    async def raceday_race(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        b = board_or_404(db, code)
        data = await body(request)
        managed(request, db, data)
        try:
            RD.save_race(db, b, data, bool(data.get("fresh")))
        except ValueError as exc:
            return json_out({"error": str(exc)}, 400)
        return json_out({"ok": True, "race_id": b.race_id})

    @app.post("/rd/{code}/clock")
    async def raceday_clock(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        """Start (at the moment the coach pressed, on the server's clock), nudge by seconds, or clear."""
        b = board_or_404(db, code)
        data = await body(request)
        managed(request, db, data)
        if not b.race_id:
            return json_out({"error": "Set up the race first."}, 400)
        now, action = RD.now_ms(), data.get("action")
        if action == "start":
            at = data.get("at")
            # The page sends when the button was pressed, on the server's clock; trust it within half a minute
            b.start_at = int(at) if isinstance(at, (int, float)) and abs(at - now) <= 30_000 else now
        elif action == "nudge" and isinstance(data.get("ms"), int) and b.start_at and abs(data["ms"]) <= 60_000:
            b.start_at += data["ms"]
        elif action == "clear":
            b.start_at = None
        else:
            return json_out({"error": "Unknown clock action."}, 400)
        b.updated_ms = now
        return json_out({"ok": True, "start_at": b.start_at})

    # ---------- live switching (live.py) ----------

    def live_call(fn):
        try:
            return fn()
        except LV.LiveError as exc:
            return json_out({"error": str(exc)}, 400)

    @app.post("/rd/{code}/live/token")
    async def raceday_live_token(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        """A token to join the board's video room: as a camera (anyone with the tap link) or as the director (a coach)."""
        b = board_or_404(db, code)
        data = await body(request)
        role, device = data.get("role"), str(data.get("device") or "")
        if role not in ("camera", "director") or not RD.ID_RE.match(device):
            return json_out({"error": "Bad request."}, 400)
        if role == "director":
            managed(request, db, data)
        name = str(data.get("name") or "").strip()[:60]
        return live_call(lambda: json_out({"token": LV.join_token(b, device, role, name)}))

    @app.post("/rd/{code}/live/air")
    async def raceday_live_air(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        """Put a phone on the stream (its device id), or nobody ("")."""
        b = board_or_404(db, code)
        data = await body(request)
        managed(request, db, data)
        device = str(data.get("device") or "")
        if device and not RD.ID_RE.match(device):
            return json_out({"error": "Bad request."}, 400)
        b.on_air = device
        b.updated_ms = RD.now_ms()
        return json_out({"ok": True, "live": LV.status(b)})

    @app.post("/rd/{code}/live/key")
    async def raceday_live_key(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        b = board_or_404(db, code)
        data = await body(request)
        managed(request, db, data)
        return live_call(lambda: (LV.set_key(b, str(data.get("key") or "")), json_out({"ok": True, "live": LV.status(b)}))[1])

    @app.post("/rd/{code}/live/stream")
    async def raceday_live_stream(request: Request, code: str, db: Session = Depends(get_db, scope="function")):
        """Start or stop sending the program to YouTube."""
        b = board_or_404(db, code)
        data = await body(request)
        user = managed(request, db, data)
        action = data.get("action")
        if action == "start":
            url = f"{settings().base_url.rstrip('/')}/rw/{b.watch_code}/program"
            out = live_call(lambda: (LV.start_stream(b, url, RD.now_ms()), None)[1])
            if out is not None:
                return out
            log(db, "admin", f"{user.username} started the YouTube stream for {b.label}.")
        elif action == "stop":
            LV.stop_stream(b)
            log(db, "admin", f"{user.username} stopped the YouTube stream for {b.label}.")
        else:
            return json_out({"error": "Unknown action."}, 400)
        return json_out({"ok": True, "live": LV.status(b)})

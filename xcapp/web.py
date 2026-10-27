"""Web application. Start with:  uvicorn --factory xcapp.web:create_app"""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from . import ai as AI
from . import analysis as A
from . import datasets as DS
from . import garmin as G
from . import merge as MG
from . import photos as P
from .config import settings
from .db import SessionLocal, create_all
from .fitfile import MAX_FITS_PER_UPLOAD, MAX_UPLOAD_BYTES, FitError, fit_files, parse_fit
from .imports import GROUPS, ImportErrors, clean_email, import_goals, import_races, import_results, import_roster, parse_date, set_goal
from .ingest import ingest, log, rematch_day
from .models import (Activity, AiRun, AppSetting, Athlete, DeletionRequest, GarminConnection, Injury, MissedDay,
                     OAuthState, PainReport, PlannedWorkout, RaceResult, RestingHeartRate, Result, StaffUser, AthletePhoto, SyncLog, TrainingGroup, utcnow)
from .security import csrf_ok, csrf_token, hash_password, new_link_token, verify_password

HERE = Path(__file__).parent
BACKFILL_IN_BACKGROUND = True  # tests switch this off and run G.process_backfills themselves
INJURY_TYPES = ["Medial tibial stress (shin splints)", "Tibial bone stress injury", "Other bone stress injury",
                "Achilles tendinopathy", "Plantar fasciitis", "IT band syndrome", "Patellofemoral pain",
                "Hamstring strain", "Calf strain", "Hip flexor strain", "Ankle sprain", "Illness", "Other"]
BODY_AREAS = ["Foot", "Ankle", "Lower leg", "Knee", "Thigh", "Hip", "Back", "Other"]
ROLES = ("admin", "coach", "trainer")
FEELS = ("Fresh", "Normal", "Heavy", "Sore")
EFFORT_WORDS = {0: "Rest", 1: "Very easy", 2: "Easy", 3: "Moderate", 4: "Somewhat hard", 5: "Hard", 7: "Very hard", 10: "Max"}
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}
MAX_LOGO_BYTES = 1024 * 1024
# The uploaded logo lives in app_settings (type and base64 data) so it survives hosts without a lasting disk.
LOGO_TYPE_KEY, LOGO_DATA_KEY = "team_logo_type", "team_logo"
# Served images are inert: no scripts (an SVG can carry them), no loads, and sandboxed if opened directly.
LOGO_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def logo_type_of(raw: bytes) -> str | None:
    """The image type from the file's contents, not its name."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    head = raw[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<svg" in head:
        return "image/svg+xml"
    return None


def get_db():
    """Commit before the response is sent (scope="function"), so a redirect never shows stale data."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


class _LoginLimiter:
    def __init__(self, limit=5, window=600):
        self.limit, self.window, self.fails = limit, window, defaultdict(deque)

    def blocked(self, key: str) -> bool:
        q = self.fails[key]
        while q and q[0] < time.time() - self.window:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, key: str):
        self.fails[key].append(time.time())

    def clear(self, key: str):
        self.fails.pop(key, None)


def ink(color: str | None) -> str:
    """Dark or white text, whichever reads better on this background color (WCAG contrast)."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (color or "").strip())
    if not m:
        return "#1B1B1F"

    def lum(hex6):
        def ch(c):
            c = int(c, 16) / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (ch(hex6[i:i + 2]) for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    bg = lum(m.group(1))
    return "#1B1B1F" if (bg + 0.05) / (lum("1B1B1F") + 0.05) >= (1.05) / (bg + 0.05) else "#FFFFFF"


def clean_username(raw) -> str:
    name = str(raw or "").strip().lower()
    if not name or not name.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise ValueError("Usernames can use letters, numbers, dots, dashes and underscores.")
    return name


def create_app() -> FastAPI:
    s = settings()
    problems = s.problems()
    if problems:
        raise RuntimeError("Fix these settings before starting:\n- " + "\n- ".join(problems))
    create_all()

    app = FastAPI(title="XC Training", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, https_only=s.secure_cookies,
                       same_site="lax", max_age=12 * 3600, session_cookie="xc_session")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    logo_path = Path(s.team_logo_file) if s.team_logo_file else None
    file_logo = bool(logo_path and logo_path.is_file() and logo_path.suffix.lower() in LOGO_TYPES)

    def has_logo() -> bool:
        if file_logo:
            return True
        with SessionLocal() as db:
            return db.get(AppSetting, LOGO_TYPE_KEY) is not None
    templates.env.globals.update(fmt_time=A.fmt_time, timedelta=dt.timedelta, EVENTS=list(A.EVENTS), GROUPS=GROUPS, team_name=s.team_name,
                                 has_logo=has_logo, FEELS=FEELS, EFFORT_WORDS=EFFORT_WORDS, RUN_TYPES=A.RUN_TYPES,
                                 MISSED_REASONS=A.MISSED_REASONS, SIDES=A.SIDES, STARTED=A.STARTED, SEEN_BY=A.SEEN_BY,
                                 MORE_DAYS=A.MORE_DAYS, missed_label=A.missed_label,
                                 run_type_of=A.run_type_of, ink=ink, fmt_gap=A.fmt_gap, PACE_TYPES=A.PACE_TYPES,
                                 pace_type=A.pace_type)
    limiter = _LoginLimiter()
    garmin_origin = "{0.scheme}://{0.netloc}".format(urlparse(s.garmin_authorize_url))
    dashboard_body = (HERE / "static" / "dashboard_body.html").read_text(encoding="utf-8")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                f"img-src 'self' data:; frame-ancestors 'none'; form-action 'self' {garmin_origin}; base-uri 'none'")
        if not request.url.path.startswith("/static"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ---------- helpers ----------

    def flash(request: Request, text: str, level: str = "ok"):
        # Reassign (not append in place): the session only saves keys that are set.
        request.session["flash"] = list(request.session.get("flash", [])) + [[level, text]]

    def render(request: Request, name: str, user: StaffUser | None = None, status_code: int = 200, **ctx):
        msgs = request.session.pop("flash", [])
        return templates.TemplateResponse(request, name, {
            "user": user, "csrf": csrf_token(request.session), "messages": msgs, "settings": s, **ctx}, status_code=status_code)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    async def form_checked(request: Request) -> dict:
        form = await request.form()
        if not csrf_ok(request.session, form.get("csrf")):
            raise HTTPException(400, "Your session expired. Go back, reload the page and try again.")
        return form

    def current_user(request: Request, db: Session = Depends(get_db, scope="function")) -> StaffUser | None:
        uid = request.session.get("uid")
        return db.get(StaffUser, uid) if uid else None

    def staff(*roles: str):
        def dep(request: Request, user: StaffUser | None = Depends(current_user, scope="function")) -> StaffUser:
            if user is None:
                raise HTTPException(303, headers={"Location": "/login"})
            if roles and user.role not in roles:
                raise HTTPException(403, "Your account does not have access to this page.")
            return user
        return dep

    any_staff = staff()
    coach_or_admin = staff("admin", "coach")
    medical = staff("admin", "trainer", "coach")

    def athlete_by_token(db: Session, token: str) -> Athlete:
        a = db.scalar(select(Athlete).where(Athlete.link_token == token, Athlete.active.is_(True)))
        if a is None:
            raise HTTPException(404, "This athlete link is not valid. Ask your coach for a new one.")
        return a

    def photo_versions(db: Session) -> dict[int, int]:
        """athlete id -> a number that changes when their photo does (for cache-safe image links)."""
        return {aid: int(ts.timestamp()) for aid, ts in db.execute(select(AthletePhoto.athlete_id, AthletePhoto.updated_at)).all()}

    def photo_response(a: Athlete) -> Response:
        if a.photo is None:
            raise HTTPException(404, "No photo.")
        return Response(a.photo.data, media_type=a.photo.media_type,
                        headers={"Cache-Control": "private, max-age=86400", "Content-Security-Policy": LOGO_CSP})

    def delete_by(a: Athlete) -> dt.date | None:
        """When a former athlete's data is due for deletion under the privacy policy."""
        return a.left_on + dt.timedelta(days=s.retention_days) if a.left_on else None

    def athletes_sorted(db: Session):
        return db.scalars(select(Athlete).where(Athlete.active.is_(True)).order_by(Athlete.last_name, Athlete.first_name)).all()

    def get_athlete(db: Session, athlete_id) -> Athlete:
        try:
            a = db.get(Athlete, int(athlete_id))
        except (TypeError, ValueError):
            a = None
        if a is None:
            raise HTTPException(404, "Athlete not found.")
        return a

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (301, 302, 303, 307):
            return RedirectResponse(exc.headers["Location"], status_code=exc.status_code)
        if request.url.path.startswith(("/api/", "/garmin/webhook", "/cron/")):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "message.html", {
            "user": None, "csrf": "", "messages": [], "settings": s,
            "title": "Something needs attention", "text": exc.detail}, status_code=exc.status_code)

    # ---------- auth ----------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, db: Session = Depends(get_db, scope="function")):
        first_run = db.scalar(select(func.count(StaffUser.id))) == 0
        return render(request, "login.html", first_run=first_run, setup_ready=bool(s.setup_code))

    @app.post("/setup")
    async def first_admin(request: Request, db: Session = Depends(get_db, scope="function")):
        """Create the first admin from the sign-in page (hosts without a command line). Gone once any staff account exists."""
        form = await form_checked(request)
        if not s.setup_code or db.scalar(select(func.count(StaffUser.id))):
            raise HTTPException(404, "Not found.")
        key = request.client.host if request.client else "?"
        if limiter.blocked(key):
            flash(request, "Too many attempts. Wait 10 minutes and try again.", "error")
            return back("/login")
        if not hmac.compare_digest(str(form.get("setup_code", "")).strip().encode(), s.setup_code.encode()):
            limiter.fail(key)
            flash(request, "That setup code is not right.", "error")
            return back("/login")
        try:
            user = StaffUser(username=clean_username(form.get("username")), role="admin",
                             password_hash=hash_password(str(form.get("password", ""))))
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back("/login")
        db.add(user)
        db.flush()
        log(db, "admin", f"First admin account {user.username} created on the sign-in page.")
        limiter.clear(key)
        request.session.clear()
        request.session["uid"] = user.id
        flash(request, f"Welcome, {user.username}. Add your other staff under Staff.")
        return back("/")

    @app.post("/login")
    async def login(request: Request, db: Session = Depends(get_db, scope="function")):
        form = await form_checked(request)
        key = request.client.host if request.client else "?"
        if limiter.blocked(key):
            flash(request, "Too many attempts. Wait 10 minutes and try again.", "error")
            return back("/login")
        user = db.scalar(select(StaffUser).where(StaffUser.username == str(form.get("username", "")).strip().lower()))
        if user is None or not verify_password(str(form.get("password", "")), user.password_hash):
            limiter.fail(key)
            flash(request, "That username and password did not match.", "error")
            return back("/login")
        limiter.clear(key)
        request.session.clear()
        request.session["uid"] = user.id
        return back("/")

    @app.post("/logout")
    async def logout(request: Request):
        await form_checked(request)
        request.session.clear()
        return back("/login")

    # ---------- home ----------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        snap = DS.snapshot(db)
        week_ago = A.team_today() - dt.timedelta(days=7)
        stats = {
            "athletes": len(snap["roster"]),
            "connected": db.scalar(select(func.count(GarminConnection.id)).where(GarminConnection.status == "connected")),
            "activities_7d": db.scalar(select(func.count(Activity.id)).where(Activity.local_date > week_ago)),
        }
        last_ai = db.scalar(select(AiRun).where(AiRun.kind == "briefing").order_by(AiRun.id.desc()))
        webhook = f"{s.base_url.rstrip('/')}/garmin/webhook/{s.garmin_webhook_secret}" if user.role == "admin" and s.garmin_webhook_secret else ""
        deletions = db.scalars(select(DeletionRequest).where(DeletionRequest.status == "open").order_by(DeletionRequest.created_at)).all()
        soon = A.team_today() + dt.timedelta(days=14)
        due = [(a, delete_by(a)) for a in db.scalars(select(Athlete).where(Athlete.active.is_(False), Athlete.left_on.is_not(None))).all()
               if delete_by(a) <= soon]
        return render(request, "home.html", user, snap=snap, stats=stats, last_ai=last_ai, webhook=webhook, deletions=deletions, due=due,
                      rhr_flags=A.resting_hr_flags(db, A.team_today()), rhr_flag=A.RESTING_HR_FLAG)

    # ---------- dashboard ----------

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        snap = DS.snapshot(db)
        first = (snap["watchlist"] or snap["roster"] or [{}])[0].get("athlete_id")
        latest = max(snap["segments"], key=lambda r: r["date"], default=None)
        params = {"athlete": first, "event": "5K", "workout": latest["workout_id"] if latest else None}
        return render(request, "dashboard.html", user, body=dashboard_body, params=json.dumps(params))

    @app.get("/api/datasets/{name}.csv")
    def dataset_csv(name: str, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        if name not in DS.COLUMNS:
            raise HTTPException(404, "Unknown dataset.")
        return PlainTextResponse(DS.to_csv(name, DS.build(db, name)), media_type="text/csv; charset=utf-8")

    # ---------- results charts (XC Stats-style) ----------

    @app.get("/charts", response_class=HTMLResponse)
    def charts_page(request: Request, user=Depends(any_staff, scope="function")):
        return render(request, "charts.html", user)

    @app.get("/api/charts/results.json")
    def charts_results(user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        return JSONResponse({"results": A.benchmark_rows(db), "roster": A.roster_rows(db)})

    # ---------- athletes ----------

    @app.get("/athletes/links.csv")
    def athlete_links(user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf, lineterminator="\n")
        w.writerow(["athlete", "grade", "email", "garmin", "personal_link"])
        for a in db.scalars(select(Athlete).where(Athlete.active.is_(True)).options(selectinload(Athlete.garmin))
                            .order_by(Athlete.last_name, Athlete.first_name)).all():
            status = a.garmin.status if a.garmin else "not connected"
            w.writerow([a.name, a.grade, a.email, status, f"{s.base_url.rstrip('/')}/a/{a.link_token}"])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="athlete-links.csv"'})

    @app.get("/athletes", response_class=HTMLResponse)
    def athletes_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.scalars(select(Athlete).where(Athlete.active.is_(True)).options(selectinload(Athlete.garmin))
                          .order_by(Athlete.last_name, Athlete.first_name)).all()
        ranks = {g.name: g.position for g in db.scalars(select(TrainingGroup)).all()}
        former = db.scalars(select(Athlete).where(Athlete.active.is_(False)).order_by(Athlete.left_on.desc(), Athlete.last_name)).all()
        return render(request, "athletes.html", user, athletes=rows, ab=A.abilities(db, A.team_today()), ranks=ranks,
                      photos=photo_versions(db), former=former, delete_by=lambda a: delete_by(a),
                      dupes=MG.likely_duplicates(list(rows) + list(former)))

    @app.post("/athletes")
    async def add_athlete(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            first, last = str(f.get("first_name", "")).strip(), str(f.get("last_name", "")).strip()
            if not first or not last:
                raise ValueError("First and last name are required.")
            grade = int(f.get("grade", 0))
            if not 6 <= grade <= 12:
                raise ValueError("Grade should be 6 to 12.")
            gender, group = f.get("gender"), f.get("group")
            if gender not in ("Boys", "Girls") or group not in GROUPS:
                raise ValueError("Pick a team and a group.")
            hr_max = int(f["hr_max"]) if f.get("hr_max") else None
            db.add(Athlete(first_name=first, last_name=last, gender=gender, grade=grade, group=group,
                           hr_max=hr_max, link_token=new_link_token()))
            flash(request, f"Added {first} {last}.")
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back("/athletes")

    @app.post("/athletes/import")
    async def athletes_import(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        return await _import(request, f, lambda text: import_roster(db, text), "/athletes", "athletes")

    async def _import(request, form, fn, url, noun):
        up = form.get("file")
        if not up or not getattr(up, "filename", ""):
            flash(request, "Choose a CSV file first.", "error")
            return back(url)
        raw = await up.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            flash(request, "That file is larger than 5 MB.", "error")
            return back(url)
        try:
            n = fn(raw.decode("utf-8-sig", errors="replace"))
            flash(request, f"Imported {n} {noun}.")
        except ImportErrors as exc:
            flash(request, "Nothing was imported. Fix these rows and try again: " + "; ".join(exc.errors[:8]), "error")
        return back(url)

    @app.get("/athletes/{athlete_id}", response_class=HTMLResponse)
    def athlete_detail(request: Request, athlete_id: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).options(selectinload(Activity.laps), selectinload(Activity.planned))
                          .order_by(Activity.start_utc.desc()).limit(40)).all()
        results = db.scalars(select(Result).where(Result.athlete_id == a.id).order_by(Result.date.desc())).all()
        run_pain = {p.activity_id: p for p in a.pain_reports if p.activity_id}
        ability = A.abilities(db, A.team_today()).get(a.id, {})
        missed = db.scalars(select(MissedDay).where(MissedDay.athlete_id == a.id).order_by(MissedDay.date.desc()).limit(20)).all()
        group_names = [g.name for g in db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()]
        races = db.scalars(select(RaceResult).where(RaceResult.athlete_id == a.id).order_by(RaceResult.date.desc()).limit(30)).all()
        return render(request, "athlete_detail.html", user, a=a, acts=acts, results=results, run_pain=run_pain, races=races,
                      ability=ability, group_names=group_names, missed=missed, vo2=A.latest_vo2(db, a.id),
                      rhr=A.resting_hr(db, a.id, A.team_today()), photo_v=photo_versions(db).get(a.id),
                      others=db.scalars(select(Athlete).where(Athlete.id != a.id).order_by(Athlete.last_name, Athlete.first_name)).all(),
                      merge_pick=request.query_params.get("merge", ""),
                      injuries=sorted(a.injuries, key=lambda i: i.onset_date, reverse=True),
                      pains=sorted(a.pain_reports, key=lambda p: p.date, reverse=True)[:10],
                      goals=sorted(a.goals, key=lambda g: (g.season, g.event)), show_notes=user.role in ("admin", "trainer"))

    @app.post("/athletes/{athlete_id}/edit")
    async def athlete_edit(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        try:
            grade = int(f.get("grade", a.grade))
            if not 6 <= grade <= 12 or f.get("group") not in GROUPS:
                raise ValueError("Check the grade and group.")
            a.grade, a.group = grade, f.get("group")
            a.hr_max = int(f["hr_max"]) if f.get("hr_max") else None
            a.hr_rest = int(f["hr_rest"]) if f.get("hr_rest") else None
            a.email = clean_email(f.get("email", ""))
            flash(request, "Saved.")
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/garmin-backfill")
    async def athlete_garmin_backfill(request: Request, athlete_id: int, background: BackgroundTasks,
                                      user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        if not (a.garmin and a.garmin.status == "connected"):
            flash(request, f"{a.name} isn't connected to Garmin.", "error")
            return back(f"/athletes/{a.id}")
        try:
            days = int(f.get("days") or s.garmin_backfill_days or 90)
        except ValueError:
            flash(request, "Enter a number of days.", "error")
            return back(f"/athletes/{a.id}")
        msg = G.queue_backfill(db, a.garmin, days)
        if G.history_blocked(a.garmin):
            flash(request, msg, "error")
            return back(f"/athletes/{a.id}")
        flash(request, msg + " Progress shows in the Sync log.")
        background.add_task(kick_backfills)
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/new-link")
    async def athlete_new_link(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a = get_athlete(db, athlete_id)
        a.link_token = new_link_token()
        flash(request, f"Made a new link for {a.name}. The old link no longer works.")
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/upload")
    async def athlete_upload_staff(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        flash(request, *await _upload(db, a, f.getlist("file")))
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/status")
    async def athlete_status(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        action = f.get("action", "")
        if action == "test":
            a.is_test = bool(f.get("is_test"))
            flash(request, f"{a.name} is " + ("now a test athlete: left out of team numbers, the watch list, the VDOT page and the AI briefing."
                                               if a.is_test else "counted with the team again."))
        elif action == "leave" and a.active:
            if a.garmin and a.garmin.status != "revoked":
                with G.make_client() as http:
                    G.disconnect(db, a.garmin, http)
            a.active, a.left_on = False, A.team_today()
            log(db, "privacy", f"{user.username} marked {a.name} as having left the team.")
            flash(request, f"{a.name} is off the roster. Their personal link no longer works and Garmin is disconnected. "
                           f"Their data is kept until {delete_by(a):%b %-d, %Y}, when the privacy policy says to delete it.")
        elif action == "return" and not a.active:
            a.active, a.left_on = True, None
            flash(request, f"{a.name} is back on the roster. Their personal link works again; they can reconnect Garmin from it.")
        nxt = str(f.get("next", ""))
        return back(nxt if nxt in ("/athletes",) else f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/other-names")
    async def athlete_other_names(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        a.other_names = ""
        MG.add_other_names(a, [n for n in str(f.get("other_names", "")).replace(",", ";").split(";")])
        flash(request, "Saved. Uploads with " + ("these names now match " + a.name + "." if a.other_names else "only their own name match them."))
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/merge")
    async def athlete_merge(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        keep = get_athlete(db, athlete_id)
        try:
            other = get_athlete(db, f.get("other_id"))
        except HTTPException:
            flash(request, "Pick the athlete to combine with.", "error")
            return back(f"/athletes/{keep.id}")
        if other.id == keep.id or not f.get("confirm"):
            flash(request, "Pick a different athlete and tick the box to confirm.", "error")
            return back(f"/athletes/{keep.id}")
        if keep.garmin and other.garmin and other.garmin.status != "revoked":
            with G.make_client() as http:
                G.disconnect(db, other.garmin, http)
        name = other.name
        n = MG.merge(db, keep, other)
        log(db, "admin", f"{user.username} combined {name} into {keep.name} ({n['moved']} records moved, {n['dropped']} duplicates dropped).")
        flash(request, f"Combined {name} into {keep.name}: {n['moved']} records moved"
                       + (f", {n['dropped']} duplicates dropped" if n["dropped"] else "") + f". Uploads under “{name}” will now match {keep.name}.")
        return back(f"/athletes/{keep.id}")

    @app.post("/athletes/{athlete_id}/delete")
    async def athlete_delete(request: Request, athlete_id: int, user=Depends(staff("admin"), scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        if f.get("confirm_name", "").strip().lower() != a.name.lower():
            flash(request, "Type the athlete's full name to confirm.", "error")
            return back(f"/athletes/{a.id}")
        if a.garmin and a.garmin.status != "revoked":
            with G.make_client() as http:
                G.disconnect(db, a.garmin, http)
        name = a.name
        for req in db.scalars(select(DeletionRequest).where(DeletionRequest.athlete_id == a.id)).all():
            req.status, req.completed_at, req.completed_by, req.athlete_name = "done", utcnow(), user.username, name
        db.delete(a)
        log(db, "admin", f"{user.username} deleted all data for {name}.")
        flash(request, f"Deleted {name} and all of their data.")
        return back("/athletes")

    async def _upload(db: Session, a: Athlete, uploads) -> tuple[str, str]:
        """Add runs from one or more .fit files, or .zip files of them (such as Garmin's data export)."""
        uploads = [u for u in uploads if getattr(u, "filename", "")]
        if not uploads:
            return "Choose a .fit or .zip file first.", "error"
        files: list[tuple[str, bytes]] = []
        try:
            for up in uploads:
                raw = await up.read(MAX_UPLOAD_BYTES + 1)
                if len(raw) > MAX_UPLOAD_BYTES:
                    return f"{up.filename} is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Upload it in smaller parts.", "error"
                files.extend(fit_files(up.filename, raw))
                if len(files) > MAX_FITS_PER_UPLOAD:
                    raise FitError(f"That upload holds more than {MAX_FITS_PER_UPLOAD} activity files. Upload it in smaller parts.")
        except FitError as exc:
            return str(exc), "error"
        if not files:
            return "No .fit activity files were found in that upload.", "error"
        if len(files) == 1 and not files[0][0].lower().endswith(".zip"):
            return _upload_one(db, a, files[0][1])
        counts, dates = {"created": 0, "upgraded": 0, "duplicate": 0, "not_run": 0, "unreadable": 0}, []
        for _name, data in files:
            try:
                parsed = parse_fit(data)
            except FitError:
                counts["unreadable"] += 1
                continue
            if not parsed.is_running:
                counts["not_run"] += 1
                continue
            act, outcome = ingest(db, a, parsed, "upload")
            counts[outcome] += 1
            if outcome != "duplicate":
                dates.append(act.local_date)
        added = counts["created"] + counts["upgraded"]
        parts = [f"Added {added} run{'' if added == 1 else 's'}"
                 + (f" ({min(dates):%b %-d} to {max(dates):%b %-d})" if len(set(dates)) > 1 else "")]
        for key, text in (("duplicate", "already here"), ("not_run", "skipped because they weren't runs"),
                          ("unreadable", "could not be read")):
            if counts[key]:
                parts.append(f"{counts[key]} {text}")
        return ", ".join(parts) + ".", "ok" if added or counts["duplicate"] else "error"

    def _upload_one(db: Session, a: Athlete, raw: bytes) -> tuple[str, str]:
        try:
            parsed = parse_fit(raw)
        except FitError as exc:
            return str(exc), "error"
        if not parsed.is_running:
            return "That file is not a run, so it was not added.", "error"
        act, outcome = ingest(db, a, parsed, "upload")
        reps = sum(1 for l in act.laps if l.is_rep)
        if outcome == "duplicate":
            return "That run is already in the system.", "ok"
        msg = f"Added a {act.distance_m / A.METERS_PER_MILE:.1f} mile run from {act.local_date:%b %-d}"
        if reps:
            msg += f" with {reps} reps" + (f", matched to {act.planned.title}" if act.planned else "")
        return msg + ".", "ok"

    # ---------- results and goals ----------

    # ---------- races (full meet results at their real distances) ----------

    @app.get("/races", response_class=HTMLResponse)
    def races_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.scalars(select(RaceResult).options(selectinload(RaceResult.athlete))
                          .order_by(RaceResult.date.desc(), RaceResult.meet, RaceResult.race, RaceResult.place, RaceResult.time_s)).all()
        meets: dict[tuple, dict[tuple, list]] = {}
        for r in rows:
            meets.setdefault((r.date, r.meet, r.course), {}).setdefault((r.race, r.distance_label, r.distance_m), []).append(r)
        return render(request, "races.html", user, meets=meets)

    @app.post("/races/import")
    async def races_import(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        up = f.get("file")
        if not up or not getattr(up, "filename", ""):
            flash(request, "Choose a CSV file first.", "error")
            return back("/races")
        raw = await up.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            flash(request, "That file is larger than 5 MB.", "error")
            return back("/races")
        try:
            n, added = import_races(db, raw.decode("utf-8-sig", errors="replace"), add_athletes=bool(f.get("add_athletes")))
            flash(request, f"Imported {n} race result{'' if n == 1 else 's'}"
                  + (f" and added {added} athlete{'' if added == 1 else 's'} to the roster" if added else "") + ".")
            if added:
                log(db, "admin", f"{user.username} added {added} athlete{'' if added == 1 else 's'} from a race results import.")
        except ImportErrors as exc:
            flash(request, "Nothing was imported. Fix these rows and try again: " + "; ".join(exc.errors[:8]), "error")
        return back("/races")

    @app.post("/races/delete")
    async def races_delete(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            d = dt.date.fromisoformat(str(f.get("date", "")))
        except ValueError:
            raise HTTPException(400, "Bad date.")
        meet = str(f.get("meet", ""))
        rows = db.scalars(select(RaceResult).where(RaceResult.date == d, RaceResult.meet == meet)).all()
        for r in rows:
            if r.result_id:
                res = db.get(Result, r.result_id)
                if res is not None:
                    db.delete(res)
            db.delete(r)
        flash(request, f"Deleted {meet} ({len(rows)} results).")
        return back("/races")

    @app.get("/results", response_class=HTMLResponse)
    def results_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.execute(select(Result, Athlete).join(Athlete, Athlete.id == Result.athlete_id)
                          .order_by(Result.date.desc(), Result.id.desc()).limit(200)).all()
        cur = A.current_xc_season(A.benchmark_rows(db)) or A.season_for(A.team_today())
        return render(request, "results.html", user, rows=rows, athletes=athletes_sorted(db), season=cur,
                      today=A.team_today().isoformat())

    @app.post("/results")
    async def add_result(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            a = get_athlete(db, f.get("athlete_id"))
            event, factor = A.normalize_event(f.get("event", ""))
            d = parse_date(f.get("date", ""))
            kind = f.get("kind", "race")
            if kind not in ("race", "time trial", "track"):
                raise ValueError("Pick race, time trial or track.")
            t = A.parse_time(f.get("time", "")) * factor
            db.add(Result(athlete_id=a.id, event=event, time_s=round(t, 2), date=d, season=A.season_for(d),
                          course=str(f.get("course", ""))[:120], kind=kind,
                          note="" if factor == 1 else f"converted from {f.get('event')}"))
            flash(request, f"Saved {a.name}: {event} {A.fmt_time(t)}.")
        except (ValueError, HTTPException) as exc:
            flash(request, getattr(exc, "detail", None) or str(exc), "error")
        return back("/results")

    @app.post("/results/{result_id}/delete")
    async def delete_result(request: Request, result_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        r = db.get(Result, result_id)
        if r:
            db.delete(r)
            flash(request, "Result deleted.")
        return back("/results")

    @app.post("/results/import")
    async def results_import(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        return await _import(request, f, lambda text: import_results(db, text), "/results", "results")

    @app.post("/goals")
    async def add_goal(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            a = get_athlete(db, f.get("athlete_id"))
            event, factor = A.normalize_event(f.get("event", ""))
            season = str(f.get("season", "")).strip()
            if not season.endswith((" XC", " Track")):
                raise ValueError("Season should look like 2026 XC.")
            g = A.parse_time(f.get("goal", "")) * factor
            set_goal(db, a.id, event, season, round(g, 2))
            flash(request, f"Goal saved: {a.name}, {event} {A.fmt_time(g)} for {season}.")
        except (ValueError, HTTPException) as exc:
            flash(request, getattr(exc, "detail", None) or str(exc), "error")
        return back("/results")

    @app.post("/goals/import")
    async def goals_import(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        season = A.current_xc_season(A.benchmark_rows(db)) or A.season_for(A.team_today())
        return await _import(request, f, lambda text: import_goals(db, text, season), "/results", "goals")

    # ---------- planned workouts ----------

    @app.get("/workouts", response_class=HTMLResponse)
    def workouts_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.scalars(select(PlannedWorkout).order_by(PlannedWorkout.date.desc())).all()
        counts = dict(db.execute(select(Activity.planned_workout_id, func.count(Activity.id))
                                 .where(Activity.planned_workout_id.is_not(None)).group_by(Activity.planned_workout_id)).all())
        ability_names = [g.name for g in db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()]
        edit = db.get(PlannedWorkout, int(request.query_params["edit"])) if request.query_params.get("edit", "").isdigit() else None
        return render(request, "workouts.html", user, rows=rows, counts=counts, today=A.team_today().isoformat(),
                      ability_names=ability_names, edit=edit)

    @app.post("/workouts")
    async def add_workout(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            d = parse_date(f.get("date", ""))
            title = str(f.get("title", "")).strip()[:120]
            rep_m = float(f.get("rep_distance_m", 0))
            kind = f.get("pace_type", "Other")
            factor = A.PACE_TYPES[kind] if kind in A.PACE_TYPES else float(f.get("pace_factor", 1) or 1)
            event = f.get("pace_event", "5K")
            if not title or not 100 <= rep_m <= 10000 or event not in A.EVENTS or not 0.8 <= factor <= 1.5:
                raise ValueError("Check the title, rep distance (100 to 10000 m), pace event and pace factor (0.8 to 1.5).")
            ability_names = [g.name for g in db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()]
            groups = ",".join(g for g in ability_names if f.get(f"group_{g}")) or "All"
            pw = db.get(PlannedWorkout, int(f["wid"])) if str(f.get("wid", "")).isdigit() else None
            old_day = pw.date if pw else None
            if pw is None:
                pw = PlannedWorkout()
                db.add(pw)
            pw.date, pw.title, pw.rep_distance_m, pw.pace_event, pw.pace_factor, pw.groups = d, title, rep_m, event, factor, groups
            db.flush()
            n = rematch_day(db, d) + (rematch_day(db, old_day) if old_day and old_day != d else 0)
            flash(request, f"{'Updated' if old_day else 'Saved'} {title}." + (f" Re-matched {n} runs already synced." if n else ""))
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back("/workouts")

    @app.post("/workouts/{wid}/delete")
    async def delete_workout(request: Request, wid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        pw = db.get(PlannedWorkout, wid)
        if pw:
            for act in db.scalars(select(Activity).where(Activity.planned_workout_id == pw.id)).all():
                act.planned_workout_id = None
            db.delete(pw)
            flash(request, "Workout deleted.")
        return back("/workouts")

    # ---------- injuries and pain ----------

    @app.get("/injuries", response_class=HTMLResponse)
    def injuries_page(request: Request, user=Depends(medical, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.scalars(select(Injury).options(selectinload(Injury.athlete)).order_by(Injury.onset_date.desc())).all()
        pains = db.scalars(select(PainReport).options(selectinload(PainReport.athlete))
                           .where(PainReport.date >= A.team_today() - dt.timedelta(days=28)).order_by(PainReport.date.desc())).all()
        missed = db.scalars(select(MissedDay).options(selectinload(MissedDay.athlete))
                            .where(MissedDay.date >= A.team_today() - dt.timedelta(days=28)).order_by(MissedDay.date.desc())).all()
        return render(request, "injuries.html", user, rows=rows, pains=pains, missed=missed, athletes=athletes_sorted(db),
                      types=INJURY_TYPES, areas=BODY_AREAS, today=A.team_today(), show_notes=user.role in ("admin", "trainer"))

    @app.post("/injuries")
    async def add_injury(request: Request, user=Depends(medical, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            a = get_athlete(db, f.get("athlete_id"))
            onset = parse_date(f.get("onset_date", ""))
            ret = parse_date(f["return_date"]) if f.get("return_date") else None
            kind = f.get("injury") if f.get("injury") != "Other" else (str(f.get("injury_other", "")).strip() or "Other")
            area = f.get("body_area")
            if kind not in INJURY_TYPES and f.get("injury") != "Other":
                raise ValueError("Pick an injury type.")
            if area not in BODY_AREAS:
                raise ValueError("Pick a body area.")
            if ret and ret < onset:
                raise ValueError("The return date is before the injury date.")
            notes = str(f.get("notes", ""))[:2000] if user.role in ("admin", "trainer") else ""
            db.add(Injury(athlete_id=a.id, onset_date=onset, injury=str(kind)[:120], body_area=area, return_date=ret,
                          notes=notes, logged_by=user.username))
            flash(request, f"Logged {kind} for {a.name}.")
        except (ValueError, HTTPException) as exc:
            flash(request, getattr(exc, "detail", None) or str(exc), "error")
        return back("/injuries")

    @app.post("/injuries/{iid}/update")
    async def update_injury(request: Request, iid: int, user=Depends(medical, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        inj = db.get(Injury, iid)
        if inj is None:
            raise HTTPException(404, "Injury not found.")
        try:
            if f.get("return_date"):
                ret = parse_date(f["return_date"])
                if ret < inj.onset_date:
                    raise ValueError("The return date is before the injury date.")
                inj.return_date = ret
            if user.role in ("admin", "trainer") and "notes" in f:
                inj.notes = str(f.get("notes", ""))[:2000]
            flash(request, "Injury updated.")
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back("/injuries")

    @app.post("/injuries/{iid}/delete")
    async def delete_injury(request: Request, iid: int, user=Depends(staff("admin", "trainer"), scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        inj = db.get(Injury, iid)
        if inj:
            db.delete(inj)
            flash(request, "Injury deleted.")
        return back("/injuries")

    # ---------- AI ----------

    @app.get("/ai", response_class=HTMLResponse)
    def ai_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        brief = db.scalar(select(AiRun).where(AiRun.kind == "briefing", AiRun.status == "ok").order_by(AiRun.id.desc()))
        last = db.scalar(select(AiRun).where(AiRun.kind == "briefing").order_by(AiRun.id.desc()))
        qa = db.scalars(select(AiRun).where(AiRun.kind == "question").order_by(AiRun.id.desc()).limit(10)).all()
        return render(request, "ai.html", user, brief=brief, last=last, qa=qa, spent=AI.month_spend(db))

    @app.post("/ai/run")
    async def ai_run(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        run = AI.generate_briefing(db)
        flash(request, "New briefing ready." if run.status == "ok" else f"The briefing could not be made: {run.error}",
              "ok" if run.status == "ok" else "error")
        return back("/ai")

    @app.post("/ai/ask")
    async def ai_ask(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        run = AI.ask(db, str(f.get("question", "")))
        if run.status != "ok":
            flash(request, f"No answer: {run.error}", "error")
        return back("/ai")

    # ---------- staff ----------

    @app.get("/staff", response_class=HTMLResponse)
    def staff_page(request: Request, user=Depends(staff("admin"), scope="function"), db: Session = Depends(get_db, scope="function")):
        return render(request, "staff.html", user, rows=db.scalars(select(StaffUser).order_by(StaffUser.username)).all(), roles=ROLES,
                      uploaded_logo=db.get(AppSetting, LOGO_TYPE_KEY) is not None)

    @app.post("/staff")
    async def staff_add(request: Request, user=Depends(staff("admin"), scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            name = clean_username(f.get("username"))
            if f.get("role") not in ROLES:
                raise ValueError("Pick a role.")
            if db.scalar(select(StaffUser).where(StaffUser.username == name)):
                raise ValueError("That username is taken.")
            db.add(StaffUser(username=name, role=f.get("role"), email=clean_email(f.get("email", "")),
                             password_hash=hash_password(str(f.get("password", "")))))
            flash(request, f"Added {name}.")
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back("/staff")

    @app.post("/staff/{uid}/password")
    async def staff_password(request: Request, uid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        target = db.get(StaffUser, uid)
        if target is None or (user.role != "admin" and target.id != user.id):
            raise HTTPException(403, "You can only change your own password.")
        try:
            target.password_hash = hash_password(str(f.get("password", "")))
            flash(request, "Password changed.")
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back("/staff" if user.role == "admin" else "/")

    @app.post("/staff/{uid}/email")
    async def staff_email(request: Request, uid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        target = db.get(StaffUser, uid)
        if target is None or (user.role != "admin" and target.id != user.id):
            raise HTTPException(403, "You can only change your own email.")
        try:
            target.email = clean_email(f.get("email", ""))
            flash(request, f"Saved {target.username}'s email.")
        except ValueError as exc:
            flash(request, str(exc) + ".", "error")
        return back("/staff" if user.role == "admin" else "/")

    @app.post("/staff/{uid}/delete")
    async def staff_delete(request: Request, uid: int, user=Depends(staff("admin"), scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        target = db.get(StaffUser, uid)
        if target and target.id != user.id:
            db.delete(target)
            flash(request, f"Removed {target.username}.")
        elif target:
            flash(request, "You cannot remove your own account.", "error")
        return back("/staff")

    # ---------- sync log ----------

    @app.get("/sync", response_class=HTMLResponse)
    def sync_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        conns = db.scalars(select(GarminConnection).options(selectinload(GarminConnection.athlete))).all()
        logs = db.scalars(select(SyncLog).order_by(SyncLog.id.desc()).limit(150)).all()
        webhook = f"{s.base_url.rstrip('/')}/garmin/webhook/{s.garmin_webhook_secret}" if user.role == "admin" and s.garmin_webhook_secret else ""
        return render(request, "sync.html", user, conns=conns, logs=logs, webhook=webhook)

    # ---------- athlete link pages ----------

    @app.get("/a/{token}", response_class=HTMLResponse)
    def athlete_page(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).order_by(Activity.start_utc.desc()).limit(10)).all()
        today = A.team_today()
        open_delete = db.scalar(select(DeletionRequest).where(DeletionRequest.athlete_id == a.id, DeletionRequest.status == "open"))
        need = db.scalar(select(func.count(Activity.id)).where(
            Activity.athlete_id == a.id, Activity.rpe.is_(None), Activity.local_date > today - dt.timedelta(days=14)))
        roster = [r for r in A.roster_rows(db) if r["athlete_id"] == a.code]
        bench = [b for b in A.benchmark_rows(db) if b["athlete_id"] == a.code]
        team_goals = A.goal_rows(db)
        prog, seasons = A.progress(bench, [g for g in team_goals if g["athlete_id"] == a.code], roster)
        tiles = [p for p in prog if p["current"] is not None]
        show_group = A.get_setting(db, "show_group_to_athletes", "1") == "1"
        mine = A.abilities(db, today).get(a.id, {})
        ability = mine if show_group else {}
        coming = db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= today, PlannedWorkout.date <= today + dt.timedelta(days=7))
                            .order_by(PlannedWorkout.date, PlannedWorkout.id)).all()
        paces = A.workout_paces(db, a, [w for w in coming if w.applies_to(a.group, mine.get("group") or "")])
        races = db.scalars(select(RaceResult).where(RaceResult.athlete_id == a.id)
                           .order_by(RaceResult.date.desc(), RaceResult.id.desc())).all()
        history = A.race_history(races)
        return render(request, "athlete.html", None, history=history, paces=paces, a=a, acts=acts, tiles=tiles, seasons=seasons, need=need, ability=ability,
                      show_group=show_group, open_delete=open_delete, races=races, vo2=A.latest_vo2(db, a.id),
                      rhr=A.resting_hr(db, a.id, today), rhr_range=(A.RESTING_HR_MIN, A.RESTING_HR_MAX),
                      rhr_cal=A.resting_hr_calendar(db, a.id, today), photo_v=photo_versions(db).get(a.id), my_paces=A.training_paces(mine.get("vdot")) if show_group else [],
                      areas=BODY_AREAS, token=token, today=today.isoformat())

    @app.post("/a/{token}/connect")
    async def athlete_connect(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        if not f.get("consent"):
            flash(request, "A parent or guardian needs to approve first. Tick the box when they have.", "error")
            return back(f"/a/{token}")
        try:
            url = G.start_authorization(db, a, "Athlete confirmed parent/guardian approval on the team link page.")
        except G.GarminError as exc:
            flash(request, str(exc), "error")
            return back(f"/a/{token}")
        request.session["athlete_token"] = token
        return render(request, "garmin_go.html", None, url=url)

    @app.post("/a/{token}/disconnect")
    async def athlete_disconnect(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a = athlete_by_token(db, token)
        if a.garmin and a.garmin.status != "revoked":
            with G.make_client() as http:
                G.disconnect(db, a.garmin, http)
            log(db, "garmin", f"{a.name} disconnected Garmin.")
        flash(request, "Garmin is disconnected. Runs you already shared stay with your team.")
        return back(f"/a/{token}")

    @app.post("/a/{token}/upload")
    async def athlete_upload(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        flash(request, *await _upload(db, a, f.getlist("file")))
        return back(f"/a/{token}")

    @app.post("/a/{token}/pain")
    async def athlete_pain(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        try:
            sev = int(f.get("severity", -1))
            area = f.get("body_area")
            if not 0 <= sev <= 10 or area not in BODY_AREAS:
                raise ValueError("Pick where it hurts and how much (0 to 10).")
            db.add(PainReport(athlete_id=a.id, date=A.team_today(), body_area=area, severity=sev,
                              note=str(f.get("note", ""))[:300], saw_trainer=bool(f.get("saw_trainer"))))
            flash(request, "Thanks. Your coach will see this today." + (" Please also tell your coach or athletic trainer in person." if sev >= A.PAIN_FLAG else ""))
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back(f"/a/{token}")

    @app.post("/a/{token}/resting-hr")
    async def athlete_resting_hr(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        try:
            bpm = int(str(f.get("bpm", "")).strip())
        except ValueError:
            bpm = 0
        today = A.team_today()
        try:
            day = dt.date.fromisoformat(str(f.get("date") or today.isoformat()))
        except ValueError:
            day = None
        if day is None or not today - dt.timedelta(days=A.RESTING_HR_BACKDATE) <= day <= today:
            flash(request, f"Pick a day from the past {A.RESTING_HR_BACKDATE} days.", "error")
        elif not A.RESTING_HR_MIN <= bpm <= A.RESTING_HR_MAX:
            flash(request, f"Enter your resting heart rate as beats per minute, between {A.RESTING_HR_MIN} and {A.RESTING_HR_MAX}.", "error")
        else:
            A.save_resting_hr(db, a.id, day, bpm, "athlete")
            flash(request, f"Saved: {bpm} beats per minute " + ("this morning." if day == today else f"for {day:%a, %b %-d}."))
        return back(f"/a/{token}#rhr")

    # ---------- branding ----------

    @app.get("/branding/logo")
    def team_logo(db: Session = Depends(get_db, scope="function")):
        kind = db.get(AppSetting, LOGO_TYPE_KEY)
        data = db.get(AppSetting, LOGO_DATA_KEY)
        if kind and data:
            raw, media = base64.b64decode(data.value), kind.value
        elif file_logo:
            raw, media = logo_path.read_bytes(), LOGO_TYPES[logo_path.suffix.lower()]
        else:
            raise HTTPException(404, "No logo configured.")
        return Response(raw, media_type=media, headers={"Cache-Control": "public, max-age=300", "Content-Security-Policy": LOGO_CSP})

    @app.post("/branding/logo")
    async def team_logo_upload(request: Request, user=Depends(staff("admin"), scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        up = f.get("logo")
        if not up or not getattr(up, "filename", ""):
            flash(request, "Choose an image file first.", "error")
            return back("/staff")
        raw = await up.read(MAX_LOGO_BYTES + 1)
        media = logo_type_of(raw)
        if len(raw) > MAX_LOGO_BYTES:
            flash(request, "That image is over 1 MB. Save a smaller copy and try again.", "error")
        elif media is None:
            flash(request, "That file is not a PNG, JPEG, WebP or SVG image.", "error")
        else:
            for key, value in ((LOGO_DATA_KEY, base64.b64encode(raw).decode()), (LOGO_TYPE_KEY, media)):
                row = db.get(AppSetting, key)
                if row is None:
                    db.add(AppSetting(key=key, value=value))
                else:
                    row.value = value
            log(db, "admin", f"{user.username} uploaded a new team logo.")
            flash(request, "Logo updated. It now shows on every page.")
        return back("/staff")

    @app.post("/branding/logo/delete")
    async def team_logo_delete(request: Request, user=Depends(staff("admin"), scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        for key in (LOGO_TYPE_KEY, LOGO_DATA_KEY):
            row = db.get(AppSetting, key)
            if row is not None:
                db.delete(row)
        log(db, "admin", f"{user.username} removed the uploaded team logo.")
        flash(request, "Uploaded logo removed." + (" The logo file from the server settings shows instead." if file_logo else ""))
        return back("/staff")

    # ---------- per-athlete charts ----------

    @app.get("/api/athletes/{athlete_id}/series.json")
    def athlete_series_staff(athlete_id: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        return JSONResponse(A.athlete_series(db, get_athlete(db, athlete_id), A.team_today()))

    @app.get("/a/{token}/series.json")
    def athlete_series_self(token: str, db: Session = Depends(get_db, scope="function")):
        return JSONResponse(A.athlete_series(db, athlete_by_token(db, token), A.team_today()))

    # ---------- run type and team workout (athlete and coach) ----------

    def workout_choices(db: Session, act: Activity) -> list[PlannedWorkout]:
        lo, hi = act.local_date - dt.timedelta(days=3), act.local_date + dt.timedelta(days=3)
        rows = db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= lo, PlannedWorkout.date <= hi)
                          .order_by(PlannedWorkout.date, PlannedWorkout.id)).all()
        if act.planned and act.planned not in rows:
            rows.append(act.planned)
        return rows

    def apply_type_and_workout(db: Session, act: Activity, form) -> str | None:
        """Sets run type and team workout from a form; returns an error message or None."""
        rtype = form.get("run_type", "")
        if rtype and rtype not in A.RUN_TYPES:
            return "Pick a type of run."
        wsel = form.get("workout", None)
        if wsel is not None:
            if wsel == "none":
                new_id = None
            else:
                try:
                    new_id = int(wsel)
                except ValueError:
                    return "Pick a workout from the list."
                if new_id not in {p.id for p in workout_choices(db, act)}:
                    return "That workout isn’t within three days of this run."
            if new_id != act.planned_workout_id:
                act.planned_workout_id = new_id
                act.workout_locked = True
        if rtype:
            act.run_type = rtype
        return None

    @app.get("/athletes/{athlete_id}/runs/{activity_id}", response_class=HTMLResponse)
    def staff_run(request: Request, athlete_id: int, activity_id: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        act = db.get(Activity, activity_id)
        if act is None or act.athlete_id != a.id:
            raise HTTPException(404, "Run not found.")
        pain = next((p for p in a.pain_reports if p.activity_id == act.id), None)
        return render(request, "staff_run.html", user, a=a, act=act, pain=pain, choices=workout_choices(db, act))

    @app.post("/athletes/{athlete_id}/runs/{activity_id}")
    async def staff_run_save(request: Request, athlete_id: int, activity_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        act = db.get(Activity, activity_id)
        if act is None or act.athlete_id != a.id:
            raise HTTPException(404, "Run not found.")
        err = apply_type_and_workout(db, act, f)
        flash(request, err or "Saved.", "error" if err else "ok")
        return back(f"/athletes/{a.id}/runs/{act.id}")

    # ---------- ability groups ----------

    @app.get("/groups", response_class=HTMLResponse)
    def groups_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        groups = db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()
        ab = A.abilities(db, A.team_today())
        athletes = athletes_sorted(db)
        counts = {(g.name, x): 0 for g in groups for x in ("Boys", "Girls")}
        for at in athletes:
            k = (ab.get(at.id, {}).get("group", ""), at.gender)
            if k in counts:
                counts[k] += 1
        unrated = sum(1 for at in athletes if not ab.get(at.id, {}).get("group"))
        targets = A.vdot_targets(db)
        ranges = {}
        cuts = sorted({g.min_vdot_boys for g in groups})
        for g in groups:
            above = [c for c in cuts if c > g.min_vdot_boys]
            top = (min(above) - 1) if above else max(targets or {0: 0})
            rows = [targets[v] for v in sorted(targets) if g.min_vdot_boys <= v <= top]
            if rows:
                ranges[g.id] = (rows[0][1], rows[-1][1], rows[0][0], rows[-1][0])
        table = "\n".join(f"{v},{t8},{t16}" for v, (t8, t16) in sorted(targets.items(), reverse=True))
        return render(request, "groups.html", user, groups=groups, counts=counts, unrated=unrated, ranges=ranges, table=table,
                      lookback=A.get_setting(db, "group_lookback_days", "365"),
                      show_athletes=A.get_setting(db, "show_group_to_athletes", "1") == "1")

    @app.post("/groups")
    async def groups_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        groups = db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()
        try:
            seen = set()
            for g in groups:
                if f.get(f"delete_{g.id}"):
                    for at in db.scalars(select(Athlete).where(Athlete.ability_override == g.name)).all():
                        at.ability_override = ""
                    db.delete(g)
                    continue
                name = str(f.get(f"name_{g.id}", g.name)).strip()[:40]
                if not name or name.lower() in seen:
                    raise ValueError("Each group needs its own name.")
                seen.add(name.lower())
                old = g.name
                g.name, g.position = name, int(f.get(f"position_{g.id}", g.position))
                g.min_vdot_boys = float(f.get(f"boys_{g.id}", g.min_vdot_boys))
                g.min_vdot_girls = float(f.get(f"girls_{g.id}", g.min_vdot_girls))
                color = str(f.get(f"color_{g.id}", g.color or "")).strip()
                if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                    raise ValueError("Colors look like #B6D7A8.")
                g.color = color.upper()
                if old != name:
                    for at in db.scalars(select(Athlete).where(Athlete.ability_override == old)).all():
                        at.ability_override = name
                    for pw in db.scalars(select(PlannedWorkout)).all():
                        parts = [name if x.strip() == old else x.strip() for x in (pw.groups or "").split(",")]
                        pw.groups = ",".join(p for p in parts if p) or "All"
            new = str(f.get("new_name", "")).strip()[:40]
            if new:
                if new.lower() in seen:
                    raise ValueError("Each group needs its own name.")
                db.add(TrainingGroup(name=new, position=len(groups), min_vdot_boys=float(f.get("new_boys") or 0),
                                     min_vdot_girls=float(f.get("new_girls") or 0)))
            if "targets" in f:
                import json as _json
                parsed = {}
                for n, line in enumerate(str(f.get("targets", "")).splitlines(), start=1):
                    line = line.strip()
                    if not line:
                        continue
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) != 3 or not parts[0].isdigit() or not all(re.fullmatch(r"\d{1,2}:\d{2}(\.\d)?", p) for p in parts[1:]):
                        raise ValueError(f"Target table line {n} should look like 60,2:13,4:57")
                    parsed[str(int(parts[0]))] = [parts[1], parts[2]]
                A.set_setting(db, "vdot_targets", _json.dumps(parsed))
            days = int(f.get("lookback", 365))
            if not 30 <= days <= 1500:
                raise ValueError("Look back between 30 and 1500 days.")
            A.set_setting(db, "group_lookback_days", str(days))
            A.set_setting(db, "show_group_to_athletes", "1" if f.get("show_athletes") else "0")
            flash(request, "Groups saved.")
        except ValueError as exc:
            db.rollback()
            msg = str(exc)
            if not msg or msg.startswith(("could not convert", "invalid literal")):
                msg = "Check the numbers: VDOT cutoffs are numbers like 52.5, and order is a whole number."
            flash(request, msg, "error")
        return back("/groups")

    @app.get("/athletes/{athlete_id}/photo")
    def athlete_photo(athlete_id: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        return photo_response(get_athlete(db, athlete_id))

    @app.get("/a/{token}/photo")
    def athlete_own_photo(token: str, db: Session = Depends(get_db, scope="function")):
        return photo_response(athlete_by_token(db, token))

    @app.post("/athletes/{athlete_id}/photo")
    async def athlete_photo_upload(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        if f.get("remove"):
            if a.photo is not None:
                db.delete(a.photo)
            flash(request, f"Removed {a.name}'s photo.")
            return back(f"/athletes/{a.id}")
        up = f.get("photo")
        if not up or not getattr(up, "filename", ""):
            flash(request, "Choose a photo first.", "error")
            return back(f"/athletes/{a.id}")
        try:
            P.save(db, a, await up.read(P.MAX_PHOTO_BYTES + 1))
            flash(request, f"Saved {a.name}'s photo.")
        except P.PhotoError as exc:
            flash(request, str(exc), "error")
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/photos")
    async def athlete_photos_zip(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        up = f.get("file")
        if not up or not getattr(up, "filename", ""):
            flash(request, "Choose a .zip of photos first.", "error")
            return back("/athletes")
        try:
            done, missed = P.import_zip(db, await up.read(P.MAX_ZIP_BYTES + 1))
        except P.PhotoError as exc:
            flash(request, str(exc), "error")
            return back("/athletes")
        log(db, "admin", f"{user.username} added {len(done)} athlete photos.")
        flash(request, f"Added {len(done)} photo{'' if len(done) == 1 else 's'}." if done else "No photos matched athletes on the roster.",
              "ok" if done else "error")
        if missed:
            flash(request, "Not matched to an athlete (name the file after the athlete, such as Sam Lowry.jpg): " + ", ".join(missed[:40])
                  + (f" and {len(missed) - 40} more" if len(missed) > 40 else ""), "error")
        return back("/athletes")

    @app.get("/vdot", response_class=HTMLResponse)
    def vdot_page(request: Request, edit: int = 0, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        return render(request, "vdot.html", user, board=A.vdot_board(db, A.team_today()), edit=bool(edit) and user.role != "trainer",
                      zones=A.PACE_ZONES, vdot_limits=(A.VDOT_MIN, A.VDOT_MAX))

    @app.post("/vdot")
    async def vdot_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        bad = []
        for a in db.scalars(select(Athlete).where(Athlete.active.is_(True))).all():
            if f"vdot_{a.id}" not in f:
                continue
            raw = str(f.get(f"vdot_{a.id}", "")).strip()
            try:
                v = int(raw) if raw else None
                if v is not None and not A.VDOT_MIN <= v <= A.VDOT_MAX:
                    raise ValueError
                a.vdot_override = v
            except ValueError:
                bad.append(a.name)
        if bad:
            flash(request, f"VDOT should be a whole number from {A.VDOT_MIN} to {A.VDOT_MAX}; not changed for " + ", ".join(bad) + ".", "error")
        else:
            flash(request, "Saved.")
        return back("/vdot?edit=1" if bad else "/vdot")

    @app.post("/athletes/{athlete_id}/vdot")
    async def athlete_vdot(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        raw = str(f.get("vdot", "")).strip()
        try:
            v = int(raw) if raw else None
            if v is not None and not A.VDOT_MIN <= v <= A.VDOT_MAX:
                raise ValueError
        except ValueError:
            flash(request, f"VDOT should be a whole number from {A.VDOT_MIN} to {A.VDOT_MAX}.", "error")
            return back(f"/athletes/{a.id}")
        a.vdot_override = v
        flash(request, f"{a.name}'s VDOT is now " + (f"set to {v}." if v is not None else "worked out from their most recent race."))
        return back(f"/athletes/{a.id}")

    @app.post("/athletes/{athlete_id}/ability")
    async def athlete_ability(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = get_athlete(db, athlete_id)
        choice = f.get("ability_override", "")
        names = {g.name for g in db.scalars(select(TrainingGroup)).all()}
        if choice and choice not in names:
            flash(request, "Pick a group from the list.", "error")
        else:
            a.ability_override = choice
            flash(request, f"{a.name} is now " + (f"pinned to {choice}." if choice else "grouped automatically."))
        return back(f"/athletes/{a.id}")

    # ---------- athlete: my runs, effort and pain per run ----------

    def _own_run(db: Session, a: Athlete, activity_id: int) -> Activity:
        act = db.get(Activity, activity_id)
        if act is None or act.athlete_id != a.id:
            raise HTTPException(404, "Run not found.")
        return act

    @app.get("/a/{token}/runs", response_class=HTMLResponse)
    def athlete_runs(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).options(selectinload(Activity.planned))
                          .order_by(Activity.start_utc.desc()).limit(60)).all()
        pains = {p.activity_id: p for p in a.pain_reports if p.activity_id}
        today = A.team_today()
        oldest = acts[-1].local_date if acts else today - dt.timedelta(days=14)
        reports = db.scalars(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date >= min(oldest, today - dt.timedelta(days=14)))).all()
        items = [("run", act.local_date, act) for act in acts] + [("missed", m.date, m) for m in reports] \
            + [("gap", d, None) for d in A.unlogged_days(db, a, today)]
        items.sort(key=lambda x: (x[1], x[0] == "run"), reverse=True)
        return render(request, "athlete_runs.html", None, a=a, items=items, pains=pains, token=token)

    @app.get("/a/{token}/runs/{activity_id}", response_class=HTMLResponse)
    def athlete_run(request: Request, token: str, activity_id: int, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        act = _own_run(db, a, activity_id)
        pain = next((p for p in a.pain_reports if p.activity_id == act.id), None)
        return render(request, "athlete_run.html", None, a=a, act=act, pain=pain, token=token, areas=BODY_AREAS,
                      choices=workout_choices(db, act))

    @app.post("/a/{token}/runs/{activity_id}")
    async def athlete_run_save(request: Request, token: str, activity_id: int, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        act = _own_run(db, a, activity_id)
        try:
            rpe = int(f.get("rpe", ""))
            if not 0 <= rpe <= 10:
                raise ValueError
        except ValueError:
            flash(request, "Pick how hard the run felt, from 0 to 10.", "error")
            return back(f"/a/{token}/runs/{act.id}")
        feel = f.get("feel", "")
        if feel and feel not in FEELS:
            feel = ""
        area = f.get("pain_area", "None")
        level = None
        if area != "None":
            try:
                level = int(f.get("pain_level", ""))
                if area not in BODY_AREAS or not 1 <= level <= 10:
                    raise ValueError
            except ValueError:
                flash(request, "You picked where it hurts. Also pick how much, from 1 to 10.", "error")
                return back(f"/a/{token}/runs/{act.id}")
        note = str(f.get("note", "")).strip()[:300]
        err = apply_type_and_workout(db, act, f)
        if err:
            flash(request, err, "error")
            return back(f"/a/{token}/runs/{act.id}")
        act.rpe, act.feel, act.athlete_note, act.feedback_at = rpe, feel, note, G._now()
        existing = db.scalar(select(PainReport).where(PainReport.activity_id == act.id))
        if level is None:
            if existing is not None:
                db.delete(existing)
        else:
            p = existing or PainReport(athlete_id=a.id, activity_id=act.id)
            p.date, p.body_area, p.severity, p.note = act.local_date, area, level, note
            p.saw_trainer = bool(f.get("saw_trainer"))
            db.add(p)
        msg = "Saved. Thanks for adding your effort."
        if level is not None and level >= A.PAIN_FLAG:
            msg += " That’s a lot of pain: please also tell your coach or athletic trainer in person."
        flash(request, msg)
        return back(f"/a/{token}/runs")

    # ---------- athlete: a day I didn't run ----------

    def _missed_day(token: str, day: str, db: Session) -> tuple[Athlete, dt.date]:
        a = athlete_by_token(db, token)
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            raise HTTPException(404, "That date isn’t valid.")
        today = A.team_today()
        if d > today or d < today - dt.timedelta(days=60):
            raise HTTPException(400, "You can report a missed day from the last 60 days, up to today.")
        return a, d

    @app.get("/a/{token}/missed")
    def athlete_missed_start(token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        gaps = A.unlogged_days(db, a, A.team_today())
        return back(f"/a/{token}/missed/{(gaps[0] if gaps else A.team_today()).isoformat()}")

    @app.get("/a/{token}/missed/{day}", response_class=HTMLResponse)
    def athlete_missed(request: Request, token: str, day: str, edit: int = 0, db: Session = Depends(get_db, scope="function")):
        a, d = _missed_day(token, day, db)
        report = db.scalar(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date == d))
        ran = db.scalar(select(func.count(Activity.id)).where(Activity.athlete_id == a.id, Activity.local_date == d))
        return render(request, "athlete_missed.html", None, a=a, day=d, report=report, editing=bool(edit or report is None),
                      ran=ran, token=token, areas=BODY_AREAS, today=A.team_today().isoformat())

    @app.post("/a/{token}/missed/{day}")
    async def athlete_missed_save(request: Request, token: str, day: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a, d = _missed_day(token, day, db)
        url = f"/a/{token}/missed/{d.isoformat()}"
        reason = f.get("reason", "")
        if reason not in A.MISSED_REASONS:
            flash(request, "Pick why you didn’t run.", "error")
            return back(url + "?edit=1")
        m = db.scalar(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date == d)) or MissedDay(athlete_id=a.id, date=d)
        m.reason = reason
        if reason == "Injury":
            area, side, started, seen, more = (f.get(k, "") for k in ("body_area", "side", "started", "seen_by", "more_days"))
            try:
                pain = int(f.get("pain", ""))
            except ValueError:
                pain = 0
            missing = [label for ok, label in ((area in BODY_AREAS, "where it hurts"), (side in A.SIDES, "which side"),
                                               (1 <= pain <= 10, "how much it hurts")) if not ok]
            if missing:
                flash(request, "Please add " + " and ".join(missing) + ".", "error")
                return back(url + "?edit=1")
            m.body_area, m.side, m.pain = area, side, pain
            m.started = started if started in A.STARTED else ""
            m.seen_by = seen if seen in A.SEEN_BY else ""
            m.more_days = more if more in A.MORE_DAYS else ""
            m.description = str(f.get("description", "")).strip()[:1000]
        else:
            m.body_area = m.side = m.started = m.seen_by = m.more_days = ""
            m.pain = None
            m.description = str(f.get("note", "")).strip()[:1000]
        m.updated_at = utcnow()
        db.add(m)
        msg = "Sent to your coaches."
        if reason == "Injury" and (m.pain or 0) >= A.PAIN_FLAG:
            msg += " That’s a lot of pain: please also tell your coach or athletic trainer in person today."
        flash(request, msg)
        return back(url)

    @app.post("/a/{token}/missed/{day}/delete")
    async def athlete_missed_delete(request: Request, token: str, day: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a, d = _missed_day(token, day, db)
        m = db.scalar(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date == d))
        if m is not None:
            db.delete(m)
            flash(request, "Report removed.")
        return back(f"/a/{token}/runs")

    @app.post("/missed/{mid}/log-injury")
    async def missed_to_injury(request: Request, mid: int, user=Depends(medical, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        m = db.get(MissedDay, mid)
        if m is None or m.reason != "Injury":
            raise HTTPException(404, "Report not found.")
        kind = f.get("injury", "")
        if kind not in INJURY_TYPES:
            flash(request, "Pick an injury type.", "error")
            return back("/injuries")
        try:
            onset = parse_date(f.get("onset_date", "")) if f.get("onset_date") else m.date
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back("/injuries")
        area = m.body_area if m.body_area in BODY_AREAS else "Other"
        inj = Injury(athlete_id=m.athlete_id, onset_date=onset, injury=kind, body_area=area, logged_by=user.username,
                     notes=("Athlete report: " + m.description) if user.role in ("admin", "trainer") and m.description else "")
        db.add(inj)
        db.flush()
        m.injury_id = inj.id
        flash(request, f"Added {kind} for {m.athlete.name} to the injury log.")
        return back("/injuries")

    # ---------- privacy: policy page, AI consent, export, deletion ----------

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return render(request, "privacy.html", None)

    @app.post("/a/{token}/ai-consent")
    async def athlete_ai_consent(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        if f.get("action") == "withdraw":
            a.ai_consent_at, a.ai_consent_note = None, ""
            log(db, "privacy", f"{a.name} withdrew consent to AI analysis.")
            flash(request, "Done. Your data will not be sent for AI analysis from now on.")
        else:
            if not (f.get("agree") and f.get("parent")):
                flash(request, "To turn on AI analysis, tick both boxes: that you agree, and that a parent or guardian agrees.", "error")
                return back(f"/a/{token}#privacy")
            a.ai_consent_at = utcnow()
            a.ai_consent_note = "Athlete agreed and confirmed parent/guardian agreement on the team link page."
            log(db, "privacy", f"{a.name} agreed to AI analysis.")
            flash(request, "Thanks. Your training data will be included in the coaches’ AI briefing. You can turn this off any time.")
        return back(f"/a/{token}#privacy")

    @app.get("/a/{token}/my-data.json")
    def athlete_export(token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).options(selectinload(Activity.laps), selectinload(Activity.planned))
                          .order_by(Activity.start_utc)).all()
        ab = A.abilities(db, A.team_today()).get(a.id, {})
        data = {
            "exported_at": utcnow().isoformat() + "Z", "team": s.team_name,
            "athlete": {"name": a.name, "email": a.email or None, "gender": a.gender, "grade": a.grade, "group": a.group, "hr_max": a.hr_max, "hr_rest": a.hr_rest,
                        "training_group": ab.get("group"), "vdot": ab.get("vdot"),
                        "ai_consent": a.ai_consent_at.isoformat() + "Z" if a.ai_consent_at else None},
            "garmin": None if a.garmin is None else {"status": a.garmin.status, "garmin_user_id": a.garmin.garmin_user_id,
                                                     "permissions": a.garmin.permissions, "connected_at": a.garmin.connected_at.isoformat() + "Z",
                                                     "consent_note": a.garmin.consent_note},
            "runs": [{"date": x.local_date.isoformat(), "start_utc": x.start_utc.isoformat() + "Z", "source": x.source, "sport": x.sport,
                      "distance_m": x.distance_m, "duration_s": x.duration_s, "avg_hr": x.avg_hr, "max_hr": x.max_hr, "device": x.device,
                      "watch_vo2_max": x.vo2_max,
                      "run_type": A.run_type_of(x), "team_workout": x.planned.title if x.planned else None,
                      "effort": x.rpe, "legs": x.feel or None, "note": x.athlete_note or None,
                      "laps": [{"lap": l.idx + 1, "distance_m": l.distance_m, "time_s": l.timer_s, "avg_hr": l.avg_hr, "max_hr": l.max_hr,
                                "rep": l.rep_num, "hr_drop_60s": l.hr_drop_60s} for l in x.laps]} for x in acts],
            "results": [{"date": r.date.isoformat(), "event": r.event, "time_s": r.time_s, "season": r.season, "course": r.course, "kind": r.kind}
                        for r in sorted(a.results, key=lambda r: r.date)],
            "goals": [{"season": g.season, "event": g.event, "goal_s": g.goal_s} for g in a.goals],
            "injuries": [{"onset": i.onset_date.isoformat(), "injury": i.injury, "body_area": i.body_area,
                          "return": i.return_date.isoformat() if i.return_date else None} for i in a.injuries],
            "resting_heart_rate": [{"date": r.date.isoformat(), "bpm": r.bpm, "source": r.source}
                                   for r in sorted(a.resting_hrs, key=lambda r: r.date)],
            "pain_reports": [{"date": p.date.isoformat(), "body_area": p.body_area, "severity": p.severity, "note": p.note or None,
                              "saw_athletic_trainer": p.saw_trainer} for p in a.pain_reports],
            "days_missed": [{"date": m.date.isoformat(), "reason": m.reason, "body_area": m.body_area or None, "side": m.side or None,
                             "pain": m.pain, "started": m.started or None, "seen_by": m.seen_by or None, "more_days": m.more_days or None,
                             "description": m.description or None}
                            for m in db.scalars(select(MissedDay).where(MissedDay.athlete_id == a.id).order_by(MissedDay.date)).all()],
            "not_included": "Athletic trainer notes on injuries are kept separately; ask the athletic trainer or coaches for a copy.",
        }
        fname = f"my-training-data-{A.team_today().isoformat()}.json"
        return Response(json.dumps(data, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/a/{token}/delete-garmin-data")
    async def athlete_delete_garmin(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        if str(f.get("confirm", "")).strip().upper() != "DELETE":
            flash(request, "Type DELETE to confirm.", "error")
            return back(f"/a/{token}#privacy")
        if a.garmin and a.garmin.status != "revoked":
            with G.make_client() as http:
                G.disconnect(db, a.garmin, http)
        if a.garmin:
            db.delete(a.garmin)
        n = 0
        for act in db.scalars(select(Activity).where(Activity.athlete_id == a.id, Activity.source.like("garmin%"))).all():
            db.delete(act)
            n += 1
        for r in db.scalars(select(RestingHeartRate).where(RestingHeartRate.athlete_id == a.id, RestingHeartRate.source == "garmin")).all():
            db.delete(r)
        log(db, "privacy", f"{a.name} deleted their Garmin data ({n} runs) and disconnected Garmin.")
        flash(request, f"Deleted {n} run{'' if n == 1 else 's'} that came from Garmin and disconnected your Garmin account.")
        return back(f"/a/{token}#privacy")

    @app.post("/a/{token}/delete-request")
    async def athlete_delete_request(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a = athlete_by_token(db, token)
        if not db.scalar(select(DeletionRequest).where(DeletionRequest.athlete_id == a.id, DeletionRequest.status == "open")):
            db.add(DeletionRequest(athlete_id=a.id, athlete_name=a.name))
            log(db, "privacy", f"{a.name} asked for all of their data to be deleted.")
        flash(request, "Your request is in. Your coaches will delete all of your data within 30 days and let you know.")
        return back(f"/a/{token}#privacy")

    # ---------- Garmin ----------

    @app.get("/garmin/callback", response_class=HTMLResponse)
    def garmin_callback(request: Request, background: BackgroundTasks, db: Session = Depends(get_db, scope="function"),
                        code: str = "", state: str = "", error: str = ""):
        # The athlete comes from the sign-in record, not the browser session: Garmin's app can send people back to a
        # different browser than the one they started in.
        pending = db.get(OAuthState, state) if state else None
        athlete = db.get(Athlete, pending.athlete_id) if pending else None
        token = athlete.link_token if athlete else request.session.get("athlete_token")

        def failed(msg: str):
            if token:
                flash(request, msg, "error")
                return back(f"/a/{token}")
            return render(request, "message.html", None, status_code=400, title="Garmin did not connect",
                          text=msg + " Open your athlete link and tap Connect Garmin again.")

        if error or not code:
            return failed("Garmin connection was cancelled." if error else "Garmin did not send a code back.")
        try:
            with G.make_client() as http:
                a = G.complete_authorization(db, code, state, http)
        except (G.GarminError, httpx.HTTPError) as exc:
            # Saved, not rolled back: the sign-in record stays used, so a reloaded page can't resend the one-time code.
            log(db, "garmin", f"Garmin connection failed{' for ' + athlete.name if athlete else ''}: {exc}", "error")
            return failed(f"Garmin did not connect: {exc}" if isinstance(exc, G.GarminError) else "Could not reach Garmin. Please try again.")
        background.add_task(kick_backfills)
        flash(request, "Garmin is connected. New runs will appear after your watch syncs.")
        return back(f"/a/{a.link_token}")

    def _process(payload: dict):
        db = SessionLocal()
        try:
            with G.make_client() as http:
                counts = G.handle_notification(db, payload, http)
            log(db, "garmin", "Notification processed: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
            db.commit()
        except Exception as exc:
            db.rollback()
            log(db, "garmin", f"Notification failed: {exc}", "error")
            db.commit()
        finally:
            db.close()

    @app.post("/garmin/webhook/{secret}")
    async def garmin_webhook(request: Request, secret: str, background: BackgroundTasks):
        if not s.garmin_webhook_secret or not hmac.compare_digest(secret, s.garmin_webhook_secret):
            raise HTTPException(404, "Not found.")
        sent_id = request.headers.get("garmin-client-id")
        if sent_id and s.garmin_client_id and sent_id != s.garmin_client_id:
            raise HTTPException(403, "Wrong client.")
        body = await request.body()
        if len(body) > 10 * 1024 * 1024:
            raise HTTPException(413, "Too large.")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "Body must be JSON.")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Body must be a JSON object.")
        background.add_task(_process, payload)
        background.add_task(kick_backfills)
        return Response(status_code=200)

    nightly_lock = threading.Lock()
    backfill_lock = threading.Lock()

    def kick_backfills():
        """Send queued Garmin past-run requests on a background thread (paced and retried; see G.process_backfills).
        Run after the response, so the queued rows are saved first."""
        if not (BACKFILL_IN_BACKGROUND and s.garmin_configured) or not backfill_lock.acquire(blocking=False):
            return

        def run():
            db = SessionLocal()
            try:
                with G.make_client() as http:
                    G.process_backfills(db, http)
            except Exception as exc:
                db.rollback()
                log(db, "garmin", f"Sending past-run requests to Garmin stopped: {exc}", "error")
                db.commit()
            finally:
                db.close()
                backfill_lock.release()
        threading.Thread(target=run, daemon=True).start()

    @app.post("/cron/nightly")
    def cron_nightly(request: Request, background: BackgroundTasks, force: bool = False):
        """Called by the hosting platform's timer. Runs only during NIGHTLY_HOUR (team time) unless force=true."""
        from .cli import is_nightly_hour, nightly
        auth = request.headers.get("authorization", "")
        if not s.cron_secret or not hmac.compare_digest(auth.encode(), f"Bearer {s.cron_secret}".encode()):
            raise HTTPException(404, "Not found.")
        if not force and not is_nightly_hour():
            return {"skipped": f"not {s.nightly_hour}:00 in {s.team_timezone}"}
        if not nightly_lock.acquire(blocking=False):
            raise HTTPException(409, "The nightly job is already running.")
        try:
            background.add_task(kick_backfills)  # also catches up on past-run requests still waiting
            return {"ran": nightly()}
        finally:
            nightly_lock.release()

    @app.get("/healthz")
    def healthz(db: Session = Depends(get_db, scope="function")):
        db.execute(select(1))
        return {"ok": True}

    return app

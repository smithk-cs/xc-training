"""Running-form pages: a coach films or picks a side-view clip on the athlete's page, the phone analyzes it, and the
report (page and PDF) is kept with the athlete. Athletes see their own reports on their page. See form.py."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import form as F
from .ingest import log
from .models import FormAnalysis
from .security import csrf_ok

# The capture page runs the pose model in the browser: WebAssembly needs 'wasm-unsafe-eval', the model runs in a
# worker, and the clip plays from a local blob. Connections stay to this site only (so the model's usage pings to
# Google are blocked too).
CAPTURE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; "
               "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def parse_pace(text: str) -> float | None:
    """'6:20' (a mile) -> 380 seconds; '' -> None. Raises ValueError for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2}):([0-5]\d)", text)
    if not m:
        raise ValueError("Pace looks like 6:20 (minutes:seconds a mile).")
    s = int(m.group(1)) * 60 + int(m.group(2))
    if not 180 <= s <= 900:
        raise ValueError("Pace should be between 3:00 and 15:00 a mile.")
    return float(s)


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db = h.render, h.flash, h.back, h.get_db
    any_staff, coach_or_admin, get_athlete, athlete_by_token, logo_bytes = h.any_staff, h.coach_or_admin, h.get_athlete, h.athlete_by_token, h.logo_bytes
    team = h.team_name

    def one(db: Session, athlete_id: int, fid: int) -> FormAnalysis:
        fa = db.get(FormAnalysis, fid)
        if fa is None or fa.athlete_id != athlete_id:
            raise HTTPException(404, "Not found.")
        return fa

    @app.get("/athletes/{athlete_id}/form/new", response_class=HTMLResponse)
    def form_new(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        resp = render(request, "form_new.html", user, a=a)
        resp.headers["Content-Security-Policy"] = CAPTURE_CSP
        return resp

    @app.post("/athletes/{athlete_id}/form")
    async def form_save(request: Request, athlete_id: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        raw = await request.body()
        if len(raw) > 200_000:
            raise HTTPException(413, "Too large.")
        try:
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Bad request.")
        except ValueError:
            return JSONResponse({"error": "Bad request."}, status_code=400)
        if not csrf_ok(request.session, data.get("csrf")):
            return JSONResponse({"error": "Your session expired. Reload the page and try again."}, status_code=400)
        try:
            clean = F.clean(data)
            pace = parse_pace(str(data.get("pace") or ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        fa = FormAnalysis(athlete_id=a.id, by=user.display_name[:60], pace_s=pace, note=str(data.get("note") or "").strip()[:300],
                          score=F.report(clean["metrics"], pace)["score"], data=json.dumps(clean, separators=(",", ":")))
        db.add(fa)
        db.flush()
        log(db, "app", f"{user.username} saved a running-form analysis for {a.name}.")
        return JSONResponse({"url": f"/athletes/{a.id}/form/{fa.id}"})

    @app.get("/athletes/{athlete_id}/form/{fid}", response_class=HTMLResponse)
    def form_report(request: Request, athlete_id: int, fid: int, user=Depends(any_staff, scope="function"),
                    db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        fa = one(db, a.id, fid)
        data = json.loads(fa.data)
        return render(request, "form_report.html", user, a=a, fa=fa, data=data, rep=F.report(data["metrics"], fa.pace_s),
                      bones=F.BONES, curve_titles=F.CURVE_TITLES, curve_keys=F.CURVE_KEYS,
                      others=db.scalars(select(FormAnalysis).where(FormAnalysis.athlete_id == a.id, FormAnalysis.id != fa.id)
                                        .order_by(FormAnalysis.created_at.desc())).all())

    def pdf_response(db: Session, a, fa) -> Response:
        data = F.pdf(fa, a.name, team, logo_bytes(db), fa.by)
        day = A.local_time(fa.created_at).date() if fa.created_at else A.team_today()
        name = re.sub(r"[^A-Za-z0-9]+", "-", f"Running-form-{a.first_name}-{a.last_name}-{day.isoformat()}").strip("-")
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}.pdf"',
                                                                       "Cache-Control": "private, no-store"})

    @app.get("/athletes/{athlete_id}/form/{fid}/pdf")
    def form_pdf(athlete_id: int, fid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        return pdf_response(db, a, one(db, a.id, fid))

    @app.post("/athletes/{athlete_id}/form/{fid}/delete")
    async def form_delete(request: Request, athlete_id: int, fid: int, user=Depends(coach_or_admin, scope="function"),
                          db: Session = Depends(get_db, scope="function")):
        await h.form_checked(request)
        a = get_athlete(db, athlete_id)
        db.delete(one(db, a.id, fid))
        flash(request, "Deleted the form analysis.")
        return back(f"/athletes/{a.id}")

    @app.get("/a/{token}/form/{fid}/pdf")
    def athlete_form_pdf(token: str, fid: int, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        return pdf_response(db, a, one(db, a.id, fid))


def for_athlete(db: Session, athlete_id: int) -> list[FormAnalysis]:
    return db.scalars(select(FormAnalysis).where(FormAnalysis.athlete_id == athlete_id).order_by(FormAnalysis.created_at.desc())).all()

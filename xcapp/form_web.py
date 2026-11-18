"""Running-form pages: a coach films or picks a side-view clip on the athlete's page, the phone analyzes it, and the
report (page and PDF) is kept with the athlete. Athletes see their own reports on their page. See form.py."""
from __future__ import annotations

import base64
import json
import re
from html import escape
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import form as F
from . import form_plan as FP
from . import reminders as RM
from . import season_plan as SPL
from .config import settings
from .ingest import log
from .models import Athlete, FormAnalysis, utcnow
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

    @app.get("/form", response_class=HTMLResponse)
    def form_page(request: Request, athlete: int = 0, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        """Running form: pick an athlete, film or choose a clip, analyze; and the team's recent analyses."""
        athletes = db.scalars(select(Athlete).where(A.on_team()).order_by(Athlete.last_name, Athlete.first_name)).all()
        recent = db.execute(select(FormAnalysis, Athlete).join(Athlete, Athlete.id == FormAnalysis.athlete_id)
                            .order_by(FormAnalysis.created_at.desc()).limit(30)).all()
        resp = render(request, "form_page.html", user, athletes=athletes, picked=athlete, recent=recent,
                      can_film=user.role in ("admin", "coach"),
                      film=FP.film_status(db, A.team_today()) if user.sees("injuries") else FP.hide_injured(FP.film_status(db, A.team_today())),
                      conditions=FP.CONDITIONS,
                      phases=SPL.PHASES)
        resp.headers["Content-Security-Policy"] = CAPTURE_CSP
        return resp

    @app.get("/athletes/{athlete_id}/form/new")
    def form_new(athlete_id: int, user=Depends(coach_or_admin, scope="function")):
        return back(f"/form?athlete={athlete_id}")   # the analyzer has its own page now

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
                          condition="tired" if data.get("condition") == "tired" else "fresh",
                          score=F.report(clean["metrics"], pace)["score"], data=json.dumps(clean, separators=(",", ":")))
        db.add(fa)
        db.flush()
        log(db, "app", f"{user.username} saved a running-form analysis for {a.name}.")
        return JSONResponse({"url": f"/athletes/{a.id}/form/{fa.id}", "view": F.view(clean, pace)})

    @app.get("/athletes/{athlete_id}/form/{fid}", response_class=HTMLResponse)
    def form_report(request: Request, athlete_id: int, fid: int, user=Depends(any_staff, scope="function"),
                    db: Session = Depends(get_db, scope="function")):
        a = get_athlete(db, athlete_id)
        fa = one(db, a.id, fid)
        data = json.loads(fa.data)
        return render(request, "form_report.html", user, a=a, fa=fa, data=data, rep=F.report(data["metrics"], fa.pace_s), view=F.view(data, fa.pace_s),
                      fatigue=FP.fatigue(db, a.id, fa), conditions=FP.CONDITIONS,
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

    @app.post("/athletes/{athlete_id}/form/{fid}/email")
    async def form_email(request: Request, athlete_id: int, fid: int, user=Depends(coach_or_admin, scope="function"),
                         db: Session = Depends(get_db, scope="function")):
        """Email the athlete this report: the score and what to work on, with the PDF attached."""
        await h.form_checked(request)
        a = get_athlete(db, athlete_id)
        fa = one(db, a.id, fid)
        back_to = f"/athletes/{a.id}/form/{fa.id}"
        if not a.email:
            flash(request, f"{a.name} has no email address yet. Add it on their athlete page.", "error")
            return back(back_to)
        pdf = pdf_response(db, a, fa)
        name = re.search(r'filename="([^"]+)"', pdf.headers["Content-Disposition"]).group(1)
        try:
            RM.send_email(a.email, *compose_report(a, fa), reply=RM.reply_to(db),
                          attachments=[{"filename": name, "content": base64.b64encode(pdf.body).decode()}])
        except RM.EmailError as exc:
            flash(request, str(exc), "error")
            return back(back_to)
        fa.emailed_at = utcnow()
        from . import push as PU
        PU.send(db, [a.id], "Your running form report", "Your coaches emailed you your video analysis. Tap to see it on your page.",
                f"{settings().base_url.rstrip('/')}/a/{a.link_token}#form")
        log(db, "email", f"{user.username} emailed {a.name} their running-form report.")
        flash(request, f"Emailed the report to {a.name} at {a.email}.")
        return back(back_to)

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


def compose_report(a: Athlete, fa: FormAnalysis) -> tuple[str, str, str]:
    """The email with a running-form report: the score, the top things to work on (cue and drill), and the PDF."""
    s = settings()
    rep = F.report(json.loads(fa.data).get("metrics", {}), fa.pace_s)
    day = A.local_time(fa.created_at).date() if fa.created_at else A.team_today()
    link = f"{s.base_url.rstrip('/')}/a/{a.link_token}"
    subject = f"{s.team_name}: your running form report ({day:%b %-d})"
    score = f"{rep['score']:.0f}%" if rep["score"] is not None else None
    focus = rep["focus"]
    intro = f"Here is the video analysis of your running form from {day:%B %-d}" + (f", filmed by {fa.by}" if fa.by else "") + "."
    text_lines = [f"Hi {a.first_name},", "", intro, "The full report is attached as a PDF.", ""]
    if score:
        text_lines += [f"Form score: {score}", ""]
    if focus:
        text_lines += ["What to work on first:"]
        for c in focus:
            text_lines += [f"  {c['priority']}. {c['title']}: {c['summary']}"]
            text_lines += [f"     Cue: {c['cue']}"] if c["cue"] else []
            text_lines += [f"     Drill: {c['drill']}"] if c["drill"] else []
        text_lines += [""]
    else:
        text_lines += ["Everything we measured is in the usual range. Keep it up.", ""]
    if fa.note:
        text_lines += [f"Coach's note: {fa.note}", ""]
    text_lines += [f"Your page: {link}", "", "Questions? Reply to this email.", "", s.team_name]
    items = "".join(
        f'<li style="margin:10px 0"><b>{escape(c["title"])}</b>: {escape(c["summary"])}'
        + (f'<br><span style="color:#6B645C">Cue:</span> {escape(c["cue"])}' if c["cue"] else "")
        + (f'<br><span style="color:#6B645C">Drill:</span> {escape(c["drill"])}' if c["drill"] else "") + "</li>"
        for c in focus)
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;color:#1B1B1F">
<div style="background:#A6192E;color:#fff;padding:14px 18px;border-bottom:4px solid #C99A2E;font-weight:bold;font-size:18px">{escape(s.team_name)}</div>
<div style="padding:16px 18px">
<p>Hi {escape(a.first_name)},</p><p>{escape(intro)} The full report is attached as a PDF.</p>
{f'<p style="font-size:22px;font-weight:bold;margin:12px 0">Form score: {score}</p>' if score else ''}
{f'<p style="margin-bottom:0"><b>What to work on first:</b></p><ol style="padding-left:20px;margin-top:4px">{items}</ol>' if focus
 else '<p>Everything we measured is in the usual range. Keep it up.</p>'}
{f'<p><b>Coach’s note:</b> {escape(fa.note)}</p>' if fa.note else ''}
<p><a href="{escape(link)}" style="display:inline-block;background:#A6192E;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:bold">Open my page</a></p>
<p style="font-size:12px;color:#6B645C">Questions? Reply to this email.</p>
</div></div>"""
    return subject, "\n".join(text_lines), html

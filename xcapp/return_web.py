"""Return-to-run plans (return_run.py): the trainer starts and adjusts them on the Injuries page; the athlete reports
each step on their page."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import analysis as A
from . import return_run as RR
from .ingest import log
from .models import Injury, ReturnPlan


def register(app, h: SimpleNamespace) -> None:
    flash, back, get_db, form_checked = h.flash, h.back, h.get_db, h.form_checked
    medical, athlete_by_token = h.medical, h.athlete_by_token

    @app.post("/injuries/{iid}/return-run")
    async def start_plan(request: Request, iid: int, user=Depends(medical, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        inj = db.get(Injury, iid)
        if inj is None:
            raise HTTPException(404, "Injury not found.")
        step = int(f.get("step", "0")) if str(f.get("step", "0")).isdigit() else 0
        p = RR.start(db, inj.athlete, A.team_today(), step=step, injury_id=inj.id, by=user.username)
        log(db, "app", f"{user.username} started {inj.athlete.name}'s return to running at step {p.step + 1}.")
        flash(request, f"Started {inj.athlete.name}’s return to running at step {p.step + 1}: {RR.STEPS[p.step][0]}. "
                       "They see it on their page.")
        return back("/injuries#return")

    @app.post("/injuries/return/{pid}")
    async def change_plan(request: Request, pid: int, user=Depends(medical, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        p = db.get(ReturnPlan, pid)
        if p is None:
            raise HTTPException(404, "Plan not found.")
        action, today = f.get("action"), A.team_today()
        if action == "back":
            p.step = max(0, p.step - 1)
        elif action == "forward":
            p.step = min(len(RR.STEPS) - 1, p.step + 1)
        elif action == "stop":
            p.status = "stopped"
        elif action == "done":
            p.status, p.finished_on = "done", today
        if action in ("back", "forward", "resume"):
            p.status, p.next_on = "active", today
        log(db, "app", f"{user.username} changed {p.athlete.name}'s return to running ({action}).")
        flash(request, f"{p.athlete.name}: " + {"stop": "plan stopped.", "done": "marked finished."}.get(
            action, f"now on step {p.step + 1}, {RR.STEPS[p.step][0]}, open today."))
        return back("/injuries#return")

    @app.post("/a/{token}/return-run")
    async def athlete_report(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        p = RR.active(db, a.id)
        try:
            if p is None:
                raise ValueError("You don’t have a return-to-running plan.")
            pain = int(f.get("pain", ""))
            msg = RR.report(db, p, pain, A.team_today(), str(f.get("note", "")).strip())
            flash(request, msg, "ok" if p.status == "active" or p.status == "done" else "error")
        except ValueError as exc:
            flash(request, str(exc) if "invalid literal" not in str(exc) else "Pick how much it hurt, 0 to 10.", "error")
        return back(f"/a/{token}#return-run")

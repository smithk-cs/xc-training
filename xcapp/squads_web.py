"""Squad competition pages: create balanced squads, move athletes, and see weekly team points."""
from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import seasons as SE
from . import squads as SQ
from .models import Athlete, SquadCompetition


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, form_checked, get_db = h.render, h.flash, h.back, h.form_checked, h.get_db
    any_staff, coach_or_admin = h.any_staff, h.coach_or_admin

    def get_comp(db: Session, cid: int) -> SquadCompetition:
        comp = db.get(SquadCompetition, cid)
        if comp is None:
            raise HTTPException(404, "That squad competition isn't here any more.")
        return comp

    @app.get("/squads", response_class=HTMLResponse)
    def squads_list(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        comps = db.scalars(select(SquadCompetition).order_by(SquadCompetition.start_date.desc(), SquadCompetition.id.desc())).all()
        if len(comps) >= 1 and "list" not in request.query_params:
            return back(f"/squads/{comps[0].id}")
        today = A.team_today()
        return render(request, "squads.html", user, comps=comps, comp=None, monday=A.week_monday(today), season=SE.period_for(today),
                      names=SQ.NAMES, max_squads=SQ.MAX_SQUADS)

    @app.post("/squads/new")
    async def squads_new(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        name = str(f.get("name", "")).strip()[:80] or "Squad competition"
        gender = f.get("gender") if f.get("gender") in ("Boys", "Girls", "All") else "All"
        try:
            n = int(f.get("squads", "4"))
            start = dt.date.fromisoformat(str(f.get("start", "")))
            end = dt.date.fromisoformat(str(f.get("end"))) if f.get("end") else None
        except ValueError:
            flash(request, "Check the number of squads and the dates.", "error")
            return back("/squads?list=1")
        problem = (f"Choose 2 to {SQ.MAX_SQUADS} squads." if not 2 <= n <= SQ.MAX_SQUADS else
                   "The end date is before the start." if end and end < start else
                   "There aren't enough athletes for that many squads." if len(SQ.eligible(db, gender)) < n else "")
        if problem:
            flash(request, problem, "error")
            return back("/squads?list=1")
        comp = SQ.create(db, name, gender, n, start, end, A.team_today())
        flash(request, f"Made {n} balanced squads. Move anyone by hand below.")
        return back(f"/squads/{comp.id}")

    @app.get("/squads/{cid}", response_class=HTMLResponse)
    def squads_page(request: Request, cid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        comp = get_comp(db, cid)
        today = A.team_today()
        st = SQ.standings(db, comp, today)
        comps = db.scalars(select(SquadCompetition).order_by(SquadCompetition.start_date.desc(), SquadCompetition.id.desc())).all()
        ab = A.abilities(db, today)
        return render(request, "squads.html", user, comps=comps, comp=comp, st=st, ab=ab, unassigned=SQ.unassigned(db, comp), season=SE.period_for(today),
                      monday=A.week_monday(today), names=SQ.NAMES, max_squads=SQ.MAX_SQUADS, categories=SQ.CATEGORIES)

    @app.post("/squads/{cid}/move")
    async def squads_move(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        comp = get_comp(db, cid)
        try:
            aid = int(f.get("athlete_id", ""))
            to = str(f.get("squad_id", ""))
            a = db.get(Athlete, aid)
            if a is None:
                raise ValueError
            SQ.move(db, comp, aid, int(to) if to.isdigit() else None)
            where = next((s.name for s in comp.squads if str(s.id) == to), None)
            flash(request, f"Moved {a.name} to {where}." if where else f"Took {a.name} out of the squads.")
        except ValueError:
            flash(request, "Pick an athlete and a squad.", "error")
        return back(f"/squads/{cid}#rosters")

    @app.post("/squads/{cid}/rebalance")
    async def squads_rebalance(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        comp = get_comp(db, cid)
        SQ.assign(db, comp, A.team_today())
        flash(request, "Dealt everyone into balanced squads again.")
        return back(f"/squads/{cid}#rosters")

    @app.post("/squads/{cid}/edit")
    async def squads_edit(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        comp = get_comp(db, cid)
        try:
            comp.name = str(f.get("name", comp.name)).strip()[:80] or comp.name
            if f.get("start"):
                comp.start_date = A.week_monday(dt.date.fromisoformat(str(f.get("start"))))
            comp.end_date = dt.date.fromisoformat(str(f.get("end"))) if f.get("end") else None
            for s in comp.squads:
                name = str(f.get(f"name_{s.id}", s.name)).strip()[:40]
                color = str(f.get(f"color_{s.id}", s.color)).strip()
                if name:
                    s.name = name
                if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                    s.color = color.upper()
            flash(request, "Saved.")
        except ValueError:
            flash(request, "Check the dates.", "error")
        return back(f"/squads/{cid}")

    @app.post("/squads/{cid}/delete")
    async def squads_delete(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        db.delete(get_comp(db, cid))
        flash(request, "Deleted the squad competition. Runs and races are unchanged.")
        return back("/squads?list=1")

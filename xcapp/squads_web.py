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
                      monday=A.week_monday(today), names=SQ.NAMES, max_squads=SQ.MAX_SQUADS,
                      races=SQ.completed_races(db, comp, today))

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

    @app.post("/squads/{cid}/scoring")
    async def squads_scoring(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        import json
        f = await form_checked(request)
        comp = get_comp(db, cid)

        def num(name: str, lo: float, hi: float, label: str) -> float:
            try:
                v = float(str(f.get(name, "")).strip())
            except ValueError:
                raise ValueError(f"{label}: enter a number.")
            if not lo <= v <= hi:
                raise ValueError(f"{label}: use {lo:g} to {hi:g}.")
            return int(v) if v == int(v) else v

        try:
            sc = {"race_on": bool(f.get("race_on")), "miles_on": bool(f.get("miles_on")), "days_on": bool(f.get("days_on")),
                  "points_on": bool(f.get("points_on")),
                  "race_points": [num(f"race_place_{i}", 0, 1000, f"Race points for place {i + 1}") for i in range(len(comp.squads))],
                  "race_scorers": int(num("race_scorers", 1, 10, "Runners who score")),
                  "miles_pct": num("miles_pct", 1, 200, "Miles share"), "miles_pts": num("miles_pts", 0, 100, "Miles points"),
                  "days_min": int(num("days_min", 1, 7, "Days run")), "days_pts": num("days_pts", 0, 100, "Days points"),
                  "points_pct": num("points_pct", 1, 200, "Practice points share"), "points_pts": num("points_pts", 0, 100, "Practice points")}
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back(f"/squads/{cid}#scoring")
        listed = set(f.getlist("race_listed"))
        ticked = set(f.getlist("race_count"))
        before = SQ.scoring(comp)["excluded_races"]
        sc["excluded_races"] = sorted({k for k in before if k not in listed} | (listed - ticked))
        comp.scoring = json.dumps(sc)
        flash(request, "Saved the scoring. The standings use it for every week.")
        return back(f"/squads/{cid}#scoring")

    @app.post("/squads/{cid}/delete")
    async def squads_delete(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        db.delete(get_comp(db, cid))
        flash(request, "Deleted the squad competition. Runs and races are unchanged.")
        return back("/squads?list=1")

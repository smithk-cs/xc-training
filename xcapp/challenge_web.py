"""Mileage challenges (challenge.py): coaches set one up and watch the leaderboard; athletes see it on their page."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import challenge as CH
from . import seasons as SE
from .ingest import log
from .models import MileageChallenge, SquadCompetition


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, coach_or_admin = h.any_staff, h.coach_or_admin

    @app.get("/challenge", response_class=HTMLResponse)
    def challenge_page(request: Request, id: int = 0, new: int = 0, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        today = A.team_today()
        all_ = db.scalars(select(MileageChallenge).order_by(MileageChallenge.start_date.desc())).all()
        c = next((x for x in all_ if x.id == id), None) or CH.current(db, today) or (all_[0] if all_ else None)
        summer = SE.period(SE.school_year_of(today) + (0 if today < SE.period(SE.school_year_of(today), "summer")["end"] else 1), "summer")
        return render(request, "challenge.html", user, c=c, challenges=all_, b=CH.board(db, c, today) if c else None, today=today,
                      summer=summer, squads=db.scalars(select(SquadCompetition).order_by(SquadCompetition.start_date.desc())).all(),
                      can_edit=user.role in ("admin", "coach"), edit=None if new else c)

    def parse(f) -> dict:
        try:
            start, end = dt.date.fromisoformat(str(f.get("start_date"))), dt.date.fromisoformat(str(f.get("end_date")))
        except ValueError:
            raise ValueError("Choose the first and last day.") from None
        if end < start:
            raise ValueError("The last day is before the first.")
        name = " ".join(str(f.get("name", "")).split())[:80] or "Mileage challenge"
        goal = str(f.get("goal_miles", "")).strip()
        try:
            goal_v = float(goal) if goal else None
        except ValueError:
            raise ValueError("The goal should be a number of miles, or empty.") from None
        if goal_v is not None and not 1 <= goal_v <= 2000:
            raise ValueError("The goal should be between 1 and 2,000 miles.")
        team = str(f.get("team", "All"))
        sq = str(f.get("squads_id", ""))
        return {"name": name, "start_date": start, "end_date": end, "goal_miles": goal_v,
                "team": team if team in ("Boys", "Girls") else "All", "squads_id": int(sq) if sq.isdigit() else None}

    @app.post("/challenge")
    async def challenge_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        cid = str(f.get("id", ""))
        c = db.get(MileageChallenge, int(cid)) if cid.isdigit() else None
        if f.get("action") == "delete" and c is not None:
            db.delete(c)
            flash(request, f"Deleted {c.name}.")
            return back("/challenge")
        try:
            vals = parse(f)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back("/challenge" + (f"?id={c.id}" if c else "") + "#setup")
        if c is None:
            c = MileageChallenge(**vals)
            db.add(c)
        else:
            for k, v in vals.items():
                setattr(c, k, v)
        db.flush()
        log(db, "app", f"{user.username} saved the mileage challenge {c.name}.")
        flash(request, f"Saved {c.name}. Athletes see it on their page while it runs.")
        return back(f"/challenge?id={c.id}")

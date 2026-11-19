"""Race-day pacing plans (pacing.py): the athlete's plan for a meet, and the coach's view of every entered athlete's."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import analysis as A
from . import pacing as PC
from .models import MeetEntry


def register(app, h: SimpleNamespace) -> None:
    render, get_db, any_staff, athlete_by_token = h.render, h.get_db, h.any_staff, h.athlete_by_token

    def meet_or_404(db: Session, mid: int) -> MeetEntry:
        m = db.get(MeetEntry, mid)
        if m is None:
            raise HTTPException(404, "Meet not found.")
        return m

    @app.get("/a/{token}/meets/{mid}/plan", response_class=HTMLResponse)
    def athlete_plan(request: Request, token: str, mid: int, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        m = meet_or_404(db, mid)
        today = A.team_today()
        if m.date < today:
            raise HTTPException(404, "That meet is over.")
        p = PC.plan(db, a, m, today)
        return render(request, "athlete_pacing.html", None, a=a, token=token, m=m, p=p, tips=PC.tips(p) if p else [])

    @app.get("/registrations/{mid}/plans", response_class=HTMLResponse)
    def meet_plans(request: Request, mid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        """Every entered athlete's predicted time and splits, by race."""
        m = meet_or_404(db, mid)
        today = A.team_today()
        ab = A.abilities(db, today)
        races = []
        for d in m.divisions:
            rows = []
            for e in sorted(d.entries, key=lambda e: e.position):
                p = PC.plan(db, e.athlete, m, today, vdot=ab.get(e.athlete_id, {}).get("vdot"), division=d)
                rows.append({"athlete": e.athlete, "plan": p})
            rows.sort(key=lambda r: r["plan"]["total"] if r["plan"] else 1e9)
            longest = max((r["plan"]["splits"] for r in rows if r["plan"]), key=len, default=[])
            races.append({"division": d, "rows": rows, "miles": len(longest),
                          "heads": [f"Mile {s['mile']}" if s["to"] - s["from"] >= 0.95 else "Finish" for s in longest]})
        return render(request, "pacing_meet.html", user, m=m, races=races)

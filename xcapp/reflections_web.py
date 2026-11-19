"""Post-race reflections (reflections.py): the athlete's form for one of their races, and a meet's reflections for
the coaches."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import reflections as RF
from .models import RaceResult


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, athlete_by_token = h.any_staff, h.athlete_by_token

    def own_race(db: Session, a, rid: int) -> RaceResult:
        r = db.get(RaceResult, rid)
        if r is None or r.athlete_id != a.id:
            raise HTTPException(404, "Race not found.")
        return r

    @app.get("/a/{token}/races/{rid}/reflect", response_class=HTMLResponse)
    def reflect_page(request: Request, token: str, rid: int, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        r = own_race(db, a, rid)
        return render(request, "athlete_reflect.html", None, a=a, token=token, r=r, ref=RF.by_race(db, a.id).get(r.id), happy=RF.HAPPY)

    @app.post("/a/{token}/races/{rid}/reflect")
    async def reflect_save(request: Request, token: str, rid: int, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        r = own_race(db, a, rid)
        try:
            RF.save(db, a, r, f.get("happy"), f.get("went_well"), f.get("change"), f.get("next_focus"))
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back(f"/a/{token}/races/{rid}/reflect")
        flash(request, f"Saved your reflection on {r.meet}. Your coaches will read it.")
        return back(f"/a/{token}#mr-h")

    @app.get("/races/reflections", response_class=HTMLResponse)
    def meet_reflections(request: Request, date: str = "", meet: str = "", user=Depends(any_staff, scope="function"),
                         db: Session = Depends(get_db, scope="function")):
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(404, "Meet not found.") from None
        races, refs = RF.for_meet(db, day, meet)
        return render(request, "race_reflections.html", user, day=day, meet=meet, races=races, refs=refs,
                      happy_label=lambda n: dict(RF.HAPPY).get(n, ""))

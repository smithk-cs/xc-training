"""The morning check-in (wellness.py) from the athlete's page."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from . import analysis as A
from . import wellness as WL


def register(app, h: SimpleNamespace) -> None:
    flash, back, get_db, form_checked, athlete_by_token = h.flash, h.back, h.get_db, h.form_checked, h.athlete_by_token

    @app.post("/a/{token}/checkin")
    async def athlete_checkin(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        try:
            WL.save(db, a, A.team_today(), f.get("sleep"), f.get("soreness"), f.get("mood"))
            flash(request, "Check-in saved. Thanks!")
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back(f"/a/{token}#checkin")
        return back(f"/a/{token}")

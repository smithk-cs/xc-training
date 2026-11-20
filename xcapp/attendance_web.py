"""Practice attendance (attendance.py): a coach taps each athlete in at practice; everyone on staff can read it."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import analysis as A
from . import attendance as AT
from . import seasons as SE


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, coach_or_admin, photo_versions = h.any_staff, h.coach_or_admin, h.photo_versions

    def pick_day(text: str, today: dt.date) -> dt.date:
        try:
            d = dt.date.fromisoformat(text)
        except (TypeError, ValueError):
            return today
        return min(d, today)

    @app.get("/attendance", response_class=HTMLResponse)
    def attendance_page(request: Request, date: str = "", team: str = "All", user=Depends(any_staff, scope="function"),
                        db: Session = Depends(get_db, scope="function")):
        today = A.team_today()
        d = pick_day(date, today)
        team = team if team in ("Boys", "Girls") else "All"
        people = AT.roster(db, team)
        ids = [a.id for a in people]
        marks, told = AT.day(db, d, ids)
        period = SE.period_for(d)
        until = min(period["end"], today)
        return render(request, "attendance.html", user, day=d, today=today, team=team, people=people, marks=marks, told=told,
                      statuses=AT.STATUSES, can_mark=user.role in ("admin", "coach"), period=period, until=until,
                      photos=photo_versions(db),
                      counts={"there": sum(1 for m in marks.values() if m.status in AT.THERE), "marked": len(marks)},
                      season=AT.summary(db, period["start"], until, ids), days=AT.marked_days(db, period["start"], until))

    @app.post("/attendance")
    async def attendance_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        today = A.team_today()
        d = pick_day(str(f.get("date", "")), today)
        team = str(f.get("team", "All"))
        choices = {int(x): str(f.get(f"s_{x}", "")) for x in f.getlist("athletes") if str(x).isdigit()}
        choices = {k: v for k, v in choices.items() if v}            # unmarked stays as it was
        n = AT.save(db, d, choices, user.username)
        flash(request, f"Saved attendance for {d:%a %b %-d}" + (f" ({n} change{'s' if n != 1 else ''})." if n else ": no changes."))
        return back(f"/attendance?date={d.isoformat()}&team={team}")

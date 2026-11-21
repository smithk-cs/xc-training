"""Time trial pages (time_trials.py): the list with an add form, and each trial's weather, results and time entry."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import analysis as A
from . import attendance as AT
from . import time_trials as TT
from . import weather as WX
from .ingest import log
from .models import TimeTrial


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, coach_or_admin = h.any_staff, h.coach_or_admin

    def trial_or_404(db: Session, tid: int) -> TimeTrial:
        t = db.get(TimeTrial, tid)
        if t is None:
            raise HTTPException(404, "Time trial not found.")
        return t

    def details(f) -> dict:
        try:
            day = dt.date.fromisoformat(str(f.get("date", "")))
        except ValueError:
            raise ValueError("Choose the day.") from None
        start = str(f.get("start_time", "")).strip() or "15:30"
        try:
            dt.time.fromisoformat(start)
        except ValueError:
            raise ValueError("The start time should look like 3:30 PM.") from None
        v, meters, unit = TT.distance(f.get("distance_value", ""), str(f.get("distance_unit", "m")))
        team = str(f.get("team", "All"))
        return {"date": day, "start_time": start[:5], "distance_value": v, "distance_m": meters, "distance_unit": unit,
                "name": " ".join(str(f.get("name", "")).split())[:80], "team": team if team in ("Boys", "Girls") else "All",
                "note": str(f.get("note", "")).strip()[:300]}

    def weather_now(db: Session, t: TimeTrial) -> bool:
        try:
            with WX.make_client() as http:
                return TT.fetch_weather(db, t, http, A.team_today())
        except WX.WeatherError:
            return False

    @app.get("/time-trials", response_class=HTMLResponse)
    def trials_page(request: Request, all: int = 0, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        today = A.team_today()
        trials = TT.listing(db, None if all else today - dt.timedelta(days=180))
        days: dict[dt.date, list] = {}
        for t in trials:
            days.setdefault(t.date, []).append(t)
        return render(request, "time_trials.html", user, days=days, today=today, all=all, units=TT.UNITS, tt=TT,
                      can_edit=user.role in ("admin", "coach"), place=WX.practice_place(db)[2])

    @app.post("/time-trials")
    async def trial_add(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            vals = details(f)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back("/time-trials#add")
        t = TimeTrial(**vals, created_by=user.username)
        db.add(t)
        db.flush()
        got = weather_now(db, t)
        log(db, "app", f"{user.username} added the {TT.title(t)} on {t.date}.")
        flash(request, f"Added the {TT.title(t)} for {t.date:%a %b %-d} at {A.fmt_clock(t.start_time)}."
              + ("" if got else " Weather isn’t available yet (more than two weeks ahead, or the service didn’t answer); it fills in nightly."))
        return back(f"/time-trials/{t.id}")

    @app.get("/time-trials/{tid}", response_class=HTMLResponse)
    def trial_page(request: Request, tid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        t = trial_or_404(db, tid)
        people = AT.roster(db, t.team)
        have = {r.athlete_id: r for r in t.results}
        return render(request, "time_trial.html", user, t=t, tt=TT, rows=TT.rows(t), people=people, have=have, units=TT.UNITS,
                      can_edit=user.role in ("admin", "coach"), weather=WX.describe(t, dew=True, humidity=True) if t.temp_f is not None else "")

    @app.post("/time-trials/{tid}/times")
    async def trial_times(request: Request, tid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        t = trial_or_404(db, tid)
        entries = {int(x): str(f.get(f"t_{x}", "")) for x in f.getlist("athletes") if str(x).isdigit()}
        n, problems = TT.save_results(db, t, entries)
        for p in problems:
            flash(request, p, "error")
        flash(request, f"Saved {n} time{'s' if n != 1 else ''}." if n else "No times changed.")
        return back(f"/time-trials/{t.id}")

    @app.post("/time-trials/{tid}/edit")
    async def trial_edit(request: Request, tid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        t = trial_or_404(db, tid)
        if f.get("action") == "delete":
            day = t.date
            db.delete(t)
            log(db, "app", f"{user.username} deleted a time trial on {day}.")
            flash(request, f"Deleted the time trial on {day:%b %-d} and its times.")
            return back("/time-trials")
        if f.get("action") == "weather":
            flash(request, "Weather updated." if weather_now(db, t) else "The weather service had nothing for that time yet.")
            return back(f"/time-trials/{t.id}")
        try:
            vals = details(f)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back(f"/time-trials/{t.id}#edit")
        moved = (vals["date"], vals["start_time"]) != (t.date, t.start_time)
        for k, v in vals.items():
            setattr(t, k, v)
        if moved:
            weather_now(db, t)
        flash(request, "Saved.")
        return back(f"/time-trials/{t.id}")

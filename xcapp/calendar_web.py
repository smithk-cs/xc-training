"""Team calendar pages: coaches manage practices and events; athletes and families view and subscribe by a private link."""
from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from . import analysis as A
from . import team_calendar as TC
from .ingest import log
from .models import CalendarEvent

TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def links(base_url: str, token: str) -> dict[str, dict]:
    base = base_url.rstrip("/")
    out = {}
    for team in TC.TEAMS:
        feed = f"{base}/team-calendar/{token}/{team.lower()}.ics"
        out[team] = {"page": f"{base}/team-calendar/{token}?team={team}", "feed": feed,
                     "webcal": "webcal://" + feed.split("://", 1)[1]}
    return out


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, form_checked, get_db = h.render, h.flash, h.back, h.form_checked, h.get_db
    any_staff, coach_or_admin, s = h.any_staff, h.coach_or_admin, h.settings

    def check_token(db: Session, token: str) -> None:
        import hmac
        if not hmac.compare_digest(token, TC.token(db)):
            raise HTTPException(404, "This calendar link has changed. Ask a coach for the new one.")

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_manage(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        today = A.team_today()
        team = request.query_params.get("team") if request.query_params.get("team") in TC.TEAMS else "All"
        edit = db.get(CalendarEvent, int(request.query_params["edit"])) if request.query_params.get("edit", "").isdigit() else None
        from . import weather as WX
        return render(request, "calendar_manage.html", user, items=TC.items(db, today, today + dt.timedelta(days=60), team), team=team,
                      practice_place=WX.practice_place(db), default_time=A.fmt_clock(WX.DEFAULT_PRACTICE_TIME),
                      links=links(s.base_url, TC.token(db)), kinds=TC.KINDS, weekdays=TC.WEEKDAYS, today=today, edit=edit,
                      series=sorted((e for e in db.query(CalendarEvent).all() if e.repeat_days and e.repeat_until and e.repeat_until >= today),
                                    key=lambda e: e.date))

    def event_form(f, e: CalendarEvent) -> None:
        e.title = str(f.get("title", "")).strip()[:120]
        if not e.title:
            raise ValueError("Give it a name, such as Practice or Team dinner.")
        e.kind = str(f.get("kind")) if f.get("kind") in TC.KINDS else "Practice"
        e.team = str(f.get("team")) if f.get("team") in ("Boys", "Girls") else ""
        e.date = dt.date.fromisoformat(str(f.get("date", "")))
        e.start_time, e.end_time = (str(f.get(k, "")).strip()[:5] for k in ("start_time", "end_time"))
        for t in (e.start_time, e.end_time):
            if t and not TIME.fullmatch(t):
                raise ValueError("Times look like 15:30.")
        if e.end_time and not e.start_time:
            raise ValueError("Add a start time too, or leave both empty for an all-day event.")
        e.location = str(f.get("location", "")).strip()[:160]
        e.notes = str(f.get("notes", "")).strip()[:300]
        days = sorted({int(x) for x in f.getlist("repeat") if str(x).isdigit() and 0 <= int(x) <= 6})
        until = str(f.get("repeat_until", "")).strip()
        if days and until:
            e.repeat_days, e.repeat_until = ",".join(map(str, days)), dt.date.fromisoformat(until)
            if e.repeat_until < e.date:
                raise ValueError("“Repeat until” is before the first day.")
            if (e.repeat_until - e.date).days > 370:
                raise ValueError("Repeat for up to a year at a time.")
        elif days and not until:
            raise ValueError("Pick the last day it repeats until.")
        else:
            e.repeat_days, e.repeat_until = "", None

    @app.post("/calendar/events")
    async def calendar_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        e = db.get(CalendarEvent, int(f["eid"])) if str(f.get("eid", "")).isdigit() else None
        new = e is None
        if new:
            e = CalendarEvent(created_by=user.username)
        try:
            event_form(f, e)
            if new:
                db.add(e)
            flash(request, f"{'Added' if new else 'Saved'} {e.title}. Subscribed calendars pick it up at their next refresh.")
        except ValueError as exc:
            db.rollback()
            msg = str(exc)
            flash(request, msg if msg.endswith(".") else "Check the dates and times.", "error")
        return back("/calendar")

    @app.post("/calendar/events/{eid}/skip")
    async def calendar_skip(request: Request, eid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        e = db.get(CalendarEvent, eid)
        day = str(f.get("date", ""))
        if e is not None and re.fullmatch(r"\d{4}-\d\d-\d\d", day):
            if e.repeat_days:
                e.skip_dates = ",".join(sorted({*filter(None, (e.skip_dates or "").split(",")), day}))
                flash(request, f"Cancelled {e.title} on {dt.date.fromisoformat(day):%a %b %-d}.")
            else:
                db.delete(e)
                flash(request, f"Removed {e.title}.")
        return back("/calendar")

    @app.post("/calendar/events/{eid}/delete")
    async def calendar_delete(request: Request, eid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        e = db.get(CalendarEvent, eid)
        if e is not None:
            db.delete(e)
            flash(request, f"Removed {e.title}{' and all its repeats' if e.repeat_days else ''}.")
        return back("/calendar")

    @app.post("/calendar/practice-place")
    async def calendar_practice_place(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        """Where practice usually is, for practice forecasts: typed coordinates, or found on the map from a name."""
        from . import weather as WX
        f = await form_checked(request)
        place = str(f.get("place", "")).strip()[:120]
        try:
            if f.get("action") == "find":
                with WX.make_client() as http:
                    hit = WX.geocode(http, place)
                if hit is None:
                    raise ValueError(f"Couldn’t find “{place}” on the map. Try the town, or paste the coordinates from Google Maps.")
                lat, lon = hit[0], hit[1]
            elif f.get("action") == "default":
                lat, lon, place = WX.DEFAULT_PRACTICE
            else:
                lat, lon = float(f.get("lat", "")), float(f.get("lon", ""))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Latitude is −90 to 90 and longitude −180 to 180.")
            for k, v in (("practice_lat", f"{lat:.5f}"), ("practice_lon", f"{lon:.5f}"), ("practice_place", place or "Practice")):
                A.set_setting(db, k, v)
            flash(request, f"Practice forecasts now use {place or 'this place'} ({lat:.4f}, {lon:.4f}).")
        except (ValueError, WX.WeatherError) as exc:
            msg = str(exc)
            flash(request, msg if msg.endswith(".") else "Latitude and longitude are numbers, such as 38.6171 and −121.3283.", "error")
        return back("/calendar#practice-place")

    @app.post("/calendar/new-link")
    async def calendar_new_link(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        TC.new_token(db)
        log(db, "calendar", f"{user.username} made new team calendar links; the old ones stopped working.")
        flash(request, "New calendar links made. The old links and subscriptions stop updating: share the new ones.")
        return back("/calendar#share")

    # ---------- for athletes and families: no sign-in, a private link ----------

    @app.get("/team-calendar/{token}", response_class=HTMLResponse)
    def calendar_public(request: Request, token: str, team: str = "All", db: Session = Depends(get_db, scope="function")):
        check_token(db, token)
        team = team if team in TC.TEAMS else "All"
        today = A.team_today()
        its = TC.items(db, today, today + dt.timedelta(days=120), team)
        months: dict[str, list] = {}
        for x in its:
            months.setdefault(x["date"].strftime("%B %Y"), []).append(x)
        return render(request, "team_calendar.html", None, months=months, team=team, token=token, links=links(s.base_url, token),
                      today=today)

    @app.get("/team-calendar/{token}/{team}.ics")
    def calendar_feed(token: str, team: str, db: Session = Depends(get_db, scope="function")):
        check_token(db, token)
        t = {x.lower(): x for x in TC.TEAMS}.get(team.lower())
        if t is None:
            raise HTTPException(404, "No such calendar.")
        body = TC.ics(db, t, s.team_name, A.team_today())
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'inline; filename="{t.lower()}-team-calendar.ics"', "Cache-Control": "no-cache"})

"""The team calendar: practices and events entered by coaches, plus meets from the Entries page, for boys, girls or
both. Shared with athletes and families by a private link (no account), and as an iCalendar feed that iPhone, Google
and Outlook calendars subscribe to, so changes reach them on their own. It holds no information about any athlete."""
from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .config import settings
from .models import CalendarEvent, MeetEntry, PlannedWorkout, utcnow

TEAMS = ("All", "Boys", "Girls")
KINDS = ("Practice", "Event")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TOKEN_KEY = "calendar_token"
FEED_PAST, FEED_AHEAD = 60, 400        # days of past and coming events in the subscribed feed
DEFAULT_LENGTH = {"Practice": 120, "Event": 120, "Meet": 240, "Workout": 120}   # minutes, when no end time is given


def token(db: Session) -> str:
    t = A.get_setting(db, TOKEN_KEY, "")
    if not t:
        t = new_token(db)
    return t


def new_token(db: Session) -> str:
    t = secrets.token_urlsafe(18)
    A.set_setting(db, TOKEN_KEY, t)
    return t


def for_team(item_team: str, team: str) -> bool:
    return team == "All" or not item_team or item_team == team


def days_of(e: CalendarEvent, start: dt.date, end: dt.date) -> list[dt.date]:
    """The days an event happens between start and end (inclusive)."""
    skip = {s.strip() for s in (e.skip_dates or "").split(",") if s.strip()}
    if not e.repeat_days or not e.repeat_until:
        return [e.date] if start <= e.date <= end and e.date.isoformat() not in skip else []
    wd = {int(x) for x in e.repeat_days.split(",") if x.strip().isdigit()}
    out, d = [], max(start, e.date)
    last = min(end, e.repeat_until)
    while d <= last:
        if d.weekday() in wd and d.isoformat() not in skip:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def meet_team(m: MeetEntry) -> str:
    genders = {d.gender for d in m.divisions}
    return next(iter(genders)) if len(genders) == 1 else ""


def items(db: Session, start: dt.date, end: dt.date, team: str = "All") -> list[dict]:
    """Everything on the calendar between start and end for a team, in order."""
    from .weather import course_for
    out = []
    for e in db.scalars(select(CalendarEvent)).all():
        if not for_team(e.team, team):
            continue
        for d in days_of(e, start, end):
            out.append({"uid": f"event-{e.id}-{d.isoformat()}", "event_id": e.id, "date": d, "start": e.start_time, "end": e.end_time,
                        "title": e.title, "kind": e.kind, "team": e.team, "location": e.location, "notes": e.notes,
                        "repeats": bool(e.repeat_days)})
    for m in db.scalars(select(MeetEntry).where(MeetEntry.date >= start, MeetEntry.date <= end)).all():
        t = meet_team(m)
        if not for_team(t, team):
            continue
        c = course_for(db, m)
        # The course and its address (the course's saved location, else the meet's), for the map link.
        address = (c.location if c and c.location else m.location) or ""
        where = ", ".join(x for x in ((c.name if c else ""), address if not c or address != c.name else "") if x)
        out.append({"uid": f"meet-{m.id}", "meet_id": m.id, "date": m.date, "start": m.start_time, "end": "", "title": m.name,
                    "kind": "Meet", "team": t, "location": where, "course": c.name if c else "", "address": address,
                    "notes": "", "repeats": False})
    for w in db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= start, PlannedWorkout.date <= end)).all():
        if not for_team(w.gender, team):
            continue
        groups = [g.strip() for g in (w.groups or "All").split(",") if g.strip() and g.strip() != "All"]
        parts = [f"{A.part_label(p)} {p.get('zone', '')}".strip() + (f" ({p['rest']} rest)" if p.get("rest") else "")
                 + (f" – {p['note']}" if p.get("note") else "") for p in w.part_list] or [w.distance_text]
        out.append({"uid": f"workout-{w.id}", "workout_id": w.id, "date": w.date, "start": "", "end": "", "title": w.title,
                    "kind": "Workout", "team": w.gender, "location": "", "parts": parts, "groups": groups,
                    "notes": "; ".join(parts) + (f" (for {', '.join(groups)})" if groups else ""), "repeats": False})
    out.sort(key=lambda x: (x["date"], x["start"] or "00:00", x["title"]))
    return out


# ---------- iCalendar (RFC 5545) ----------

def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """Lines longer than 75 bytes continue on the next line after a space."""
    out, b = [], line.encode()
    while len(b) > 75:
        cut = 75 if not out else 74
        while cut and (b[cut] & 0xC0) == 0x80:   # don't split a multi-byte character
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return [out[0]] + [" " + x for x in out[1:]]


def _utc(d: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=settings().tz).astimezone(dt.timezone.utc)


def ics(db: Session, team: str, team_name: str, today: dt.date) -> str:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    name = f"{team_name}{'' if team == 'All' else ' ' + team}"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//XC Training//Team calendar//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(name)}", f"X-WR-TIMEZONE:{settings().team_timezone}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]   # ask calendar apps to check hourly
    for x in items(db, today - dt.timedelta(days=FEED_PAST), today + dt.timedelta(days=FEED_AHEAD), team):
        lines += ["BEGIN:VEVENT", f"UID:{x['uid']}@xc-training", f"DTSTAMP:{stamp}"]
        if x["start"]:
            start = _utc(x["date"], x["start"])
            end = _utc(x["date"], x["end"]) if x["end"] and x["end"] > x["start"] else start + dt.timedelta(minutes=DEFAULT_LENGTH[x["kind"]])
            lines += [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{x['date']:%Y%m%d}", f"DTEND;VALUE=DATE:{x['date'] + dt.timedelta(days=1):%Y%m%d}"]
        title = f"{x['kind']}: {x['title']}" if x["kind"] in ("Meet", "Workout") else x["title"]
        lines.append(f"SUMMARY:{_esc(title + ('' if team != 'All' or not x['team'] else ' (' + x['team'] + ')'))}")
        if x["location"]:
            lines.append(f"LOCATION:{_esc(x['location'])}")
        if x["notes"]:
            lines.append(f"DESCRIPTION:{_esc(x['notes'])}")
        lines.append(f"CATEGORIES:{x['kind']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"

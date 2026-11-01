"""The parts of the school year: Summer Base, Cross Country, Winter Base and Track & Field.

Coaches set each part's dates (and may rename it) on the Calendar page; parts not saved follow the usual dates:
- Cross Country: the first Monday in August to the first Saturday in December;
- Winter Base: from then to the day before track starts;
- Track & Field: the second Monday in February to the first Saturday in June;
- Summer Base: from then to the day before cross country.
The Cross Country dates also decide which races and results belong to "2026 XC" rather than to track.
Saved dates are kept in memory and read again at most every 30 seconds (and right after a coach saves).
"""
from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import text

KINDS = ("summer", "xc", "winter", "track")
NAMES = {"summer": "Summer Base", "xc": "Cross Country", "winter": "Winter Base", "track": "Track & Field"}
REFRESH_S = 30

_cache: dict = {"key": None, "at": 0.0, "rows": {}}


def _first(year: int, month: int, weekday: int, nth: int = 1) -> dt.date:
    d = dt.date(year, month, 1)
    return d + dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (nth - 1))


def default_dates(school_year: int, kind: str) -> tuple[dt.date, dt.date]:
    y = school_year
    xc = (_first(y, 8, 0), _first(y, 12, 5))
    track = (_first(y + 1, 2, 0, 2), _first(y + 1, 6, 5))
    if kind == "xc":
        return xc
    if kind == "track":
        return track
    if kind == "winter":
        return xc[1] + dt.timedelta(days=1), track[0] - dt.timedelta(days=1)
    return _first(y, 6, 5) + dt.timedelta(days=1), xc[0] - dt.timedelta(days=1)   # summer


def load(conn) -> dict[tuple[int, str], tuple[str, dt.date, dt.date]]:
    """Saved periods, read through any connection or session."""
    def day(v):
        return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])
    try:
        rows = conn.execute(text("SELECT school_year, kind, name, start_date, end_date FROM season_periods")).all()
    except Exception:   # table not made yet
        return {}
    return {(int(y), k): (n, day(s), day(e)) for y, k, n, s, e in rows}


def _saved() -> dict:
    from . import db as D
    key = id(D._engine) if D._engine is not None else None
    if key is None:
        return {}
    if _cache["key"] != key or time.monotonic() - _cache["at"] > REFRESH_S:
        with D._engine.connect() as conn:
            _cache.update(key=key, at=time.monotonic(), rows=load(conn))
    return _cache["rows"]


def forget() -> None:
    _cache.update(key=None, at=0.0, rows={})


def refresh(conn=None) -> None:
    """Read the saved dates again now (after an edit, or during the database upgrade through its connection)."""
    if conn is not None:
        from . import db as D
        _cache.update(key=id(D._engine) if D._engine is not None else None, at=time.monotonic(), rows=load(conn))
    else:
        _cache["at"] = 0.0


def period(school_year: int, kind: str) -> dict:
    saved = _saved().get((school_year, kind))
    if saved:
        start, end = saved[1], saved[2]
    elif kind == "winter":     # the gap between this year's cross country and track
        start, end = period(school_year, "xc")["end"] + dt.timedelta(days=1), period(school_year, "track")["start"] - dt.timedelta(days=1)
    elif kind == "summer":     # from the end of last year's track to cross country
        start = period(school_year - 1, "track")["end"] + dt.timedelta(days=1)
        end = period(school_year, "xc")["start"] - dt.timedelta(days=1)
    else:
        start, end = default_dates(school_year, kind)
    return {"school_year": school_year, "kind": kind, "name": saved[0] if saved else NAMES[kind], "start": start, "end": end,
            "saved": bool(saved), "label": f"{start.year} {saved[0] if saved else NAMES[kind]}"}


def school_year_of(d: dt.date) -> int:
    """The school year a day belongs to: summer base onward counts toward the coming year."""
    return d.year if d >= period(d.year, "summer")["start"] else d.year - 1


def year_periods(school_year: int) -> list[dict]:
    return [period(school_year, k) for k in KINDS]


def period_for(d: dt.date) -> dict:
    """The part of the school year a day falls in. Saved dates win; a day no part covers belongs to the nearest
    earlier part."""
    best = None
    for y in (d.year - 1, d.year):
        for p in year_periods(y):
            if p["start"] <= d <= p["end"]:
                return p
            if p["start"] <= d and (best is None or p["start"] > best["start"]):
                best = p
    return best or period(d.year, "summer")


def xc_dates(year: int) -> tuple[dt.date, dt.date]:
    p = period(year, "xc")
    return p["start"], p["end"]

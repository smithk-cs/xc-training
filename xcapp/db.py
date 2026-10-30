"""Database engine and sessions. SQLite by default; set DATABASE_URL for Postgres."""
from __future__ import annotations

import datetime as dt
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None


def init_engine(url: str):
    global _engine, _Session
    kwargs = {}
    if url.startswith("sqlite"):
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _fk_on(dbapi_conn, _):  # enforce foreign keys and cascades in SQLite
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def engine():
    if _engine is None:
        from .config import settings
        init_engine(settings().database_url)
    return _engine


# Jesuit's training groups (VDOT JHS sheet): name, boys' and girls' minimum VDOT, and a color that looks like the name.
DEFAULT_GROUPS = [("Platinum", 73, 73, "#E5E4E2"), ("Gold", 68, 68, "#D4AF37"), ("Silver", 62, 62, "#C0C0C0"),
                  ("Bronze", 58, 58, "#CD7F32"), ("Emerald", 54, 54, "#50C878"), ("Aquamarine", 49, 49, "#7FDBCA"),
                  ("Ivory", 0, 0, "#FFFFF0")]
# The sheet's original pale colors; groups still on them are moved to the colors above (a coach's own choice is kept).
SHEET_COLORS = {"Platinum": "#EFEFEF", "Gold": "#FFE599", "Silver": "#D9D9D9", "Bronze": "#F9CB9C", "Emerald": "#B6D7A8",
                "Aquamarine": "#CFE2F3", "Ivory": "#FFFFFF"}
# The team's target 800 and 1600 times by VDOT (VDOT JHS sheet).
DEFAULT_TARGETS = {74: ("1:50", "4:06"), 73: ("1:51", "4:09"), 72: ("1:52", "4:12"), 71: ("1:53", "4:15"), 70: ("1:54", "4:19"),
                   69: ("1:56", "4:23"), 68: ("1:58", "4:26"), 67: ("2:00", "4:30"), 66: ("2:01", "4:33"), 65: ("2:03", "4:37"),
                   64: ("2:05", "4:41"), 63: ("2:07", "4:45"), 62: ("2:09", "4:49"), 61: ("2:11", "4:53"), 60: ("2:13", "4:57"),
                   59: ("2:15", "5:02"), 58: ("2:17", "5:06"), 57: ("2:19", "5:11"), 56: ("2:21", "5:16"), 55: ("2:23", "5:21"),
                   54: ("2:25", "5:27"), 53: ("2:27", "5:32"), 52: ("2:29", "5:38"), 51: ("2:31", "5:44"), 50: ("2:33", "5:50"),
                   49: ("2:35", "5:56"), 48: ("2:37", "6:03")}

# Columns added after the first release, added in place to an existing database.
ADDED_COLUMNS = {
    "activities": [("rpe", "INTEGER"), ("feel", "VARCHAR(20) DEFAULT ''"), ("athlete_note", "VARCHAR(300) DEFAULT ''"),
                   ("feedback_at", "TIMESTAMP"), ("run_type", "VARCHAR(20) DEFAULT ''"), ("workout_locked", "BOOLEAN DEFAULT FALSE"),
                   ("vo2_max", "FLOAT"), ("workout_check", "VARCHAR(12) DEFAULT ''"), ("workout_check_note", "VARCHAR(400) DEFAULT ''"),
                   ("workout_check_for", "INTEGER"), ("workout_checked_at", "TIMESTAMP"), ("zone_seconds", "TEXT DEFAULT ''"),
                   ("zone_vdot", "INTEGER")],
    "athletes": [("ability_override", "VARCHAR(40) DEFAULT ''"), ("ai_consent_at", "TIMESTAMP"), ("ai_consent_note", "VARCHAR(200) DEFAULT ''"),
                 ("vdot_override", "INTEGER"), ("is_test", "BOOLEAN DEFAULT FALSE"), ("other_names", "VARCHAR(500) DEFAULT ''"), ("email", "VARCHAR(254) DEFAULT ''"), ("email_reminders", "BOOLEAN DEFAULT FALSE"), ("grad_year", "INTEGER"),
                 ("alumni", "BOOLEAN DEFAULT FALSE"), ("left_on", "DATE"), ("vdot_mark", "VARCHAR(10) DEFAULT ''"), ("weekly_plan", "VARCHAR(20) DEFAULT ''"),
                 ("experience", "INTEGER DEFAULT 2"), ("show_name", "BOOLEAN DEFAULT FALSE"), ("show_photo", "BOOLEAN DEFAULT FALSE"),
                 ("show_spotlight", "BOOLEAN DEFAULT FALSE"), ("show_vdot", "BOOLEAN DEFAULT FALSE"), ("consent_by", "VARCHAR(160) DEFAULT ''"),
                 ("consent_at", "TIMESTAMP")],
    "staff_users": [("email", "VARCHAR(254) DEFAULT ''"), ("full_name", "VARCHAR(120) DEFAULT ''"), ("title", "VARCHAR(80) DEFAULT ''"),
                    ("phone", "VARCHAR(30) DEFAULT ''"), ("on_entries", "BOOLEAN DEFAULT TRUE")],
    "meet_entries": [("course_id", "INTEGER")],
    "courses": [("video_url", "VARCHAR(500) DEFAULT ''"), ("distances", "VARCHAR(200) DEFAULT ''")],
    "meet_divisions": [("gender", "VARCHAR(10) DEFAULT 'Boys'"), ("level", "VARCHAR(20) DEFAULT 'Varsity'"), ("max_entries", "INTEGER")],
    "race_results": [("points", "FLOAT")],
    "planned_workouts": [("distance_unit", "VARCHAR(2) DEFAULT 'm'"), ("parts", "TEXT DEFAULT ''"), ("gender", "VARCHAR(10) DEFAULT ''")],
    "training_groups": [("color", "VARCHAR(7) DEFAULT ''"), ("weekly_miles", "FLOAT DEFAULT 0"), ("weekly_miles_girls", "FLOAT")],
    "pain_reports": [("activity_id", "INTEGER"), ("saw_trainer", "BOOLEAN DEFAULT FALSE")],
}


def create_all():
    from sqlalchemy import inspect, text
    from . import models  # noqa: F401  (register tables)
    eng = engine()
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    with eng.begin() as conn:
        for table, cols in ADDED_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        if eng.dialect.name == "postgresql":  # room for the notes of merged same-day pain reports (SQLite has no limit)
            conn.execute(text("ALTER TABLE pain_reports ALTER COLUMN note TYPE VARCHAR(1000)"))
        # Groups saved when boys and girls shared one weekly mileage goal: the girls start on that same goal.
        conn.execute(text("UPDATE training_groups SET weekly_miles_girls = COALESCE(weekly_miles, 0) WHERE weekly_miles_girls IS NULL"))
        # Starting ability groups; coaches rename and re-set them on the Groups page.
        if not conn.execute(text("SELECT COUNT(*) FROM training_groups")).scalar():
            for pos, (name, boys, girls, color) in enumerate(DEFAULT_GROUPS):
                conn.execute(text("INSERT INTO training_groups (name, position, min_vdot_boys, min_vdot_girls, color) VALUES (:n, :p, :b, :g, :c)"),
                             {"n": name, "p": pos, "b": boys, "g": girls, "c": color})
        for name, _b, _g, color in DEFAULT_GROUPS:
            conn.execute(text("UPDATE training_groups SET color = :new WHERE name = :name AND UPPER(color) = :old"),
                         {"new": color, "name": name, "old": SHEET_COLORS[name]})
        if not conn.execute(text("SELECT COUNT(*) FROM app_settings WHERE key = 'vdot_targets'")).scalar():
            import json
            conn.execute(text("INSERT INTO app_settings (key, value) VALUES ('vdot_targets', :v)"),
                         {"v": json.dumps({str(k): list(v) for k, v in DEFAULT_TARGETS.items()})})
        # Times saved under the old season dates (cross country Jul-Nov) move to the current ones (first Monday in August
        # to the first Saturday in December), such as December championships from track back to cross country.
        from .analysis import season_for
        for rid, day, season in conn.execute(text("SELECT id, date, season FROM results")).all():
            day = day if isinstance(day, dt.date) else dt.date.fromisoformat(str(day)[:10])
            if season_for(day) != season:
                conn.execute(text("UPDATE results SET season = :s WHERE id = :i"), {"s": season_for(day), "i": rid})
        # Races saved before they had team and division fields: read them from the race name (such as 'Boys JV').
        rows = conn.execute(text("SELECT id, name, gender, level FROM meet_divisions")).all()
        for rid, name, gender, level in rows:
            if name == f"{gender} {level}":
                continue
            g = "Girls" if (name or "").lower().startswith(("girl", "women")) else "Boys"
            lv = next((x for x in ("Frosh/Soph", "Freshman", "Sophomore", "Varsity", "JV") if x.lower() in (name or "").lower()), "Varsity")
            if (g, lv) != (gender, level):
                conn.execute(text("UPDATE meet_divisions SET gender = :g, level = :l WHERE id = :i"), {"g": g, "l": lv, "i": rid})


def SessionLocal():
    engine()
    return _Session()


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

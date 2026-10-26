"""Database engine and sessions. SQLite by default; set DATABASE_URL for Postgres."""
from __future__ import annotations

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
                   ("feedback_at", "TIMESTAMP"), ("run_type", "VARCHAR(20) DEFAULT ''"), ("workout_locked", "BOOLEAN DEFAULT FALSE")],
    "athletes": [("ability_override", "VARCHAR(40) DEFAULT ''"), ("ai_consent_at", "TIMESTAMP"), ("ai_consent_note", "VARCHAR(200) DEFAULT ''")],
    "training_groups": [("color", "VARCHAR(7) DEFAULT ''")],
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

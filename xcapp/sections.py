"""The sections of the app (the menu's tabs), which an admin can turn off for a coach or trainer account.

A section turned off disappears from that person's menu, and its pages (and the actions on them) answer
"no access". Admins always have every section; Staff (accounts) is for admins only anyway.
"""
from __future__ import annotations

# key, menu label, the addresses that belong to it
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("dashboard", "Dashboard", ("/dashboard",)),
    ("athletes", "Athletes", ("/athletes", "/api/athletes")),
    ("charts", "Charts", ("/charts", "/api/charts")),
    ("races", "Races", ("/races",)),
    ("trials", "Time trials", ("/time-trials",)),
    ("courses", "Courses", ("/courses",)),
    ("entries", "Entries", ("/registrations",)),
    ("goals", "Goals", ("/results", "/goals")),
    ("workouts", "Workouts", ("/workouts",)),
    ("week", "Week", ("/week",)),
    ("attendance", "Attendance", ("/attendance",)),
    ("squads", "Squads", ("/squads",)),
    ("challenge", "Challenge", ("/challenge",)),
    ("calendar", "Calendar", ("/calendar",)),
    ("raceday", "Race Day", ("/race-day",)),
    ("form", "Running form", ("/form",)),
    ("vdot", "VDOT", ("/vdot",)),
    ("reports", "Reports", ("/reports",)),
    ("newsletter", "Newsletter", ("/newsletter",)),
    ("groups", "Groups", ("/groups",)),
    ("injuries", "Injuries", ("/injuries", "/missed")),
    ("ai", "AI", ("/ai",)),
    ("sync", "Sync log", ("/sync", "/data-check")),
)
KEYS = {k for k, _, _ in SECTIONS}
# The dashboard's data feeds (/api/datasets/<name>.csv, datasets.COLUMNS) share one address, so each names its section here.
DATASETS = {"roster": "athletes", "benchmarks": "charts", "goals": "goals", "weekly_load": "dashboard",
            "injuries": "injuries", "pain_reports": "injuries", "segments": "workouts", "ai_insights": "ai"}
LABELS = {k: label for k, label, _ in SECTIONS}

# Archived sections: out of everyone's menu (admins too) and their pages closed, with their data kept, until an admin
# brings them back on the Staff page. Race Day was archived in November 2026; until the list is first saved, that's it.
ARCHIVED_KEY = "archived_sections"
ARCHIVED_DEFAULT = "raceday"
PUBLIC_PREFIXES = {"raceday": ("/rd/", "/rw/")}     # links that work without signing in (Race Day's tap and watch links)


def archived(db) -> set[str]:
    from .analysis import get_setting
    return {k for k in get_setting(db, ARCHIVED_KEY, ARCHIVED_DEFAULT).split(",") if k in KEYS}


def set_archived(db, keys) -> None:
    from .analysis import set_setting
    set_setting(db, ARCHIVED_KEY, ",".join(k for k, _, _ in SECTIONS if k in set(keys)))


def public_section(path: str) -> str | None:
    for key, prefixes in PUBLIC_PREFIXES.items():
        if path.startswith(prefixes):
            return key
    return None


def section_for(path: str) -> str | None:
    """The section a page address belongs to ('/newsletter-tips' is Newsletter, '/races/edit-meet' is Races)."""
    for key, _, prefixes in SECTIONS:
        for p in prefixes:
            if path == p or path.startswith(p + "/") or path.startswith(p + "-") or path.startswith(p + "."):
                return key
    return None

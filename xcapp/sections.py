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
    ("courses", "Courses", ("/courses",)),
    ("entries", "Entries", ("/registrations",)),
    ("goals", "Goals", ("/results", "/goals")),
    ("workouts", "Workouts", ("/workouts",)),
    ("week", "Week", ("/week",)),
    ("squads", "Squads", ("/squads",)),
    ("calendar", "Calendar", ("/calendar",)),
    ("vdot", "VDOT", ("/vdot",)),
    ("reports", "Reports", ("/reports",)),
    ("newsletter", "Newsletter", ("/newsletter",)),
    ("groups", "Groups", ("/groups",)),
    ("injuries", "Injuries", ("/injuries", "/missed")),
    ("ai", "AI", ("/ai",)),
    ("sync", "Sync log", ("/sync", "/data-check")),
)
KEYS = {k for k, _, _ in SECTIONS}
LABELS = {k: label for k, label, _ in SECTIONS}


def section_for(path: str) -> str | None:
    """The section a page address belongs to ('/newsletter-tips' is Newsletter, '/races/edit-meet' is Races)."""
    for key, _, prefixes in SECTIONS:
        for p in prefixes:
            if path == p or path.startswith(p + "/") or path.startswith(p + "-") or path.startswith(p + "."):
                return key
    return None

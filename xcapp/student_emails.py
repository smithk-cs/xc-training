"""School emails pasted in bulk, matched to athletes by the school's pattern: last name, first initial and the last two
digits of the graduation year (albertsl27@student.jhs.net is Alberts, L..., class of 2027). Last names are compared
by their letters only, so Saint-Evens, Saint Evens and SaintEvens all match saintevensb28. An address that fits no
athlete, or more than one, is listed for the coach rather than guessed."""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import classyear as CY
from .models import Athlete

EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LOCAL = re.compile(r"([a-z]+)([a-z])(\d{2})")


def letters(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def parse(text: str) -> list[str]:
    """The email addresses in the pasted text, each once, in order."""
    seen, out = set(), []
    for e in EMAIL.findall(text or ""):
        e = e.strip(".").lower()
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def match(db: Session, emails: list[str], today: dt.date) -> list[dict]:
    """[{"email", "status", "athlete", "candidates"}]; status: "match" (one athlete fits), "none", "many", or "odd"
    (the address doesn't follow the last name + initial + year pattern)."""
    people = [a for a in db.scalars(select(Athlete).where(Athlete.is_test.is_(False))).all()]
    out = []
    for e in emails:
        m = LOCAL.fullmatch(e.split("@", 1)[0])
        if not m:
            out.append({"email": e, "status": "odd", "athlete": None, "candidates": []})
            continue
        last, initial, yy = m.group(1), m.group(2), int(m.group(3))
        year = 2000 + yy

        def grad(a):
            return a.grad_year or (CY.class_of(a.grade, today) if a.grade else None)
        fits = [a for a in people if letters(a.last_name) == last and letters(a.first_name)[:1] == initial and grad(a) == year]
        if not fits:   # a hyphenated or two-part last name written differently, or the first and last swapped
            fits = [a for a in people if letters(a.first_name)[:1] == initial and grad(a) == year
                    and (letters(a.last_name).startswith(last) or last.startswith(letters(a.last_name) or "#"))]
        status = "match" if len(fits) == 1 else "none" if not fits else "many"
        out.append({"email": e, "status": status, "athlete": fits[0] if len(fits) == 1 else None, "candidates": fits})
    return out


def apply(rows: list[dict], replace: bool) -> dict:
    """Save the matched emails: {"saved", "same", "kept" (a different email already on file, not replaced)}."""
    saved, same, kept = [], [], []
    for r in rows:
        a = r["athlete"]
        if r["status"] != "match":
            continue
        if (a.email or "").lower() == r["email"]:
            same.append(r)
        elif a.email and not replace:
            kept.append(r)
        else:
            a.email = r["email"]
            saved.append(r)
    return {"saved": saved, "same": same, "kept": kept}

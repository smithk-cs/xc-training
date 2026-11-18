"""School emails pasted in bulk, matched to athletes by the school's pattern: last name, first initial and the last two
digits of the graduation year (albertsl27@student.jhs.net is Alberts, L..., class of 2027). Last names are compared
by their letters only, so Saint-Evens, Saint Evens and SaintEvens all match saintevensb28. Only an exact fit is saved.
An address that only nearly fits (a last name that starts the other, such as Lee and Leeson, which may be a two-part
name written differently or a different student) is listed for the coach to confirm; one that fits no athlete, or
more than one, or that fits an athlete another pasted address also fits, is listed rather than guessed. An athlete's
page link goes to the address saved, so a wrong guess would send it to another student."""
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
    """[{"email", "status", "athlete", "candidates"}]; status: "match" (exactly one athlete fits), "check" (one athlete
    nearly fits: confirm it), "none", "many" (more than one athlete fits, or another address fits the same athlete), or
    "odd" (the address doesn't follow the last name + initial + year pattern)."""
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
        exact = bool(fits)
        if not fits:   # a hyphenated or two-part last name written differently: for the coach to confirm
            fits = [a for a in people if letters(a.first_name)[:1] == initial and grad(a) == year
                    and (letters(a.last_name).startswith(last) or last.startswith(letters(a.last_name) or "#"))]
        status = "none" if not fits else "many" if len(fits) > 1 else "match" if exact else "check"
        out.append({"email": e, "status": status, "athlete": fits[0] if len(fits) == 1 else None, "candidates": fits})
    # Two addresses for one athlete: neither is saved or offered, the coach picks
    taken: dict[int, list[dict]] = {}
    for r in out:
        if r["status"] in ("match", "check"):
            taken.setdefault(r["athlete"].id, []).append(r)
    for same in taken.values():
        if len(same) > 1:
            for r in same:
                r["status"], r["clash"] = "many", [x["email"] for x in same if x is not r]
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

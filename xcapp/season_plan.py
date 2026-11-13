"""The season plan, after Daniels' Running Formula: each week has a phase and a share of each group's peak weekly
miles, and the plan suggests that week's two quality sessions for each group, sized within Daniels' limits (limits.py).

Phases (Daniels' four phases, about six weeks each when there's time for all four):
- I Foundation: easy miles, strides and hill strides, strength and drills; no hard quality yet.
- II Early quality: repetition (R) work for speed and economy; the rest easy.
- III Transition quality: interval (I) work, the hardest phase, with some threshold (T).
- IV Final quality: threshold (T) work and races, sharpened with short R work; taper into the championships.

Mileage: each group's peak weekly miles (set here, from its Groups goal at first) times the week's share. In base
seasons the share builds about 5 points every third week and holds in between (Daniels: raise the miles, then hold
them at least three weeks); in season it holds near 100%, with the last two weeks tapering to 85% and 70%. Applying a
week sets each group's weekly miles goal on the Groups page, which the Week page, Dashboard and easy runs use.
"""
from __future__ import annotations

import datetime as dt
import json
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import seasons as SE
from .limits import LIMITS
from .models import SeasonWeek, TrainingGroup

MILE = A.METERS_PER_MILE
PHASES = {
    1: ("Phase I", "Foundation", "Easy miles, strides and hill strides twice a week, strength and drills. No hard quality yet."),
    2: ("Phase II", "Early quality", "Repetition (R) work for speed and economy, full recovery between; everything else easy."),
    3: ("Phase III", "Transition quality", "Interval (I) work, the hardest phase, with threshold (T) as the second session."),
    4: ("Phase IV", "Final quality", "Threshold (T) work and races, kept sharp with short R work; taper into the championships."),
}
PEAKS_KEY = "plan_peaks"


def weeks_of(season: dict) -> list[dt.date]:
    m, out = A.week_monday(season["start"]), []
    while m <= season["end"]:
        out.append(m)
        m += dt.timedelta(days=7)
    return out


def default_plan(season: dict) -> list[tuple[int, int]]:
    """(phase, miles %) for each week of the season: base seasons build, in-season quality and a taper."""
    n = len(weeks_of(season))
    out = []
    if season["kind"] in ("summer", "winter"):
        p1 = math.ceil(0.6 * n)
        for i in range(n):
            out.append((1 if i < p1 else 2, min(90, 70 + 5 * (i // 3))))
    else:
        p2, p3 = round(0.2 * n), round(0.4 * n)
        for i in range(n):
            phase = 2 if i < p2 else 3 if i < p2 + p3 else 4
            pct = 85 if i == n - 2 and n >= 4 else 70 if i == n - 1 and n >= 4 else 95 if phase == 2 else 100
            out.append((phase, pct))
    return out


def weeks(db: Session, season: dict) -> list[dict]:
    """The season's weeks with their saved plan (or None): [{"monday", "row"}]."""
    days = weeks_of(season)
    rows = {w.monday: w for w in db.scalars(select(SeasonWeek).where(SeasonWeek.monday >= days[0], SeasonWeek.monday <= days[-1])).all()} if days else {}
    return [{"monday": d, "row": rows.get(d)} for d in days]


def fill(db: Session, season: dict, replace: bool = False) -> int:
    """Save the default plan for the season's weeks (only weeks not planned yet, unless replace). Returns weeks saved."""
    ensure_peaks(db)
    n = 0
    for w, (phase, pct) in zip(weeks(db, season), default_plan(season)):
        if w["row"] is None:
            db.add(SeasonWeek(monday=w["monday"], phase=phase, miles_pct=pct))
            n += 1
        elif replace:
            w["row"].phase, w["row"].miles_pct = phase, pct
            n += 1
    db.flush()
    return n


def week_of(db: Session, day: dt.date) -> SeasonWeek | None:
    return db.scalar(select(SeasonWeek).where(SeasonWeek.monday == A.week_monday(day)))


def peaks(db: Session) -> dict[str, dict[str, float]]:
    """Each group's peak weekly miles for boys and girls: saved on the plan page, or the group's current goal."""
    try:
        saved = json.loads(A.get_setting(db, PEAKS_KEY, "{}") or "{}")
    except ValueError:
        saved = {}
    out = {}
    for g in db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all():
        mine = saved.get(g.name) or {}
        out[g.name] = {t: float(mine.get(t, g.miles_goal(t)) or 0) for t in ("Boys", "Girls")}
    return out


def save_peaks(db: Session, values: dict[str, dict[str, float]]) -> None:
    A.set_setting(db, PEAKS_KEY, json.dumps(values, separators=(",", ":")))


def ensure_peaks(db: Session) -> None:
    """Keep the peaks as they are now before anything changes the groups' goals (applying a week sets those goals)."""
    if not A.get_setting(db, PEAKS_KEY, ""):
        save_peaks(db, peaks(db))


def planned_miles(peak: float, pct: int) -> float:
    return round(peak * pct / 100 * 2) / 2


def apply_miles(db: Session, sw: SeasonWeek) -> list[str]:
    """Set each group's weekly miles goal to the plan's for that week. Returns 'Gold 42 mi' lines for the change."""
    ensure_peaks(db)
    pk, out = peaks(db), []
    for g in db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all():
        p = pk.get(g.name) or {}
        if not p.get("Boys") and not p.get("Girls"):
            continue
        boys, girls = planned_miles(p.get("Boys", 0), sw.miles_pct), planned_miles(p.get("Girls", 0), sw.miles_pct)
        g.weekly_miles, g.weekly_miles_girls = boys, girls
        out.append(f"{g.name} {boys:g} mi" + (f" (girls {girls:g})" if girls != boys else ""))
    return out


def _limit(zone: str, weekly: float) -> float:
    share, cap = LIMITS[zone]
    return min(weekly * share, cap) if cap else weekly * share


def _pick(limit_mi: float, options: tuple[float, ...], lo: int, hi: int) -> tuple[int, float] | None:
    """(reps, meters each): the longest rep from options that fits at least lo reps within the limit, up to hi reps;
    None when even the shortest doesn't fit lo reps (the session would go over Daniels' limit)."""
    for m in options:
        n = math.floor(limit_mi * MILE / m + 1e-9)
        if n >= lo:
            return min(n, hi), m
    return None


def suggestions(phase: int, weekly: float, run_days: int = 6) -> list[dict]:
    """The week's two quality sessions for a group running `weekly` miles: [{"key", "title", "label", "why", "parts"}].
    Reps are sized to Daniels' limits for the zone (limits.py), within sensible bounds."""
    if not weekly:
        return []
    easy_mi = max(2.0, round(weekly / max(run_days, 1) * 2) / 2)
    easy = {"sets": 1, "reps": 1, "dist": round(easy_mi * MILE), "mi": easy_mi, "zone": "Easy", "rest": "", "note": ""}
    strides = lambda n, hill: {"sets": 1, "reps": n, "dist": 100, "zone": "Repetition", "rest": "jog down" if hill else "walk back",
                               "note": "Hill strides" if hill else "Strides", "strides": True}
    r, i, t = _limit("Repetition", weekly), _limit("Interval", weekly), _limit("Threshold", weekly)
    rep = lambda n, m, zone, rest: ({"sets": 1, "reps": n, "dist": round(m), "mi": 1, "zone": zone, "rest": rest, "note": ""}
                                    if abs(m - MILE) < 1 else {"sets": 1, "reps": n, "dist": m, "zone": zone, "rest": rest, "note": ""})
    dist = lambda m: "1 mi" if abs(m - MILE) < 1 else f"{m:g} m"
    out = []

    def add(key, zone, pick, rest, title, why):
        if pick:
            n, m = pick
            out.append({"key": key, "title": title.format(n=n, d=dist(m)), "why": why, "parts": [rep(n, m, zone, rest)]})

    if phase == 1:
        out += [{"key": "strides", "title": f"Easy {easy_mi:g} mi + 6 strides", "why": "Strides keep speed and economy with no fatigue.",
                 "parts": [easy, strides(6, False)]},
                {"key": "hills", "title": f"Easy {easy_mi:g} mi + 8 hill strides", "why": "Hill strides build strength and a good push-off.",
                 "parts": [easy, strides(8, True)]}]
    elif phase == 2:
        add("r", "Repetition", _pick(r, (200, 150), 6, 16), "full recovery jog", "R: {n} × {d}",
            f"Repetition pace for speed and economy; R running at most {r:.1f} mi (5% of the week).")
        add("r2", "Repetition", _pick(r, (400, 300), 3, 8), "full recovery jog", "R: {n} × {d}",
            "Longer R reps, with a jog as long as the rep between.")
    elif phase == 3:
        add("i", "Interval", _pick(i, (1000, 800, 600), 3, 6), "jog as long as the rep", "I: {n} × {d}",
            f"Interval pace, the phase's key session; I running at most {i:.1f} mi (8% of the week, 10K cap).")
        add("t", "Threshold", _pick(t, (MILE, 1200, 1000), 2, 5), "1 min jog", "T: {n} × {d} cruise",
            f"Threshold reps with 1 min rests; T running at most {t:.1f} mi (10% of the week).")
    else:
        add("t", "Threshold", _pick(t, (MILE, 1200, 1000), 2, 5), "1 min jog", "T: {n} × {d} cruise",
            f"Threshold reps with 1 min rests; T running at most {t:.1f} mi (10% of the week).")
        tempo = min(4.0, math.floor(t * 2) / 2)
        if tempo >= 1.5:
            out.append({"key": "tempo", "title": f"T: {tempo:g} mi tempo", "why": "A steady threshold run (about 20 minutes).",
                        "parts": [{"sets": 1, "reps": 1, "dist": round(tempo * MILE), "mi": tempo, "zone": "Threshold", "rest": "", "note": ""}]})
        add("r", "Repetition", _pick(r, (200, 150), 4, 8), "full recovery jog", "R: {n} × {d} sharpener",
            "Short R reps keep the legs quick in race weeks (or the race is the session).")
    for s in out:
        s["label"] = " + ".join(A.part_label(p) for p in s["parts"])
    return out


def board(db: Session, sw: SeasonWeek | None, run_days: int = 6) -> list[dict]:
    """For a planned week: each group (and team, where boys' and girls' miles differ) with its planned miles and
    suggested sessions: [{"group", "team", "miles", "sessions"}]."""
    if sw is None:
        return []
    out = []
    for name, p in peaks(db).items():
        miles = {t: planned_miles(p.get(t, 0), sw.miles_pct) for t in ("Boys", "Girls")}
        teams = [("", miles["Boys"])] if miles["Boys"] == miles["Girls"] else [(t, m) for t, m in miles.items()]
        for team, mi in teams:
            if mi:
                out.append({"group": name, "team": team, "miles": mi, "sessions": suggestions(sw.phase, mi, run_days)})
    return out


def season_for(day: dt.date) -> dict:
    return SE.period_for(day)

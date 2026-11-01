"""Squads: the team split into balanced groups that compete week by week.

Balancing, boys and girls each on their own: athletes are ranked by VDOT, dealt out like a snake draft
(1-2-3-3-2-1 ...), then the strongest and weakest squads swap runners while that brings their averages closer.
Athletes without a VDOT count as average.

Scoring, set for each competition in its Settings (squads.DEFAULT_SCORING):
- Races (the coach picks which completed races count): each race is scored like a cross country meet among the
  squads (squad runners placed in finishing order, a squad's score the sum of its first five places, lowest wins),
  and the squads earn 40, 25, 10, 5 ... points by place;
- Miles: points for each athlete who runs at least 90% of their weekly miles goal (their VDOT group's goal);
- Days run: points for each athlete who runs at least 6 days that week (the Week page's days run);
- Practice points (off at first): points for each athlete who earns at least 90% of their weekly points target.
Practice points count once the week is over; race points as soon as the race is in. Athletes count for the squad
they are on now, for every week.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import analysis as A
from . import medals as MD
from . import points as PT
from .models import Athlete, RaceResult, Squad, SquadCompetition, SquadMember

NAMES = ("Red", "Gold", "Blue", "Green", "Purple", "Orange", "Silver", "Black")
COLORS = ("#C8102E", "#D4A017", "#1F5FBF", "#2E8B57", "#7B4FA0", "#E07B1F", "#8A8D91", "#333333")
MAX_SQUADS = 8


# ---------- choosing and balancing ----------

def eligible(db: Session, gender: str) -> list[Athlete]:
    q = select(Athlete).where(A.on_team())
    if gender in ("Boys", "Girls"):
        q = q.where(Athlete.gender == gender)
    return db.scalars(q.order_by(Athlete.last_name, Athlete.first_name)).all()


def strengths(db: Session, athletes: list[Athlete], as_of: dt.date) -> dict[int, float]:
    """Each athlete's VDOT as standard deviations above (+) or below (-) their gender's average; 0 when unknown."""
    ab = A.abilities(db, as_of)
    v = {a.id: ab.get(a.id, {}).get("vdot") for a in athletes}
    out = {}
    for g in {a.gender for a in athletes}:
        known = [v[a.id] for a in athletes if a.gender == g and v[a.id]]
        mean = statistics.fmean(known) if known else 0
        sd = statistics.pstdev(known) if len(known) > 1 else 1
        for a in athletes:
            if a.gender == g:
                out[a.id] = (v[a.id] - mean) / (sd or 1) if v[a.id] else 0.0
    return out


def balance(strength: dict[int, float], n: int) -> list[list[int]]:
    """Split athletes into n squads of equal size (within one) and nearly equal average strength."""
    order = sorted(strength, key=lambda i: (-strength[i], i))
    squads: list[list[int]] = [[] for _ in range(n)]
    for k, aid in enumerate(order):
        r, c = divmod(k, n)
        squads[c if r % 2 == 0 else n - 1 - c].append(aid)

    def mean(s):
        return statistics.fmean(strength[i] for i in s) if s else 0.0

    for _ in range(500):
        hi = max(range(n), key=lambda i: mean(squads[i]))
        lo = min(range(n), key=lambda i: mean(squads[i]))
        gap = mean(squads[hi]) - mean(squads[lo])
        best = None
        for x in squads[hi]:
            for y in squads[lo]:
                d = strength[x] - strength[y]
                if d <= 0:
                    continue
                new_gap = abs((sum(strength[i] for i in squads[hi]) - d) / len(squads[hi])
                              - (sum(strength[i] for i in squads[lo]) + d) / len(squads[lo]))
                if new_gap < gap - 1e-9 and (best is None or new_gap < best[0]):
                    best = (new_gap, x, y)
        if best is None:
            break
        _, x, y = best
        squads[hi].remove(x), squads[lo].remove(y)
        squads[hi].append(y), squads[lo].append(x)
    return squads


def create(db: Session, name: str, gender: str, n: int, start: dt.date, end: dt.date | None, today: dt.date) -> SquadCompetition:
    comp = SquadCompetition(name=name, gender=gender, start_date=A.week_monday(start), end_date=end)
    for i in range(n):
        comp.squads.append(Squad(name=NAMES[i], color=COLORS[i], position=i))
    db.add(comp)
    db.flush()
    assign(db, comp, today)
    return comp


def assign(db: Session, comp: SquadCompetition, today: dt.date) -> None:
    """(Re)deal every eligible athlete into balanced squads, replacing any earlier split."""
    for s in comp.squads:
        s.members.clear()
    db.flush()
    athletes = eligible(db, comp.gender)
    for s, ids in zip(comp.squads, split_by_gender(athletes, strengths(db, athletes, today), len(comp.squads))):
        for aid in ids:
            s.members.append(SquadMember(competition_id=comp.id, athlete_id=aid))
    db.flush()


def split_by_gender(athletes: list[Athlete], strength: dict[int, float], n: int) -> list[list[int]]:
    """Balance boys and girls each on their own, so every squad is even in both; a gender's extra runners go to
    the squads that are smallest so far."""
    out: list[list[int]] = [[] for _ in range(n)]
    for g in sorted({a.gender for a in athletes}):
        parts = balance({a.id: strength[a.id] for a in athletes if a.gender == g}, n)
        slots = sorted(range(n), key=lambda i: (len(out[i]), i))
        for i, part in zip(slots, sorted(parts, key=len, reverse=True)):
            out[i] += part
    return out


def move(db: Session, comp: SquadCompetition, athlete_id: int, squad_id: int | None) -> None:
    """Put an athlete on a squad (None takes them out of the competition)."""
    m = db.scalar(select(SquadMember).where(SquadMember.competition_id == comp.id, SquadMember.athlete_id == athlete_id))
    if squad_id is None:
        if m is not None:
            db.delete(m)
        return
    if squad_id not in {s.id for s in comp.squads}:
        raise ValueError("That squad isn't in this competition.")
    if m is None:
        db.add(SquadMember(competition_id=comp.id, squad_id=squad_id, athlete_id=athlete_id))
    else:
        m.squad_id = squad_id


# ---------- weekly scoring ----------

DEFAULT_SCORING = {
    "race_on": True, "race_points": [40, 25, 10, 5], "race_scorers": 5, "excluded_races": [],
    "miles_on": True, "miles_pct": 90, "miles_pts": 1,
    "days_on": True, "days_min": 6, "days_pts": 1,
    "points_on": False, "points_pct": 90, "points_pts": 1,
}
CATEGORIES = (("race", "Races"), ("miles", "Miles"), ("days", "Days run"), ("points", "Practice points"))


def scoring(comp: SquadCompetition) -> dict:
    import json
    try:
        saved = json.loads(comp.scoring) if comp.scoring else {}
    except ValueError:
        saved = {}
    return {**DEFAULT_SCORING, **{k: v for k, v in saved.items() if k in DEFAULT_SCORING}}


def race_key(day: dt.date, meet: str, race: str) -> str:
    return f"{day.isoformat()}|{meet}|{race}"


def score_race(rs: list[RaceResult], squad_of: dict[int, int], scorers: int) -> list[dict]:
    """Cross country scoring among the squads: squad runners placed 1, 2, 3 ... in finishing order, each squad's
    score the sum of its first `scorers` places (lowest wins). Squads short of that many finishers come after the
    full ones (more finishers first, then the lower score)."""
    rs = sorted((r for r in rs if r.athlete_id in squad_of and r.time_s), key=lambda r: r.time_s)
    places: dict[int, list[int]] = defaultdict(list)
    for k, r in enumerate(rs, 1):
        places[squad_of[r.athlete_id]].append(k)
    out = [{"squad_id": sid, "finishers": len(ps), "score": sum(ps[:scorers]), "complete": len(ps) >= scorers}
           for sid, ps in places.items()]
    out.sort(key=lambda x: (not x["complete"], -min(x["finishers"], scorers), x["score"]))
    return out


def _award(order: list[dict], table: list[float], same) -> dict[int, float]:
    """Points by place, ties sharing the points of the places they cover."""
    pts, i = {}, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and same(order[j + 1], order[i]):
            j += 1
        share = statistics.fmean(table[k] if k < len(table) else 0 for k in range(i, j + 1))
        for k in range(i, j + 1):
            pts[order[k]["squad_id"]] = share
        i = j + 1
    return pts


def weeks_of(comp: SquadCompetition, today: dt.date) -> list[dt.date]:
    last = min(today, comp.end_date) if comp.end_date else today
    out, m = [], comp.start_date
    while m <= last:
        out.append(m)
        m += dt.timedelta(days=7)
    return out


def completed_races(db: Session, comp: SquadCompetition, today: dt.date) -> list[dict]:
    """Races squad members ran during the competition, newest first, and whether each counts."""
    sc = scoring(comp)
    ids = list(db.scalars(select(SquadMember.athlete_id).where(SquadMember.competition_id == comp.id)).all())
    end = min(today, comp.end_date) if comp.end_date else today
    rows = db.execute(select(RaceResult.date, RaceResult.meet, RaceResult.race).where(
        RaceResult.athlete_id.in_(ids or [-1]), RaceResult.date >= comp.start_date, RaceResult.date <= end)).all()
    count = defaultdict(int)
    for d, m, r in rows:
        count[(d, m, r or "")] += 1
    return [{"date": d, "meet": m, "race": r, "runners": n, "key": race_key(d, m, r), "counts": race_key(d, m, r) not in sc["excluded_races"]}
            for (d, m, r), n in sorted(count.items(), key=lambda x: (x[0][0], x[0][1], x[0][2]), reverse=True)]


def standings(db: Session, comp: SquadCompetition, today: dt.date) -> dict:
    sc = scoring(comp)
    squads = db.scalars(select(Squad).where(Squad.competition_id == comp.id).order_by(Squad.position)
                        .options(selectinload(Squad.members).selectinload(SquadMember.athlete))).all()
    squad_of = {m.athlete_id: s.id for s in squads for m in s.members}
    athletes = [m.athlete for s in squads for m in s.members]
    mondays = weeks_of(comp, today)
    finished = [m for m in mondays if m + dt.timedelta(days=6) < today]
    saved = MD.ensure_weeks(db, athletes, finished, today)
    live = {}
    current = mondays[-1] if mondays and mondays[-1] not in finished else None
    if current is not None and athletes:
        live = {w["a"].id: w for w in PT.team_week(db, current, athletes=athletes)}
    end = (mondays[-1] + dt.timedelta(days=6)) if mondays else comp.start_date
    results = db.scalars(select(RaceResult).where(RaceResult.athlete_id.in_(list(squad_of) or [-1]),
                                                  RaceResult.date >= comp.start_date, RaceResult.date <= end)).all()
    races = defaultdict(list)
    for r in results:
        races[(r.date, r.meet, r.race or "")].append(r)
    weeks, totals = [], {s.id: defaultdict(float) for s in squads}
    for m in mondays:
        done = m in finished
        rows = {}
        for s in squads:
            hit_m = hit_d = hit_p = 0
            miles = pts = 0.0
            for mem in s.members:
                if done:
                    r = saved.get((mem.athlete_id, m))
                    vals = (r.miles, r.goal_miles, r.points, r.target, r.run_days or 0) if r else (0, None, 0, None, 0)
                else:
                    w = live.get(mem.athlete_id)
                    vals = (w["miles"], w["goal_miles"], w["points"], w["target"], w["run_days"]) if w else (0, None, 0, None, 0)
                mi, goal, p, target, days = vals
                miles, pts = miles + mi, pts + p
                hit_m += bool(goal) and mi >= goal * sc["miles_pct"] / 100 - 1e-9
                hit_p += bool(target) and p >= target * sc["points_pct"] / 100 - 1e-9
                hit_d += days >= sc["days_min"]
            rows[s.id] = {"members": len(s.members), "miles": miles, "points": pts, "hit_miles": hit_m, "hit_days": hit_d,
                          "hit_points": hit_p, "race": 0.0}
        week_races = []
        for (day, meet, race), rs in sorted(races.items()):
            if not (m <= day <= m + dt.timedelta(days=6)):
                continue
            key = race_key(day, meet, race)
            counts = sc["race_on"] and key not in sc["excluded_races"]
            order = score_race(rs, squad_of, int(sc["race_scorers"]))
            award = _award(order, [float(x) for x in sc["race_points"]],
                           lambda a, b: (a["complete"], a["score"], a["complete"] or a["finishers"])
                           == (b["complete"], b["score"], b["complete"] or b["finishers"])) if counts else {}
            for x in order:
                x["points"] = award.get(x["squad_id"], 0)
                if counts:
                    rows[x["squad_id"]]["race"] += x["points"]
            week_races.append({"date": day, "meet": meet, "race": race, "key": key, "counts": counts, "order": order,
                               "runners": sum(x["finishers"] for x in order)})
        for sid, r in rows.items():
            r["parts"] = {"race": r["race"] if sc["race_on"] else None,
                          "miles": r["hit_miles"] * sc["miles_pts"] if sc["miles_on"] else None,
                          "days": r["hit_days"] * sc["days_pts"] if sc["days_on"] else None,
                          "points": r["hit_points"] * sc["points_pts"] if sc["points_on"] else None}
            r["total"] = sum(v for v in r["parts"].values() if v)
            if done or any(x["counts"] for x in week_races):
                t = totals[sid]
                # practice only counts once the week is over; races count as soon as they are run
                t["total"] += r["total"] if done else (r["parts"]["race"] or 0)
                for k, v in r["parts"].items():
                    if v and (done or k == "race"):
                        t[k] += v
                if done:
                    t["miles_run"] += r["miles"]
        weeks.append({"monday": m, "done": done, "rows": rows, "races": week_races})
    table = sorted(squads, key=lambda s: (-totals[s.id]["total"], s.position))
    return {"squads": squads, "weeks": weeks[::-1], "totals": {k: dict(v) for k, v in totals.items()}, "table": table,
            "finished": len(finished), "strength": _squad_strength(db, squads, today), "scoring": sc,
            "categories": [(k, n) for k, n in CATEGORIES if sc[k + "_on"]]}


def _squad_strength(db: Session, squads: list[Squad], today: dt.date) -> dict[int, dict]:
    """Each squad's average VDOT per gender, to show the split is fair."""
    ab = A.abilities(db, today)
    out = {}
    for s in squads:
        by = defaultdict(list)
        for m in s.members:
            v = ab.get(m.athlete_id, {}).get("vdot")
            if v:
                by[m.athlete.gender].append(v)
        out[s.id] = {g: statistics.fmean(vs) for g, vs in by.items()}
    return out


def unassigned(db: Session, comp: SquadCompetition) -> list[Athlete]:
    """Eligible athletes not on any squad (e.g. joined the team after the split)."""
    have = set(db.scalars(select(SquadMember.athlete_id).where(SquadMember.competition_id == comp.id)).all())
    return [a for a in eligible(db, comp.gender) if a.id not in have]

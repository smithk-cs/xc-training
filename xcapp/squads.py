"""Squads: the team split into balanced groups that compete week by week.

Balancing, boys and girls each on their own: athletes are ranked by VDOT, dealt out like a snake draft
(1-2-3-3-2-1 ...), then the strongest and weakest squads swap runners while that brings their averages closer.
Athletes without a VDOT count as average.

Weekly team points (only for finished weeks; the current week shows so far):
- Miles: the squad's average share of each athlete's weekly miles goal (capped at 110% so one big week can't carry
  a squad);
- Practice points: the squad's average share of each athlete's weekly points target (capped at 110%);
- Races, in a week with races: in each race, every squad runner earns one point for each runner from another squad
  they finish ahead of.
In each category the best squad gets as many team points as there are squads, the next one fewer, down to 1 (ties
share). Shares, not totals, so squads compare fairly however fast their runners are. Athletes count for the squad
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
SHARE_CAP = 1.10
CATEGORIES = (("miles", "Miles"), ("points", "Practice points"), ("races", "Races"))


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

def _rank_points(values: dict[int, float | None], n: int, low_wins: bool = False) -> dict[int, float]:
    """n for the best squad down to 1, ties sharing; squads without a value get nothing."""
    have = [(sid, v) for sid, v in values.items() if v is not None]
    have.sort(key=lambda x: x[1] if low_wins else -x[1])
    out, i = {}, 0
    while i < len(have):
        j = i
        while j + 1 < len(have) and abs(have[j + 1][1] - have[i][1]) < 1e-9:
            j += 1
        pts = statistics.fmean(n - k for k in range(i, j + 1))
        for k in range(i, j + 1):
            out[have[k][0]] = pts
        i = j + 1
    return out


def _share(done: float | None, due: float | None) -> float | None:
    return min(done / due, SHARE_CAP) if due and due > 0 and done is not None else None


def race_points(results: list[RaceResult], squad_of: dict[int, int]) -> tuple[dict[int, int], list[dict]]:
    """Per squad, runners from other squads beaten in the same race; and each race's squad scores."""
    races = defaultdict(list)
    for r in results:
        if r.athlete_id in squad_of and r.time_s:
            races[(r.date, r.meet, r.race)].append(r)
    total: dict[int, int] = defaultdict(int)
    detail = []
    for (day, meet, race), rs in sorted(races.items()):
        rs.sort(key=lambda r: r.time_s)
        score: dict[int, int] = defaultdict(int)
        for k, r in enumerate(rs):
            mine = squad_of[r.athlete_id]
            score[mine] += sum(1 for x in rs[k + 1:] if squad_of[x.athlete_id] != mine)
        for sid, v in score.items():
            total[sid] += v
        detail.append({"date": day, "meet": meet, "race": race, "runners": len(rs), "score": dict(score)})
    return dict(total), detail


def weeks_of(comp: SquadCompetition, today: dt.date) -> list[dt.date]:
    last = min(today, comp.end_date) if comp.end_date else today
    out, m = [], comp.start_date
    while m <= last:
        out.append(m)
        m += dt.timedelta(days=7)
    return out


def standings(db: Session, comp: SquadCompetition, today: dt.date) -> dict:
    squads = db.scalars(select(Squad).where(Squad.competition_id == comp.id).order_by(Squad.position)
                        .options(selectinload(Squad.members).selectinload(SquadMember.athlete))).all()
    squad_of = {m.athlete_id: s.id for s in squads for m in s.members}
    athletes = [m.athlete for s in squads for m in s.members]
    n = len(squads)
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
    weeks, totals = [], {s.id: defaultdict(float) for s in squads}
    for m in mondays:
        done = m in finished
        rows = {}
        for s in squads:
            ws = []
            for mem in s.members:
                if done:
                    r = saved.get((mem.athlete_id, m))
                    ws.append((r.miles, r.goal_miles, r.points, r.target, r.score) if r else (0, None, 0, None, None))
                else:
                    w = live.get(mem.athlete_id)
                    ws.append((w["miles"], w["goal_miles"], w["points"], w["target"], w.get("score_pct")) if w else (0, None, 0, None, None))
            miles_sh = [x for x in (_share(mi, g) for mi, g, *_ in ws) if x is not None]
            pts_sh = [x for x in (_share(p, t) for _, _, p, t, _ in ws) if x is not None]
            rows[s.id] = {"members": len(ws), "miles": sum(w[0] for w in ws), "points": sum(w[2] for w in ws),
                          "miles_pct": 100 * statistics.fmean(miles_sh) if miles_sh else None,
                          "points_pct": 100 * statistics.fmean(pts_sh) if pts_sh else None}
        week_results = [r for r in results if m <= r.date <= m + dt.timedelta(days=6)]
        race_total, race_detail = race_points(week_results, squad_of)
        for s in squads:
            rows[s.id]["races"] = race_total.get(s.id, 0) if race_detail else None
        cat_pts = {"miles": _rank_points({sid: r["miles_pct"] for sid, r in rows.items()}, n),
                   "points": _rank_points({sid: r["points_pct"] for sid, r in rows.items()}, n),
                   "races": _rank_points({sid: r["races"] for sid, r in rows.items()}, n) if race_detail else {}}
        for sid, r in rows.items():
            r["team_points"] = {k: cat_pts[k].get(sid) for k in cat_pts}
            r["total"] = sum(v for v in r["team_points"].values() if v)
            if done:
                t = totals[sid]
                t["team_points"] += r["total"]
                t["miles"] += r["miles"]
                t["points"] += r["points"]
                t["races"] += r["races"] or 0
                for k in cat_pts:
                    t[k + "_tp"] += r["team_points"][k] or 0
        weeks.append({"monday": m, "done": done, "rows": rows, "races": race_detail})
    table = sorted(squads, key=lambda s: (-totals[s.id]["team_points"], s.position))
    return {"squads": squads, "weeks": weeks[::-1], "totals": {k: dict(v) for k, v in totals.items()}, "table": table,
            "finished": len(finished), "strength": _squad_strength(db, squads, today)}


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

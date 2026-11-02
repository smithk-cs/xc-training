"""VDOT update report: each runner's VDOT on a chosen day (by default the day before a meet) against the VDOT their
result at that meet gives, on the team's scale, so coaches can see who moved up, stayed, or dropped."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import analysis as A
from .models import Athlete, RaceResult, VdotChange

KINDS = (("up", "Up"), ("same", "No change"), ("down", "Down"), ("new", "No earlier VDOT"))


def meets(db: Session, today: dt.date) -> list[dict]:
    """Meets with results at a VDOT distance (1500 m to 10K), newest first."""
    rows = db.execute(select(RaceResult.date, RaceResult.meet).where(
        RaceResult.date <= today, RaceResult.distance_m >= A.VDOT_MIN_M, RaceResult.distance_m <= A.VDOT_MAX_M,
        RaceResult.time_s > 0).distinct()).all()
    return [{"date": d, "meet": m, "key": f"{d.isoformat()}|{m}"} for d, m in sorted(set(rows), reverse=True)]


def coach_vdot_on(changes: list[VdotChange], current: int | None, day: dt.date) -> int | None:
    """A coach-set VDOT as it stood at the end of `day`, from the athlete's change history."""
    done = [c for c in changes if c.date <= day]
    if done:
        return done[-1].vdot
    later = [c for c in changes if c.date > day]
    return later[0].previous if later else current


def vdot_on(db: Session, athletes: list[Athlete], day: dt.date) -> dict[int, tuple[int | None, str]]:
    """athlete id -> (VDOT that day on the team's scale, where it came from)."""
    lookback = int(A.get_setting(db, "group_lookback_days", "365"))
    targets = A.vdot_targets(db)
    races = A.latest_races(db, day - dt.timedelta(days=lookback), day)
    hist: dict[int, list[VdotChange]] = {}
    for c in db.scalars(select(VdotChange).where(VdotChange.athlete_id.in_([a.id for a in athletes] or [-1]))
                        .order_by(VdotChange.date, VdotChange.at, VdotChange.id)).all():
        hist.setdefault(c.athlete_id, []).append(c)
    out = {}
    for a in athletes:
        coach = coach_vdot_on(hist.get(a.id, []), a.vdot_override, day)
        if coach is not None:
            out[a.id] = (coach, "set by a coach")
        elif a.id in races:
            v, text, _ = races[a.id]
            out[a.id] = (A.team_vdot(v, targets), text)
        else:
            out[a.id] = (None, "")
    return out


def report(db: Session, day: dt.date, meet: str, before: dt.date, show: set[str], today: dt.date) -> dict:
    results = db.scalars(select(RaceResult).where(RaceResult.date == day, RaceResult.meet == meet,
                                                  RaceResult.distance_m >= A.VDOT_MIN_M, RaceResult.distance_m <= A.VDOT_MAX_M,
                                                  RaceResult.time_s > 0).options(selectinload(RaceResult.athlete))).all()
    best: dict[int, tuple[float, RaceResult]] = {}
    for r in results:   # two races the same day: the faster effort
        v = A.vdot(r.distance_m, r.time_s)
        if r.athlete_id not in best or v > best[r.athlete_id][0]:
            best[r.athlete_id] = (v, r)
    athletes = [r.athlete for _, r in best.values()]
    targets = A.vdot_targets(db)
    from . import courses as CS
    from . import weather as WX
    from .models import MeetConditions
    cond = db.scalar(select(MeetConditions).where(MeetConditions.meet == meet, MeetConditions.date == day))
    own = WX.race_weather(db)

    def adjusted(r):
        c = WX.result_course(db, meet, day, r.course or "", cond)
        return CS.adjusted_time(r.time_s, r.distance_m, c, WX.merged(cond, own.get((meet, day, r.race))))
    old = vdot_on(db, athletes, before)
    now = A.abilities(db, today)
    rows, counts = [], {k: 0 for k, _ in KINDS}
    for aid, (v, r) in best.items():
        new = A.team_vdot(v, targets)
        was, source = old.get(aid, (None, ""))
        change = None if was is None else new - was
        kind = "new" if change is None else "up" if change > 0 else "down" if change < 0 else "same"
        counts[kind] += 1
        if kind in show:
            cur = now.get(aid, {})
            rows.append({"a": r.athlete, "r": r, "new": new, "was": was, "source": source, "change": change, "kind": kind,
                         "current": cur.get("vdot"), "group": cur.get("group") or "", "color": cur.get("color") or "",
                         "adjusted": adjusted(r)})
    rows.sort(key=lambda x: (x["change"] is None, -(x["change"] or 0), x["a"].last_name, x["a"].first_name))
    return {"rows": rows, "counts": counts, "total": len(best)}

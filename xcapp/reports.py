"""Reports for coaches: an official roster to print, sorted and filtered as needed, and one athlete's season overview with
charts. Charts are drawn here as SVG so they print exactly as they look."""
from __future__ import annotations

import datetime as dt
import math
import statistics
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import registration as REG
from .models import Activity, Athlete, MissedDay, RaceResult, RestingHeartRate, Result

RED, GOLD, INK, MUTED, GRID = "#A6192E", "#C99A2E", "#1B1B1F", "#6B645C", "#E4DED6"

ROSTER_COLUMNS = {  # key: (heading, numeric?)
    "grade": ("Grade", True), "group": ("Running group", False), "vdot": ("VDOT", True), "sb": ("Season best", True),
    "pr": ("Personal record", True), "races": ("Races", True), "avg_place": ("Avg place", True),
    "best_place": ("Best place", True), "avg_points": ("Avg points", True), "best_points": ("Best points", True),
}
DEFAULT_COLUMNS = ["grade", "group", "vdot", "sb", "pr", "races", "avg_place", "avg_points"]
PDF_HEADS = {"grade": "Grade", "group": "Group", "vdot": "VDOT", "sb": "SB", "pr": "PR", "races": "Races", "avg_place": "Avg pl.",
             "best_place": "Best pl.", "avg_points": "Avg pts", "best_points": "Best pts"}


def seasons_with_races(db: Session) -> list[str]:
    days = {d for (d,) in db.execute(select(RaceResult.date)).all()} | {d for (d,) in db.execute(select(Result.date)).all()}
    names = {A.season_for(d) for d in days} | {A.season_for(A.team_today())}
    return sorted(names, key=lambda s: (int(s.split()[0]), s.endswith("XC")), reverse=True)


def season_dates(season: str) -> tuple[dt.date, dt.date]:
    yr = int(season.split()[0])
    return (dt.date(yr, 7, 1), dt.date(yr, 11, 30)) if season.endswith("XC") else (dt.date(yr - 1, 12, 1), dt.date(yr, 6, 30))


def _times(db: Session, athlete_ids, start: dt.date | None, end: dt.date) -> dict[int, dict[float, float]]:
    out: dict[int, dict[float, float]] = {}

    def add(aid, m, t):
        k = REG._key(m)
        if t and (k not in out.setdefault(aid, {}) or t < out[aid][k]):
            out[aid][k] = t
    q = select(RaceResult).where(RaceResult.athlete_id.in_(athlete_ids), RaceResult.date <= end)
    r = select(Result).where(Result.athlete_id.in_(athlete_ids), Result.date <= end)
    if start:
        q, r = q.where(RaceResult.date >= start), r.where(Result.date >= start)
    for x in db.scalars(q).all():
        add(x.athlete_id, x.distance_m, x.time_s)
    for x in db.scalars(r).all():
        if x.event in A.EVENTS:
            add(x.athlete_id, A.EVENTS[x.event], x.time_s)
    return out


def roster(db: Session, season: str, distance: str, gender: str = "", grade: str = "", group: str = "",
           sort: str = "name", desc: bool = False) -> list[dict]:
    start, end = season_dates(season)
    people = db.scalars(select(Athlete).where(A.on_team())).all()
    people = [a for a in people if (not gender or a.gender == gender) and (not grade or str(a.grade) == grade)]
    ab = A.abilities(db, min(end, A.team_today()))
    if group:
        people = [a for a in people if ab.get(a.id, {}).get("group") == group]
    ids = [a.id for a in people]
    k = REG._key(REG.meters(distance) or 5000)
    season_t, all_t = _times(db, ids, start, end), _times(db, ids, None, end)
    races = {}
    for r in db.scalars(select(RaceResult).where(RaceResult.athlete_id.in_(ids), RaceResult.date >= start, RaceResult.date <= end)).all():
        races.setdefault(r.athlete_id, []).append(r)
    rows = []
    for a in people:
        mine = races.get(a.id, [])
        places = [r.place for r in mine if r.place]
        pts = [r.points for r in mine if r.points is not None]
        info = ab.get(a.id, {})
        rows.append({"athlete": a, "name": a.name, "grade": a.grade, "group": info.get("group") or "", "color": info.get("color") or "",
                     "vdot": info.get("vdot"), "sb": season_t.get(a.id, {}).get(k), "pr": all_t.get(a.id, {}).get(k),
                     "races": len(mine), "avg_place": round(statistics.mean(places), 1) if places else None,
                     "best_place": min(places) if places else None,
                     "avg_points": round(statistics.mean(pts), 1) if pts else None, "best_points": min(pts) if pts else None})
    from .models import TrainingGroup
    ranks = {g.name: g.position for g in db.scalars(select(TrainingGroup)).all()}
    return sort_rows(rows, sort, desc, ranks)


def sort_rows(rows: list[dict], sort: str, desc: bool, ranks: dict[str, int]) -> list[dict]:
    """Sort by a column; athletes with no value in it always go last, by name."""
    by_name = lambda r: (r["athlete"].last_name.lower(), r["athlete"].first_name.lower())
    if sort == "group":
        return sorted(rows, key=lambda r: (ranks.get(r["group"], 99), -(r["vdot"] or 0), by_name(r)), reverse=desc)
    if sort not in ROSTER_COLUMNS or sort == "name":
        return sorted(rows, key=by_name, reverse=desc)
    have = sorted((r for r in rows if r.get(sort) is not None), key=lambda r: (r[sort], by_name(r)), reverse=desc)
    return have + sorted((r for r in rows if r.get(sort) is None), key=by_name)


# ---------- one athlete's season ----------

def equivalent_5k(distance_m: float, time_s: float) -> float:
    return time_s * (5000.0 / distance_m) ** A.RIEGEL_EXPONENT


def overview(db: Session, a: Athlete, season: str) -> dict:
    start, end = season_dates(season)
    today = A.team_today()
    last = min(end, today)
    races = db.scalars(select(RaceResult).where(RaceResult.athlete_id == a.id, RaceResult.date >= start, RaceResult.date <= end)
                       .order_by(RaceResult.date, RaceResult.id)).all()
    # PR: faster than every earlier time at that distance; SB: the best so far this season at that distance.
    best_ever = dict(_times(db, [a.id], None, start - dt.timedelta(days=1)).get(a.id, {}))
    best_season: dict[float, float] = {}
    targets = A.vdot_targets(db)
    rows, prev_eq = [], None
    for r in races:
        k = REG._key(r.distance_m)
        eq = equivalent_5k(r.distance_m, r.time_s)
        pr, sb = r.time_s < best_ever.get(k, math.inf), r.time_s < best_season.get(k, math.inf)
        best_ever[k], best_season[k] = min(best_ever.get(k, math.inf), r.time_s), min(best_season.get(k, math.inf), r.time_s)
        rows.append({"r": r, "label": REG.distance_label(r.distance_m), "pace": r.time_s / (r.distance_m / A.METERS_PER_MILE),
                     "eq": eq, "vdot": A.team_vdot(A.vdot(r.distance_m, r.time_s), targets),
                     "vs_prev": None if prev_eq is None else eq - prev_eq, "pr": pr, "sb": sb})
        prev_eq = eq
    acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id, Activity.local_date >= start, Activity.local_date <= last)).all()
    runs = [x for x in acts if "run" in (x.sport or "running")]
    weeks = A.week_starts(last, max(1, (last - start).days // 7 + 1))
    weeks = [w for w in weeks if w + dt.timedelta(days=6) >= start]
    weekly = [round(sum(x.distance_m for x in runs if w <= x.local_date <= w + dt.timedelta(days=6)) / A.METERS_PER_MILE, 1) for w in weeks]
    while weekly and weekly[0] == 0:  # start the chart at the first week with running
        weeks, weekly = weeks[1:], weekly[1:]
    efforts = [x.rpe for x in runs if x.rpe is not None]
    missed = db.scalars(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date >= start, MissedDay.date <= last)).all()
    rhr = db.scalars(select(RestingHeartRate).where(RestingHeartRate.athlete_id == a.id, RestingHeartRate.date >= start,
                                                    RestingHeartRate.date <= last).order_by(RestingHeartRate.date)).all()
    workouts = [x for x in runs if x.planned_workout_id]
    ab = A.abilities(db, today).get(a.id, {})
    eqs = [x["eq"] for x in rows]
    places = [x["r"].place for x in rows if x["r"].place]
    pts = [x["r"].points for x in rows if x["r"].points is not None]
    active_weeks = [m for m in weekly if m > 0]
    stats = {
        "races": len(rows), "best_eq": min(eqs) if eqs else None,
        "improve": (eqs[0] - eqs[-1]) if len(eqs) > 1 else None,
        "improve_pct": ((eqs[0] - eqs[-1]) / eqs[0] * 100) if len(eqs) > 1 else None,
        "vdot_first": rows[0]["vdot"] if rows else None, "vdot_last": rows[-1]["vdot"] if rows else None,
        "best_place": min(places) if places else None, "avg_place": statistics.mean(places) if places else None,
        "avg_points": statistics.mean(pts) if pts else None, "prs": sum(1 for x in rows if x["pr"]),
        "miles": round(sum(x.distance_m for x in runs) / A.METERS_PER_MILE, 1), "avg_week": round(statistics.mean(active_weeks), 1) if active_weeks else 0,
        "top_week": max(weekly) if weekly else 0, "runs": len(runs), "workouts": len(workouts),
        "avg_effort": round(statistics.mean(efforts), 1) if efforts else None, "missed": len(missed),
        "rhr_avg": round(statistics.mean(r.bpm for r in rhr)) if rhr else None,
    }
    return {"athlete": a, "season": season, "start": start, "end": end, "rows": rows, "weeks": weeks, "weekly": weekly,
            "stats": stats, "ability": ab, "rhr": rhr, "paces": A.training_paces(ab.get("vdot")),
            "charts": {"races": race_chart(rows), "miles": mileage_chart(weeks, weekly), "vdot": vdot_chart(rows),
                       "rhr": rhr_chart(rhr) if len(rhr) >= 5 else ""}}


# ---------- SVG charts ----------

def _svg(w, h, body, label):
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{escape(label)}" '
            f'style="font-family:Inter,system-ui,sans-serif" xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def _nice(lo, hi, n=4):
    if hi <= lo:
        hi = lo + 1
    step = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(step))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= step), default=step)
    start = math.floor(lo / step) * step
    return [start + i * step for i in range(int(math.ceil((hi - start) / step)) + 1)]


def race_chart(rows) -> str:
    if len(rows) < 1:
        return ""
    W, H, L, R, T, B = 640, 240, 56, 16, 16, 44
    ys = [x["eq"] for x in rows]
    lo, hi = min(ys) - 10, max(ys) + 10
    ticks = _nice(lo, hi)
    lo, hi = ticks[0], ticks[-1]
    n = len(rows)
    X = lambda i: L + (W - L - R) * (i / (n - 1) if n > 1 else 0.5)
    Yi = lambda v: T + (H - T - B) * (v - lo) / (hi - lo)   # faster (lower) times nearer the top
    body = []
    for t in ticks:
        y = Yi(t)
        body.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
                    f'<text x="{L-6}" y="{y+4:.1f}" font-size="11" text-anchor="end" fill="{MUTED}">{A.fmt_time(t)[:-2]}</text>')
    pts = " ".join(f"{X(i):.1f},{Yi(v):.1f}" for i, v in enumerate(ys))
    if n > 1:
        body.append(f'<polyline points="{pts}" fill="none" stroke="{RED}" stroke-width="2.5" stroke-linejoin="round"/>')
    for i, x in enumerate(rows):
        cx, cy = X(i), Yi(x["eq"])
        fill = GOLD if x["pr"] else RED
        body.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{fill}" stroke="#fff" stroke-width="1.5">'
                    f'<title>{escape(x["r"].meet)}: {x["label"]} {A.fmt_time(x["r"].time_s)} (5K equivalent {A.fmt_time(x["eq"])})</title></circle>')
        body.append(f'<text x="{cx:.1f}" y="{H-B+16}" font-size="10.5" text-anchor="middle" fill="{MUTED}">{x["r"].date:%b %-d}</text>')
    body.append(f'<text x="{L}" y="{H-6}" font-size="10.5" fill="{MUTED}">Higher is faster · gold = personal record</text>')
    return _svg(W, H, "".join(body), "Race times as 5K equivalents through the season")


def mileage_chart(weeks, weekly) -> str:
    if not weeks or not any(weekly):
        return ""
    W, H, L, R, T, B = 640, 200, 40, 10, 14, 34
    ticks = _nice(0, max(weekly))
    hi = ticks[-1]
    n = len(weeks)
    bw = (W - L - R) / n
    body = []
    for t in ticks:
        y = H - B - (H - T - B) * t / hi
        body.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
                    f'<text x="{L-6}" y="{y+4:.1f}" font-size="11" text-anchor="end" fill="{MUTED}">{t:g}</text>')
    avg = statistics.mean([m for m in weekly if m > 0]) if any(weekly) else 0
    for i, (w, m) in enumerate(zip(weeks, weekly)):
        h = (H - T - B) * m / hi
        x = L + i * bw + bw * 0.15
        body.append(f'<rect x="{x:.1f}" y="{H-B-h:.1f}" width="{bw*0.7:.1f}" height="{h:.1f}" rx="2" fill="{RED if m >= avg else "#D58C98"}">'
                    f'<title>Week of {w:%b %-d}: {m:g} miles</title></rect>')
        if n <= 12 or i % 2 == 0:
            body.append(f'<text x="{x + bw*0.35:.1f}" y="{H-B+14}" font-size="10" text-anchor="middle" fill="{MUTED}">{w:%-m/%-d}</text>')
    ya = H - B - (H - T - B) * avg / hi
    body.append(f'<line x1="{L}" x2="{W-R}" y1="{ya:.1f}" y2="{ya:.1f}" stroke="{GOLD}" stroke-dasharray="5 4" stroke-width="1.5"/>'
                f'<text x="{L+6}" y="{ya-6:.1f}" font-size="11" font-weight="600" fill="#8A6A1F" stroke="#fff" stroke-width="3" '
                f'paint-order="stroke">average {avg:.1f} mi</text>')
    return _svg(W, H, "".join(body), "Miles per week through the season")


def vdot_chart(rows) -> str:
    if len(rows) < 2:
        return ""
    W, H, L, R, T, B = 300, 160, 34, 10, 12, 26
    ys = [x["vdot"] for x in rows]
    ticks = _nice(min(ys) - 1, max(ys) + 1, 3)
    lo, hi = ticks[0], ticks[-1]
    n = len(ys)
    X = lambda i: L + (W - L - R) * i / (n - 1)
    Y = lambda v: H - B - (H - T - B) * (v - lo) / (hi - lo)
    body = [f'<line x1="{L}" x2="{W-R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="{GRID}"/>'
            f'<text x="{L-5}" y="{Y(t)+4:.1f}" font-size="10" text-anchor="end" fill="{MUTED}">{t:g}</text>' for t in ticks]
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(ys))
    area = f"{X(0):.1f},{H-B} " + pts + f" {X(n-1):.1f},{H-B}"
    body.append(f'<polygon points="{area}" fill="{GOLD}" opacity=".18"/><polyline points="{pts}" fill="none" stroke="{GOLD}" stroke-width="2.5"/>')
    body += [f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3.5" fill="{GOLD}"><title>{v}</title></circle>' for i, v in enumerate(ys)]
    return _svg(W, H, "".join(body), "VDOT from each race")


def rhr_chart(rows) -> str:
    W, H, L, R, T, B = 300, 120, 30, 10, 10, 22
    ys = [r.bpm for r in rows]
    ticks = _nice(min(ys) - 2, max(ys) + 2, 3)
    lo, hi = ticks[0], ticks[-1]
    d0, d1 = rows[0].date, rows[-1].date
    span = max(1, (d1 - d0).days)
    X = lambda d: L + (W - L - R) * (d - d0).days / span
    Y = lambda v: H - B - (H - T - B) * (v - lo) / (hi - lo)
    body = [f'<line x1="{L}" x2="{W-R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="{GRID}"/>'
            f'<text x="{L-5}" y="{Y(t)+4:.1f}" font-size="10" text-anchor="end" fill="{MUTED}">{t:g}</text>' for t in ticks]
    body.append(f'<polyline points="{" ".join(f"{X(r.date):.1f},{Y(r.bpm):.1f}" for r in rows)}" fill="none" stroke="{INK}" stroke-width="1.6"/>')
    return _svg(W, H, "".join(body), "Resting heart rate through the season")


def roster_pdf(rows: list[dict], columns: list[str], title: str, subtitle: str, team: str, logo, staff) -> bytes:
    pdf, w = REG.branded_page(team, "Official roster", logo)
    pdf.set_xy(48, 118)
    pdf.set_text_color(*_rgb(INK))
    pdf.set_font("Barlow", size=22)
    pdf.multi_cell(w, 26, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Inter", size=11)
    pdf.set_text_color(*_rgb(MUTED))
    pdf.multi_cell(w, 16, subtitle, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    widths = {"#": 0.05, "name": 0.25}
    if "group" in columns:
        widths["group"] = 0.14
    rest = (1 - sum(widths.values())) / max(1, len([c for c in columns if c not in widths]))
    heads = [("#", "#", "C"), ("name", "Athlete", "L")] + [(c, PDF_HEADS[c], "C") for c in columns]

    def header():
        pdf.set_font("InterSemi", size=7.5)
        pdf.set_fill_color(*_rgb(INK))
        pdf.set_text_color(255, 255, 255)
        for c, label, align in heads:
            pdf.cell(w * widths.get(c, rest), 20, label.upper(), align=align, fill=True)
        pdf.ln(20)
        pdf.set_font("Inter", size=10)
        pdf.set_text_color(*_rgb(INK))
    header()
    pdf.set_draw_color(*_rgb(GRID))
    for i, r in enumerate(rows, 1):
        if pdf.get_y() > pdf.h - 80:
            pdf.add_page()
            header()
        pdf.set_fill_color(*((247, 244, 240) if i % 2 == 0 else (255, 255, 255)))
        vals = [str(i), r["name"]] + [cell_text(r, c) for c in columns]
        for (c, _, align), v in zip(heads, vals):
            pdf.cell(w * widths.get(c, rest), 19, v, align=align, fill=True, border="B")
        pdf.ln(19)
    REG.staff_table(pdf, w, staff)
    return bytes(pdf.output())


def cell_text(r: dict, c: str) -> str:
    v = r.get(c)
    if v is None or v == "":
        return "–"
    if c in ("sb", "pr"):
        return A.fmt_time(v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def vdot_history_chart(changes) -> str:
    """Coach-set VDOT over time as steps; a cleared VDOT breaks the line."""
    pts = [(c.date, c.vdot) for c in changes if c.vdot is not None]
    if len(pts) < 2:
        return ""
    W, H, L, R, T, B = 640, 180, 36, 14, 12, 26
    ys = [v for _, v in pts]
    ticks = _nice(min(ys) - 1, max(ys) + 1, 3)
    lo, hi = ticks[0], ticks[-1]
    d0, d1 = pts[0][0], max(pts[-1][0], A.team_today())
    span = max(1, (d1 - d0).days)
    X = lambda d: L + (W - L - R) * (d - d0).days / span
    Y = lambda v: H - B - (H - T - B) * (v - lo) / (hi - lo)
    body = [f'<line x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{GRID}"/>'
            f'<text x="{L-5}" y="{Y(v)+4:.1f}" font-size="10" text-anchor="end" fill="{MUTED}">{v:g}</text>' for v in ticks]
    path, prev = [], None
    for c in changes:
        if c.vdot is None:
            prev = None
            continue
        if prev is None:
            path.append(f"M{X(c.date):.1f},{Y(c.vdot):.1f}")
        else:
            path.append(f"H{X(c.date):.1f}V{Y(c.vdot):.1f}")
        prev = c.vdot
    if prev is not None:
        path.append(f"H{X(d1):.1f}")
    body.append(f'<path d="{"".join(path)}" fill="none" stroke="{GOLD}" stroke-width="2.5"/>')
    for c in changes:
        if c.vdot is not None:
            up = c.previous is not None and c.vdot > c.previous
            body.append(f'<circle cx="{X(c.date):.1f}" cy="{Y(c.vdot):.1f}" r="4" fill="{"#1F6E43" if up else GOLD}">'
                        f'<title>{c.date:%b %-d}: {c.previous if c.previous is not None else "race"} → {c.vdot}</title></circle>')
    body.append(f'<text x="{L}" y="{H-6}" font-size="10" fill="{MUTED}">{d0:%b %-d}</text>'
                f'<text x="{W-R}" y="{H-6}" font-size="10" text-anchor="end" fill="{MUTED}">{d1:%b %-d}</text>')
    return _svg(W, H, "".join(body), "Coach-set VDOT over time")

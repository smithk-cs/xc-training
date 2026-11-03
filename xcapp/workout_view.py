"""A run laid over its team workout, rep by rep, with target pace and heart-rate bands.

The run: Garmin's second-by-second distance and heart rate, saved compactly with each run (Activity.stream: a point
about every 3 seconds, plus the points on each side of a pause). Older runs without it are drawn from their laps.

Pauses: a gap of more than 10 seconds between points means the watch was stopped (by hand or auto-pause). The time
is shown as a pause and never counts against a rep. Standing still with the watch running shows as very slow pace.

The plan: each workout part expanded into its reps (2 sets x 4 x 400 m = 8 reps), each with its zone. Hard reps
(anything but Easy) are matched in order to the run's efforts:
- laps the watch or athlete marked as reps, when there are any;
- otherwise stretches clearly faster than easy pace (halfway between easy pace and the slowest hard target).
Extra efforts (strides) are skipped and missing reps shown as not found, by lining both up in order (the closest
sizes win). Before the first rep is the warm-up, after the last the cool-down; both are judged as Easy.

Targets:
- pace: the band around the zone's pace at the athlete's VDOT (halfway to the neighbouring zones);
- heart rate: Daniels' share of max heart rate for the zone (easy 65-79%, moderate 80-90%, threshold 88-92%,
  interval 92-100% (Daniels gives 98-100% by the end of 3-5 minute reps; shorter reps average lower); none for repetition, which is too short for heart rate to catch up), or the zone the coach chose
  for that part. Max heart rate: the athlete's own (set on their page), else the highest their watch has recorded.
  The first 30 seconds of each rep aren't judged for heart rate, which takes that long to rise.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import statistics

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analysis as A
from .models import Activity, Athlete

STEP_S = 3
PAUSE_S = 10
SMOOTH_S = 20
HR_LAG_S = 30
MIN_EFFORT_S = 20
MERGE_GAP_S = 8
HR_PCT = {"Easy": (65, 79), "Moderate": (80, 90), "Threshold": (88, 92), "Interval": (92, 100)}
HR_CHOICES = ("Easy", "Moderate", "Threshold", "Interval")
SLOWEST_EASY = 1.15     # easy band: up to 15% slower than easy pace
FASTEST_REP = 0.94      # repetition band: up to 6% faster than repetition pace


# ---------- saving the run's detail ----------

def make_stream(dist_samples: list[tuple[float, float]], hr_samples: list[tuple[float, int]]) -> str:
    """A compact JSON stream: t (seconds since start), d (meters so far), h (heart rate or null), one point about every
    STEP_S seconds and the points on both sides of every pause."""
    pts = sorted((t, d) for t, d in dist_samples if t is not None and d is not None)
    hrs = sorted((t, h) for t, h in hr_samples if t is not None and h)
    if not pts and not hrs:
        return ""
    if not pts:   # heart rate only (treadmill without a footpod): distance unknown
        pts = [(t, None) for t, _ in hrs]
    ht = [t for t, _ in hrs]

    def hr_at(t):
        i = bisect.bisect_left(ht, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(hrs)), key=lambda j: abs(ht[j] - t), default=None)
        return hrs[best][1] if best is not None and abs(ht[best] - t) <= 5 else None

    keep, last = [], None
    for k, (t, d) in enumerate(pts):
        gap_after = k + 1 < len(pts) and pts[k + 1][0] - t > PAUSE_S
        gap_before = k > 0 and t - pts[k - 1][0] > PAUSE_S
        if last is None or t - last >= STEP_S or gap_after or gap_before or k == len(pts) - 1:
            keep.append((t, d))
            last = t
    return json.dumps({"t": [round(t) for t, _ in keep], "d": [None if d is None else round(d, 1) for _, d in keep],
                       "h": [hr_at(t) for t, _ in keep]}, separators=(",", ":"))


def stream_points(act: Activity) -> list[tuple[float, float | None, float | None]]:
    """(seconds, meters, heart rate) from the saved stream, else built from the laps (each lap at its average)."""
    if act.stream:
        try:
            s = json.loads(act.stream)
            pts = list(zip(s["t"], s["d"], s["h"]))
        except (ValueError, KeyError):
            pts = []
        if pts and all(d is None for _, d, _ in pts) and act.laps:   # heart rate only: distance from the laps
            pts = [(t, lap_distance_at(act, t), h) for t, _, h in pts]
        if pts:
            return pts
    out, d = [], 0.0
    for lap in sorted(act.laps, key=lambda l: l.idx):
        t0, moving = lap.start_offset_s or 0, lap.timer_s or lap.elapsed_s or 0
        if moving <= 0:
            continue
        steps = max(1, int(moving // STEP_S))
        for i in range(steps + 1):
            out.append((t0 + moving * i / steps, d + (lap.distance_m or 0) * i / steps, lap.avg_hr))
        d += lap.distance_m or 0
    return out


def lap_distance_at(act: Activity, t: float) -> float | None:
    """Distance so far at t seconds, spread evenly through each lap."""
    d = 0.0
    for lap in sorted(act.laps, key=lambda l: l.idx):
        t0, span = lap.start_offset_s or 0, lap.elapsed_s or lap.timer_s or 0
        if t < t0:
            return d
        if t <= t0 + span and span > 0:
            return d + (lap.distance_m or 0) * (t - t0) / span
        d += lap.distance_m or 0
    return d


# ---------- targets ----------

def pace_band(zone: str, v: float | None) -> tuple[float, float] | None:
    """(fastest, slowest) seconds per mile for a zone at VDOT v."""
    if not v:
        return None
    paces = [(n, A.pace_at(v, pct)) for n, pct in A.PACE_ZONES]   # slowest first
    names = [n for n, _ in paces]
    if zone not in names:
        return None
    i = names.index(zone)
    p = paces[i][1]
    slow = (p + paces[i - 1][1]) / 2 if i > 0 else p * SLOWEST_EASY
    fast = (p + paces[i + 1][1]) / 2 if i + 1 < len(paces) else p * FASTEST_REP
    return fast, slow


def hr_zone_for(part: dict) -> str | None:
    choice = part.get("hr") or ""
    if choice == "none":
        return None
    if choice in HR_CHOICES:
        return choice
    return part.get("zone") if part.get("zone") in HR_PCT else None


def hr_band(zone: str | None, hr_max: float | None) -> tuple[float, float] | None:
    if not zone or not hr_max or zone not in HR_PCT:
        return None
    lo, hi = HR_PCT[zone]
    return hr_max * lo / 100, hr_max * hi / 100


def max_hr(db: Session, a: Athlete, day: dt.date) -> tuple[float | None, str]:
    if a.hr_max:
        return float(a.hr_max), "set on the athlete’s page"
    seen = db.scalar(select(func.max(Activity.max_hr)).where(
        Activity.athlete_id == a.id, Activity.local_date > day - dt.timedelta(days=365), Activity.local_date <= day,
        Activity.max_hr <= 230))
    return (float(seen), "the highest the watch has recorded in a year") if seen else (None, "")


# ---------- the plan ----------

def plan_items(parts: list[dict], daily_miles: float | None) -> list[dict]:
    out = []
    for pi, p in enumerate(A.resolve_fill(parts, daily_miles)):
        n_sets, n_reps = p.get("sets") or 1, p.get("reps") or 1
        unit = (f"{p['mi']:g} mi" if p.get("mi") else f"{p['mins']:g} min" if p.get("mins") else
                f"{(p.get('dist') or 0) / 1000:g}K" if (p.get("dist") or 0) >= 1000 and (p.get("dist") or 0) % 1000 == 0 else
                f"{p.get('dist') or 0:g} m")
        for s in range(n_sets):
            for r in range(n_reps):
                out.append({"part": pi, "set": s + 1, "rep": r + 1, "zone": p.get("zone") or "Easy", "hr_zone": hr_zone_for(p),
                            "dist": None if p.get("mins") else p.get("dist"), "secs": (p.get("mins") or 0) * 60 or None,
                            "label": ("Easy run to finish the day" if p.get("fill") else
                                      f"{unit} {p.get('zone', '').lower()}" + (f" · set {s + 1}, rep {r + 1}" if n_sets > 1 else
                                                                              f" · rep {r + 1}" if n_reps > 1 else "")),
                            "hard": (p.get("zone") or "Easy") != "Easy", "rest": p.get("rest") or ""})
    return out


# ---------- the run ----------

def moving_series(points):
    """Per point: smoothed pace (s/mi) over SMOOTH_S of moving time, and pauses (watch stopped) as (start, end)."""
    pauses = [(points[k - 1][0], points[k][0]) for k in range(1, len(points)) if points[k][0] - points[k - 1][0] > PAUSE_S]
    paces = []
    for k, (t, d, _) in enumerate(points):
        if d is None:
            paces.append(None)
            continue
        j = k
        while j > 0 and t - points[j - 1][0] <= SMOOTH_S and points[j][0] - points[j - 1][0] <= PAUSE_S and points[j - 1][1] is not None:
            j -= 1
        span, dist = t - points[j][0], d - points[j][1]
        paces.append(span / dist * A.METERS_PER_MILE if span >= 6 and dist > 1 else None)
    return paces, pauses


def efforts_from_laps(act: Activity) -> list[dict]:
    reps = sorted((l for l in act.laps if l.is_rep), key=lambda l: l.idx)
    return [{"t0": l.start_offset_s, "t1": l.start_offset_s + (l.elapsed_s or l.timer_s or 0), "dist": l.distance_m,
             "secs": l.timer_s or l.elapsed_s, "from": "lap"} for l in reps]


def efforts_from_stream(points, paces, threshold: float) -> list[dict]:
    """Stretches at least MIN_EFFORT_S long faster than `threshold` (s/mi), joined across short dips but not pauses."""
    runs, cur = [], None
    for k, (t, d, _) in enumerate(points):
        fast = paces[k] is not None and paces[k] <= threshold
        paused = k > 0 and t - points[k - 1][0] > PAUSE_S
        if cur and (paused or (not fast and t - cur["last"] > MERGE_GAP_S)):
            runs.append(cur)
            cur = None
        if fast:
            if cur is None:
                cur = {"k0": k, "t0": t, "d0": d}
            cur.update(k1=k, t1=t, d1=d, last=t)
    if cur:
        runs.append(cur)
    def raw(k):   # pace from point k-1 to point k (None across a pause)
        if k <= 0 or k >= len(points) or points[k][1] is None or points[k - 1][1] is None:
            return None
        span, dist = points[k][0] - points[k - 1][0], points[k][1] - points[k - 1][1]
        return span / dist * A.METERS_PER_MILE if 0 < span <= PAUSE_S and dist > 0.5 else None

    out = []
    for r in runs:
        # The smoothed pace lags, so sharpen the edges with point-to-point pace: start where running got fast,
        # end where it last was fast.
        k0, k1 = r["k0"], r["k1"]
        while k0 > 0 and (raw(k0) or 1e9) <= threshold and r["t0"] - points[k0 - 1][0] <= SMOOTH_S:
            k0 -= 1
        while k1 > k0 and (raw(k1) or 1e9) > threshold:
            k1 -= 1
        t0, d0, t1, d1 = points[k0][0], points[k0][1], points[k1][0], points[k1][1]
        secs = t1 - t0
        if secs >= MIN_EFFORT_S and d1 is not None and d0 is not None:
            out.append({"t0": t0, "t1": t1, "dist": d1 - d0, "secs": secs, "from": "pace"})
    return out


def match(hard: list[dict], efforts: list[dict], v: float | None) -> list[dict | None]:
    """Line the assigned hard reps up with the run's efforts in order, skipping extra efforts and missing reps, so
    the sizes agree best (dynamic programming over both lists)."""
    def size(item):
        if item["secs"]:
            return item["secs"]
        if item["dist"] and v:
            return item["dist"] / A.METERS_PER_MILE * A.pace_at(v, A.WORKOUT_ZONES.get(item["zone"], A.EASY_PCT))
        return None

    def cost(item, e):
        want = size(item)
        return min(abs(e["secs"] - want) / want, 2.0) if want else 0.5

    n, m = len(hard), len(efforts)
    SKIP_EFFORT, MISS_REP = 0.6, 1.2
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            opts = []
            if i and j:
                opts.append(best[i - 1][j - 1] + cost(hard[i - 1], efforts[j - 1]))
            if j:
                opts.append(best[i][j - 1] + SKIP_EFFORT)
            if i:
                opts.append(best[i - 1][j] + MISS_REP)
            best[i][j] = min(opts)
    out: list[dict | None] = [None] * n
    i, j = n, m
    while i and j:
        if abs(best[i][j] - (best[i - 1][j - 1] + cost(hard[i - 1], efforts[j - 1]))) < 1e-9:
            out[i - 1] = efforts[j - 1]
            i, j = i - 1, j - 1
        elif abs(best[i][j] - (best[i][j - 1] + SKIP_EFFORT)) < 1e-9:
            j -= 1
        else:
            i -= 1
    return out


def judge(points, paces, t0: float, t1: float, pband, hband, hr_lag: float) -> dict:
    """Time in each band between t0 and t1, average pace and heart rate."""
    sel = [k for k, (t, _, _) in enumerate(points) if t0 <= t <= t1]
    if not sel:
        return {}
    moving = [(k, min(points[k][0] - points[k - 1][0], PAUSE_S)) for k in sel if k > 0 and points[k][0] - points[k - 1][0] <= PAUSE_S]
    d0 = next((points[k][1] for k in sel if points[k][1] is not None), None)
    d1 = next((points[k][1] for k in reversed(sel) if points[k][1] is not None), None)
    secs = sum(w for _, w in moving)
    dist = (d1 - d0) if d0 is not None and d1 is not None else None
    out = {"secs": secs, "dist": dist, "pace": secs / dist * A.METERS_PER_MILE if dist and dist > 20 else None}
    hrs = [(points[k][2], w) for k, w in moving if points[k][2] and points[k][0] >= t0 + hr_lag]
    out["hr"] = sum(h * w for h, w in hrs) / sum(w for _, w in hrs) if hrs else None
    if pband:
        timed = [(paces[k], w) for k, w in moving if paces[k] is not None]
        tot = sum(w for _, w in timed)
        out["pace_in"] = 100 * sum(w for p, w in timed if pband[0] <= p <= pband[1]) / tot if tot else None
        out["pace_fast"] = 100 * sum(w for p, w in timed if p < pband[0]) / tot if tot else None
    if hband and hrs:
        tot = sum(w for _, w in hrs)
        out["hr_in"] = 100 * sum(w for h, w in hrs if hband[0] <= h <= hband[1]) / tot
        out["hr_high"] = 100 * sum(w for h, w in hrs if h > hband[1]) / tot
    return out


def verdict(value, band, fast_word="fast", slow_word="slow") -> str:
    if value is None or not band:
        return ""
    if value < band[0]:
        return fast_word
    if value > band[1]:
        return slow_word
    return "on target"


OLD_BAND = 0.03   # older workouts: within 3% of their target pace


def old_style_items(db: Session, a: Athlete, pw) -> list[dict]:
    """Reps for a workout from before parts: its rep distance at its target pace; how many from the title ('5 x 1K')."""
    import re
    zone = A.OLD_TYPE_ZONE.get(A.pace_type(pw.pace_factor), "Interval")
    m = re.match(r"\s*(\d+)\s*[x×]", pw.title or "")
    dist = pw.rep_distance_m or None
    unit = (f"{dist / 1000:g}K" if dist and dist >= 1000 and dist % 1000 == 0 else f"{dist:g} m") if dist else "Rep"
    target = A.target_pace(db, a.id, pw)
    band = (target * (1 - OLD_BAND), target * (1 + OLD_BAND)) if target else None
    base = {"part": 0, "set": 1, "zone": zone, "hr_zone": zone if zone in HR_PCT else None, "dist": dist, "secs": None,
            "hard": True, "rest": "", "pace_band": band, "unit": unit}
    if m:
        return [dict(base, rep=i + 1, label=f"{unit} · rep {i + 1}") for i in range(int(m.group(1)))]
    return [dict(base, rep=1, label=unit, open_count=True)]


def build(db: Session, act: Activity) -> dict:
    """Everything the workout chart and table need, as plain data."""
    a = act.athlete
    pw = act.planned
    v = act.zone_vdot or A.abilities(db, act.local_date).get(a.id, {}).get("vdot")
    hmax, hmax_from = max_hr(db, a, act.local_date)
    points = stream_points(act)
    paces, pauses = moving_series(points) if points else ([], [])
    group = A.abilities(db, act.local_date).get(a.id, {}).get("group") or ""
    from .models import TrainingGroup
    g = db.scalar(select(TrainingGroup).where(TrainingGroup.name == group)) if group else None
    goal = A.athlete_miles_goal(g, a.gender, a.experience)
    from .points import settings_for
    target_days = settings_for(db)[0]
    items = plan_items(pw.part_list, goal / target_days if goal else None) if pw is not None and pw.part_list else []
    old_style = pw is not None and not pw.part_list    # "5 x 1K @ 5K pace": one rep distance at a race-based pace
    if old_style:
        items = old_style_items(db, a, pw)
    hard = [x for x in items if x["hard"]]
    efforts = efforts_from_laps(act)
    source = "laps"
    if len(efforts) < max(1, len(hard) // 2) and points and v:
        hard_paces = [pace_band(x["zone"], v)[1] for x in hard if pace_band(x["zone"], v)]
        if hard_paces:
            threshold = (A.pace_at(v, A.EASY_PCT) + max(hard_paces)) / 2
            efforts, source = efforts_from_stream(points, paces, threshold), "pace"
    if old_style and hard and hard[0].get("open_count"):   # the title doesn't say how many: one per effort of about that size
        want = hard[0]["dist"]
        hard = [dict(hard[0], rep=i + 1, label=f"{hard[0]['unit']} · rep {i + 1}") for i, e in enumerate(
            e for e in efforts if not want or not e.get("dist") or abs(e["dist"] - want) <= 0.4 * want)]
    matched = match(hard, efforts, v) if hard else []
    reps = []
    for item, e in zip(hard, matched):
        pb, hb = item.get("pace_band") or pace_band(item["zone"], v), hr_band(item["hr_zone"], hmax)
        row = {**item, "pace_band": pb, "hr_band": hb, "found": e is not None}
        if e:
            row.update(t0=e["t0"], t1=e["t1"], **judge(points, paces, e["t0"], e["t1"], pb, hb, HR_LAG_S))
            if e["from"] == "lap" and e.get("secs") and e.get("dist"):   # the lap's own numbers are the exact ones
                row.update(secs=e["secs"], dist=e["dist"], pace=e["secs"] / e["dist"] * A.METERS_PER_MILE)
            row["pace_verdict"] = verdict(row.get("pace"), pb)
            row["hr_verdict"] = verdict(row.get("hr"), hb, "low", "high")
        reps.append(row)
    found = [r for r in reps if r["found"]]
    easy = []
    if points:
        start, end = points[0][0], points[-1][0]
        eb, ehb = pace_band("Easy", v), hr_band("Easy", hmax)
        if found:
            if found[0]["t0"] - start > 60:
                easy.append({"label": "Warm-up", "t0": start, "t1": found[0]["t0"], "pace_band": eb, "hr_band": ehb})
            if end - found[-1]["t1"] > 60:
                easy.append({"label": "Cool-down", "t0": found[-1]["t1"], "t1": end, "pace_band": eb, "hr_band": ehb})
        elif not hard:
            easy.append({"label": "Easy running", "t0": start, "t1": end, "pace_band": eb, "hr_band": ehb})
        for x in easy:
            x.update(judge(points, paces, x["t0"], x["t1"], eb, ehb, 0))
            x["pace_verdict"], x["hr_verdict"] = verdict(x.get("pace"), eb), verdict(x.get("hr"), ehb, "low", "high")
    keys = ("secs", "dist", "pace", "hr", "pace_in", "pace_fast", "hr_in", "hr_high", "pace_verdict", "hr_verdict", "rest")
    for row in reps + easy:   # every row has every field, empty when the run doesn't say
        for k in keys:
            row.setdefault(k, None if k not in ("pace_verdict", "hr_verdict", "rest") else "")
    return {
        "has_stream": bool(act.stream), "source": source, "vdot": v, "hr_max": hmax, "hr_max_from": hmax_from,
        "reps": reps, "easy": easy, "pauses": [{"t0": a0, "t1": a1, "secs": a1 - a0} for a0, a1 in pauses],
        "paused_secs": sum(a1 - a0 for a0, a1 in pauses), "items": items, "workout": pw,
        "chart": {"t": [p[0] for p in points], "pace": [None if x is None else round(x, 1) for x in paces],
                  "hr": [p[2] for p in points],
                  "reps": [{"t0": r["t0"], "t1": r["t1"], "label": r["label"], "pace": r["pace_band"], "hr": r["hr_band"]}
                           for r in found] + [{"t0": x["t0"], "t1": x["t1"], "label": x["label"], "pace": x["pace_band"],
                                              "hr": x["hr_band"], "easy": True} for x in easy],
                  "pauses": [[a0, a1] for a0, a1 in pauses]},
    }

"""The dashboard: how each athlete is doing this week on mileage, points, days running and the total score, against
what the week asks for so far (the same numbers as the Week page; see points.team_week), sorted into three bands."""
from __future__ import annotations

TABS = (("miles", "Mileage"), ("points", "Points"), ("days", "Days running"), ("score", "Total score"))
BANDS = (("top", "90% or more", 90), ("mid", "80–89%", 80), ("low", "Below 80%", None))


def _measure(tab: str, r: dict) -> tuple[float | None, str, str]:
    """(percent, what it's made of, why there's no percent) for one athlete's week on one tab."""
    if tab == "miles":
        if not r.get("goal_miles"):
            return None, "", "No weekly miles goal (set on the Groups page)"
        return r.get("miles_pct"), f"{r['miles']:.1f} of {r.get('miles_due') or 0:.1f} mi due so far · {r['goal_miles']:g} for the week", "Nothing due yet"
    if tab == "points":
        if not r.get("target"):
            return None, "", r.get("no_score") or "No points target yet"
        return r.get("points_pct"), f"{r['points']:.0f} of {r.get('points_due') or 0:.0f} pts due so far · {r['target']:.0f} for the week", "Nothing due yet"
    if tab == "days":
        return r.get("days_pct"), f"{r['run_days']} of {r.get('days_due', 0)} run days so far · {r['target_days']} for the week", "Nothing due yet"
    if not r.get("target") and not r.get("goal_miles"):
        return None, "", r.get("no_score") or "No points target or miles goal yet"
    sh = r.get("shares") or {"points": 0.4, "miles": 0.4, "days": 0.2}
    parts = []
    if r.get("target") and r.get("points_pct") is not None:
        parts.append(f"points {min(r['points_pct'], 100):.0f}% × {100 * sh['points']:.0f}%")
    if r.get("goal_miles") and r.get("miles_pct") is not None:
        parts.append(f"miles {min(r['miles_pct'], 100):.0f}% × {100 * sh['miles']:.0f}%")
    if r.get("days_pct") is not None:
        parts.append(f"days run {min(r['days_pct'], 100):.0f}% × {100 * sh['days']:.0f}%")
    return r.get("score_pct"), " + ".join(parts).capitalize() + " (each counted up to 100%)" if parts else "", "Nothing due yet"


def board(rows: list[dict]) -> dict:
    """For each tab: the athletes in each band (highest first) and those with nothing to measure yet."""
    out = {}
    for tab, _ in TABS:
        bands = {key: [] for key, _, _ in BANDS}
        none = []
        for r in rows:
            pct, detail, why_not = _measure(tab, r)
            entry = {"a": r["a"], "group": r.get("group_name") or "", "detail": detail}
            if pct is None:
                none.append({**entry, "detail": why_not})
                continue
            shown = round(pct)   # banded by the number shown, so 89.6% (shown 90%) is in the top band
            entry["pct"] = shown
            key = next(k for k, _, low in BANDS if low is None or shown >= low)
            bands[key].append(entry)
        for v in bands.values():
            v.sort(key=lambda e: (-e["pct"], e["a"].last_name, e["a"].first_name))
        none.sort(key=lambda e: (e["a"].last_name, e["a"].first_name))
        out[tab] = {"bands": bands, "none": none, "count": len(rows)}
    return out

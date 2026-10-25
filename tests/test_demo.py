"""End to end through the real pipeline: sample team -> runs with laps and heart rate -> ingest -> the
dashboard's numbers. Every number must match the sample files the published prototype uses."""
import csv
import datetime as dt
import pathlib

from xcapp import datasets as DS
from xcapp.demo import load_demo
from xcapp.models import Activity

DEMO = pathlib.Path(__file__).resolve().parents[1] / "xcapp" / "demo"


def read(name):
    with open(DEMO / name, newline="") as f:
        return list(csv.DictReader(f))


def test_demo_round_trip(session):
    out = load_demo(session)
    assert out["athletes"] == 62 and out["workouts"] == 4
    snap = DS.snapshot(session, dt.date(2026, 10, 25))

    # weekly mileage, load ratio and injured weeks
    want = {(r["athlete_id"], r["week_start"]): r for r in read("weekly_load.csv")}
    got = {(r["athlete_id"], r["week_start"]): r for r in snap["weekly_load"]}
    assert set(want) == set(got)
    # The sample file stores miles rounded to 0.1 but computed its ratios from unrounded miles, so the exact
    # expectation is the ratio of the file's own (rounded) miles; the file's ratio must agree within rounding.
    by_athlete = {}
    for (aid, ws), w in sorted(want.items()):
        by_athlete.setdefault(aid, []).append(float(w["miles"]))
    expected = {}
    for aid, ms in by_athlete.items():
        for i, wk in enumerate(sorted(ws for (a, ws) in want if a == aid)):
            expected[(aid, wk)] = None if i < 4 else ms[i] / (sum(ms[i - 4:i]) / 4)
    worst_miles = worst_exact = worst_file = 0.0
    for k, w in want.items():
        g = got[k]
        worst_miles = max(worst_miles, abs(float(w["miles"]) - g["miles"]))
        assert int(w["injured"]) == g["injured"], k
        if expected[k] is None:
            assert g["acwr"] == "", k  # the app has no history before June 1
        else:
            worst_exact = max(worst_exact, abs(round(expected[k], 2) - g["acwr"]))
            worst_file = max(worst_file, abs(float(w["acwr"]) - g["acwr"]))
    assert worst_miles <= 0.051, worst_miles
    assert worst_exact <= 0.0051, worst_exact
    assert worst_file <= 0.03, worst_file

    # every rep: detected, numbered, paced, heart rate and recovery recovered from second-by-second data
    skipped = set(out["skipped_workouts"])
    weekly = {(r["athlete_id"], r["week_start"]): r for r in read("weekly_load.csv")}
    for aid, day in skipped:  # only workouts listed inside weeks the sample marks as injury weeks
        d = dt.date.fromisoformat(day)
        monday = (d - dt.timedelta(days=d.weekday())).isoformat()
        assert weekly[(aid, monday)]["injured"] == "1", (aid, day)
    want = {(r["athlete_id"], r["workout_id"], r["rep"]): r for r in read("segments.csv") if (r["athlete_id"], r["date"]) not in skipped}
    got = {(r["athlete_id"], r["workout_id"], str(r["rep"])): r for r in snap["segments"]}
    assert set(want) == set(got), (len(want), len(got))
    for k, w in want.items():
        g = got[k]
        assert abs(float(w["pace_sec_mi"]) - g["pace_sec_mi"]) <= 0.11, k
        assert int(w["avg_hr"]) == g["avg_hr"] and int(w["max_hr"]) == g["max_hr"], k
        assert (w["hr_drop_60s"] or "") == ("" if g["hr_drop_60s"] == "" else str(g["hr_drop_60s"])), k

    # headline numbers and the watch list (now including the sample pain report)
    k = snap["kpis"]
    assert (k["median_gain"], k["goals_met"], k["goal_total"], k["injured_now"], k["over_now"], k["over_prev"]) == (35.1, 14, 62, 2, 1, 3)
    assert [w["athlete"] for w in snap["watchlist"]] == [
        "Lucia Sorensen", "Adrian Iverson", "Ruby Haddad", "Silas Mercado", "Sofia Brennan", "Willa Moreno"]
    assert snap["watchlist"][-1]["reasons"] == "Pain 5/10: Lower leg"

    # both rep-detection paths were used: structured watch workouts and manual laps
    acts = session.query(Activity).filter(Activity.planned_workout_id.is_not(None)).all()
    kinds = {a.planned.title: {l.intensity for l in a.laps} for a in acts}
    assert "interval" in kinds["6 x 1K @ 5K pace"] and kinds["5 x 1K @ 5K pace"] == {"active"}

"""The Python calculations against the published sample dashboard's numbers."""
import csv
import datetime as dt
import pathlib

import pytest

from xcapp import analysis as A
from xcapp.models import Activity, Athlete, Injury

DEMO = pathlib.Path(__file__).resolve().parents[1] / "xcapp" / "demo"


def read(name):
    with open(DEMO / name, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def sample():
    d = {n: read(n + ".csv") for n in ("roster", "benchmarks", "goals", "weekly_load", "injuries", "segments")}
    d["summaries"] = A.workout_summaries(d["segments"])
    return d


def test_risk_bands_match_published_numbers(sample):
    bands = {b["band"]: (b["weeks"], b["injuries"]) for b in A.risk_bands(sample["weekly_load"], sample["injuries"])}
    assert bands == {"Under 0.8": (33, 2), "0.8 to 1.3": (1182, 16), "Over 1.3": (53, 6)}


def test_workout_summary_and_kpis(sample):
    s = next(x for x in sample["summaries"] if x["athlete_id"] == "A59" and x["workout_id"] == "W4")
    assert s["recovery"] == 18.5 and s["avg_hr"] == 202 and s["flag"] == "Slow recovery"
    watch = A.watchlist(sample["weekly_load"], sample["injuries"], sample["summaries"], sample["roster"])
    assert [w["athlete"] for w in watch] == ["Lucia Sorensen", "Adrian Iverson", "Ruby Haddad", "Silas Mercado", "Sofia Brennan"]
    assert watch[0]["reasons"] == "Load ratio 1.60; Slow recovery in latest workout"
    prog, seasons = A.progress(sample["benchmarks"], sample["goals"], sample["roster"])
    assert seasons == {"last": "2025 XC", "track": "2026 Track", "current": "2026 XC"}
    k = A.kpis(sample["injuries"], sample["weekly_load"], prog, watch, sample["roster"])
    assert (k["median_gain"], k["returners"], k["goals_met"], k["goal_total"]) == (35.1, 45, 14, 62)
    assert (k["injured_now"], k["over_now"], k["over_prev"], k["roster"]) == (2, 1, 3, 62)
    rowan = next(p for p in prog if p["athlete"] == "Rowan Valdez" and p["event"] == "5K")
    assert rowan["change"] == -3.0


def test_pain_report_flags(sample):
    roster = sample["roster"]
    ids = {r["athlete"]: r["athlete_id"] for r in roster}
    pain = [{"athlete_id": ids["Willa Moreno"], "athlete": "Willa Moreno", "date": "2026-10-21", "body_area": "Lower leg", "severity": "5"},
            {"athlete_id": ids["Camila Takahashi"], "athlete": "Camila Takahashi", "date": "2026-10-22", "body_area": "Foot", "severity": "3"},
            {"athlete_id": ids["Adrian Iverson"], "athlete": "Adrian Iverson", "date": "2026-10-10", "body_area": "Knee", "severity": "8"}]
    watch = A.watchlist(sample["weekly_load"], sample["injuries"], sample["summaries"], roster, pain)
    names = {w["athlete"]: w["reasons"] for w in watch}
    assert names["Willa Moreno"] == "Pain 5/10: Lower leg"
    assert "Camila Takahashi" not in names            # below the flag
    assert "Pain" not in names["Adrian Iverson"]      # older than this week


def test_weekly_rows_from_activities(session):
    a = Athlete(first_name="Test", last_name="Runner", gender="Boys", grade=10, group="JV", link_token="t1")
    session.add(a)
    session.flush()
    as_of = dt.date(2026, 10, 25)
    miles = [10, 10, 10, 10, 13, 20]
    for k, m in enumerate(miles):
        day = as_of - dt.timedelta(days=7 * (len(miles) - 1 - k) + 6)  # first day of each week
        session.add(Activity(athlete_id=a.id, source="upload", external_id=f"x{k}", start_utc=dt.datetime(2026, 1, 1) + dt.timedelta(days=k),
                             local_date=day, distance_m=m * A.METERS_PER_MILE, sport="running"))
    session.add(Activity(athlete_id=a.id, source="upload", external_id="bike", start_utc=dt.datetime(2026, 2, 1),
                         local_date=as_of, distance_m=50 * A.METERS_PER_MILE, sport="cycling"))
    session.add(Injury(athlete_id=a.id, onset_date=as_of - dt.timedelta(days=3), injury="Calf strain", body_area="Lower leg"))
    session.flush()
    rows = A.weekly_load_rows(session, as_of, n_weeks=6)
    assert [r["miles"] for r in rows] == [10.0, 10.0, 10.0, 10.0, 13.0, 20.0]
    assert [r["acwr"] for r in rows][-2:] == [1.3, round(20 / 10.75, 2)]
    assert rows[0]["acwr"] == ""                       # no history before the first week
    assert [r["injured"] for r in rows] == [0, 0, 0, 0, 0, 1]
    newbie = Athlete(first_name="New", last_name="Kid", gender="Girls", grade=9, group="JV", link_token="t2")
    session.add(newbie)
    session.flush()
    for k, m in enumerate([5, 20]):
        session.add(Activity(athlete_id=newbie.id, source="upload", external_id=f"n{k}", start_utc=dt.datetime(2026, 3, 1) + dt.timedelta(days=k),
                             local_date=as_of - dt.timedelta(days=13 - 7 * k), distance_m=m * A.METERS_PER_MILE, sport="running"))
    session.flush()
    new_rows = [r for r in A.weekly_load_rows(session, as_of, n_weeks=6) if r["athlete"] == "New Kid"]
    assert [r["miles"] for r in new_rows][-2:] == [5.0, 20.0] and all(r["acwr"] == "" for r in new_rows)
    inj = A.injury_rows(session, as_of)
    assert inj[0]["status"] == "Active" and inj[0]["days_missed"] == 3

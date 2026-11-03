import datetime as dt
import json
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import workout_view as WV
from xcapp.models import Activity, Athlete, PlannedWorkout

DAY = dt.date(2026, 10, 20)
V = 50


def synthetic_run():
    """Warm-up 10 min easy, two 25 s strides, 4 x 400 m at interval pace with the watch paused 90 s between reps,
    then 8 min easy. Returns (dist_samples, hr_samples) each second, with gaps where the watch was paused."""
    easy = A.pace_at(V, A.EASY_PCT) / A.METERS_PER_MILE          # seconds per meter
    fast = A.pace_at(V, A.INTERVAL_PCT) / A.METERS_PER_MILE
    t, d, dist, hrs = 0.0, 0.0, [], []

    def run(secs, sec_per_m, hr0, hr1):
        nonlocal t, d
        for k in range(int(secs)):
            dist.append((t, d))
            hrs.append((t, int(hr0 + (hr1 - hr0) * min(k / 40, 1))))
            t += 1
            d += 1 / sec_per_m
    run(600, easy, 120, 140)
    for _ in range(2):
        run(25, fast * 0.9, 145, 150)
        run(60, easy, 145, 140)
    for rep in range(4):
        run(400 * fast, fast, 150, 190)
        if rep < 3:
            t += 90                                                  # watch paused
    run(480, easy, 160, 135)
    return dist, hrs


def make(db):
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-wv",
                vdot_override=V, hr_max=200)
    db.add(a)
    db.flush()
    pw = PlannedWorkout(date=DAY, title="4 x 400", rep_distance_m=400, groups="All",
                        parts=json.dumps([{"sets": 1, "reps": 10, "mins": 1, "zone": "Easy", "rest": ""},
                                          {"sets": 1, "reps": 4, "dist": 400, "zone": "Interval", "rest": "90 s rest"}]))
    db.add(pw)
    db.flush()
    dist, hrs = synthetic_run()
    act = Activity(athlete_id=a.id, source="garmin_file", external_id="wv1", start_utc=dt.datetime(2026, 10, 20, 22),
                   local_date=DAY, duration_s=dist[-1][0], distance_m=dist[-1][1], sport="running", planned_workout_id=pw.id,
                   stream=WV.make_stream(dist, hrs), zone_vdot=V)
    db.add(act)
    db.flush()
    return a, act


def test_stream_is_compact_and_keeps_pauses():
    dist, hrs = synthetic_run()
    s = json.loads(WV.make_stream(dist, hrs))
    assert len(s["t"]) < len(dist) / 2.5 and len(s["t"]) == len(s["d"]) == len(s["h"])
    gaps = [b - a for a, b in zip(s["t"], s["t"][1:]) if b - a > WV.PAUSE_S]
    assert len(gaps) == 3 and all(abs(g - 91) <= 1 for g in gaps)


def test_bands():
    fast, slow = WV.pace_band("Interval", V)
    assert fast < A.pace_at(V, A.INTERVAL_PCT) < slow
    assert WV.hr_band("Threshold", 200) == (176, 184) and WV.hr_band(None, 200) is None
    assert WV.hr_zone_for({"zone": "Repetition"}) is None and WV.hr_zone_for({"zone": "Interval", "hr": "Threshold"}) == "Threshold"
    assert WV.hr_zone_for({"zone": "Interval", "hr": "none"}) is None


def test_reps_found_across_pauses_strides_skipped(session):
    a, act = make(session)
    wv = WV.build(session, act)
    reps = wv["reps"]
    assert len(reps) == 4 and all(r["found"] for r in reps) and wv["source"] == "pace"
    assert len(wv["pauses"]) == 3 and 265 <= wv["paused_secs"] <= 280
    for r in reps:
        assert r["pace_verdict"] == "on target" and r["pace_in"] > 70
        assert abs(r["dist"] - 400) < 40 and r["hr_band"] == (184, 200)
    first = reps[0]
    assert first["t0"] > 600 + 2 * 85 - 5                                   # after the strides, not on them
    assert [x["label"] for x in wv["easy"]] == ["Warm-up", "Cool-down"]
    assert wv["easy"][0]["pace_verdict"] == "on target"
    assert len(wv["chart"]["reps"]) == 6 and len(wv["chart"]["pauses"]) == 3


def test_page_and_week_link():
    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a, act = make(db)
    db.commit()
    aid, rid = a.id, act.id
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get(f"/athletes/{aid}/runs/{rid}").text
    assert 'id="workout"' in page and "Against the workout: 4 x 400" in page and "data-workout-chart" in page
    assert page.count("✓ on target") >= 4 and "Watch paused 3 times" in page and "Warm-up" in page and "184–200" in page
    week = c.get(f"/week?team=Boys&start={DAY - dt.timedelta(days=DAY.weekday())}").text
    assert f'href="/athletes/{aid}/runs/{rid}#workout"' in week


def test_fit_file_keeps_its_detail_and_a_resent_copy_fills_it_in(session, athlete):
    from fitgen import build_fit
    from xcapp.fitfile import parse_fit
    from xcapp.ingest import ingest
    data = build_fit(dt.datetime(2026, 10, 20, 22, 0, tzinfo=dt.timezone.utc),
                     [{"dist": 1600, "dur": 480, "hr": lambda s: 140}, {"dist": 400, "dur": 80, "hr": lambda s: 175}])
    act, outcome = ingest(session, athlete, parse_fit(data), "garmin_file")
    s = json.loads(act.stream)
    assert outcome == "created" and s["t"][0] == 0 and s["t"][-1] >= 555 and s["h"][0] == 140 and abs(s["d"][-1] - 2000) < 10
    act.stream = ""                                     # saved before streams were kept
    session.flush()
    again, outcome = ingest(session, athlete, parse_fit(data), "garmin_file")
    assert outcome == "duplicate" and again.id == act.id and again.stream

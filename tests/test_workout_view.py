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
    assert (wv["vdot"], wv["hr_max"], wv["any_recovery"]) == (V, 200, True)
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
    assert "✓ Rests OK</a>" in week and "rest 1:31" in page
    assert "Pace targets at VDOT 50." in page and "Heart-rate targets from a max of 200" in page and "No max heart rate" not in page


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


def test_recovery_column_paused_and_measured(session):
    a, act = make(session)
    wv = WV.build(session, act)
    reps = wv["reps"]
    assert [r["rec_note"] for r in reps[:3]] == ["Watch paused"] * 3 and reps[3]["rec_drop"] is not None
    assert any("watch was paused" in n for n in wv["recovery_notes"])


def test_recovery_shrinking_and_below_usual(session):
    from xcapp.models import Lap
    a = Athlete(first_name="Ann", last_name="Rec", gender="Girls", grade=11, group="Varsity", link_token="t-rec", vdot_override=V, hr_max=200)
    session.add(a)
    session.flush()
    easy = A.pace_at(V, A.EASY_PCT) / A.METERS_PER_MILE
    fast = A.pace_at(V, A.INTERVAL_PCT) / A.METERS_PER_MILE
    t, d, dist, hrs = 0.0, 0.0, [], []
    for k in range(300):                                   # warm-up
        dist.append((t, d)); hrs.append((t, 135)); t += 1; d += 1 / easy
    for after in (160, 165, 170, 176, 180):                # 5 x 400, then 90 s jogging with the watch running
        for k in range(int(400 * fast)):
            dist.append((t, d)); hrs.append((t, 190)); t += 1; d += 1 / fast
        for k in range(90):
            dist.append((t, d)); hrs.append((t, int(190 - (190 - after) * min(k / 60, 1)))); t += 1; d += 1 / (easy * 1.3)
    pw = PlannedWorkout(date=DAY, title="5 x 400", rep_distance_m=400, groups="All",
                        parts=json.dumps([{"sets": 1, "reps": 5, "dist": 400, "zone": "Interval", "rest": "90 s jog"}]))
    session.add(pw)
    session.flush()
    act = Activity(athlete_id=a.id, source="garmin_file", external_id="rec1", start_utc=dt.datetime(2026, 10, 20, 22), local_date=DAY,
                   duration_s=t, distance_m=d, sport="running", planned_workout_id=pw.id, stream=WV.make_stream(dist, hrs), zone_vdot=V)
    earlier = Activity(athlete_id=a.id, source="garmin_file", external_id="rec0", start_utc=dt.datetime(2026, 10, 13, 22),
                       local_date=DAY - dt.timedelta(days=7), duration_s=1800, distance_m=6000, sport="running")
    for i in range(6):
        earlier.laps.append(Lap(idx=i, start_offset_s=i * 200, elapsed_s=90, timer_s=90, distance_m=400, is_rep=True, hr_drop_60s=38))
    session.add_all([act, earlier])
    session.flush()
    wv = WV.build(session, act)
    drops = [round(r["rec_drop"]) for r in wv["reps"]]
    assert drops[:4] == [30, 25, 20, 14] and wv["reps"][4]["rec_drop"] == 10 and wv["reps"][4]["rec_last"]
    assert wv["reps"][0]["rec_end"] == 190 and wv["reps"][0]["rec_next"] is not None and wv["reps"][0]["rec_next"] < 190
    assert wv["usual_recovery"] == 38
    notes = " ".join(wv["recovery_notes"])
    assert "the athlete was tiring" in notes and "smaller than this athlete’s usual (38)" in notes
    assert WV.summary(wv)["label"] == "Slow recovery"


def test_rest_rules_by_zone():
    rep = {"t0": 0, "t1": 60, "secs": 60, "zone": "Repetition", "rest": "", "rec_end": 190, "rec_next": 150}
    nxt = {"t0": 60 + 90, "found": True}
    r = WV.rest_check(rep, nxt, 200)                       # 90 s after a 60 s R rep: Daniels wants 2-3 times
    assert r["rest_verdict"] == "short" and r["rest_target"].startswith("Daniels") and "not recovered" in r["ready_flag"]
    r = WV.rest_check(dict(rep, rec_next=135), {"t0": 60 + 150, "found": True}, 200)
    assert r["rest_verdict"] == "ok" and r["ready_flag"] == ""
    i = dict(rep, zone="Interval", secs=180, t1=180, rec_end=190, rec_next=184)
    r = WV.rest_check(i, {"t0": 180 + 170, "found": True}, 200)
    assert r["rest_verdict"] == "ok" and "barely came down" in r["ready_flag"]
    t = WV.rest_check(dict(rep, zone="Threshold", secs=300, t1=300, rest="1 min jog", rec_next=185), {"t0": 300 + 65, "found": True}, 200)
    assert t["rest_verdict"] == "ok" and t["rest_target"] == "1:00 written" and t["ready_flag"] == ""
    assert WV.rest_check(rep, None, 200)["rest_secs"] is None


def test_summaries_and_week_line(session):
    a, act = make(session)
    wv = WV.build(session, act)
    assert [r["rest_verdict"] for r in wv["reps"][:3]] == ["ok"] * 3                  # 90 s written, about 91 taken
    assert WV.summary(wv) == {"label": "Rests OK", "level": "good", "why": "3 of 3 rests about as planned."}
    first = WV.day_summary(session, act)
    assert first["label"] == "Rests OK" and json.loads(act.recovery_note)["for"] == act.planned_workout_id
    act.stream = ""                                                                   # saved: not worked out again
    assert WV.day_summary(session, act) == first


def test_athlete_run_page_chart_after_folding_effort():
    from xcapp.db import SessionLocal
    from xcapp.web import create_app
    db = SessionLocal()
    a, act = make(db)
    db.commit()
    rid = act.id
    db.close()
    c = TestClient(create_app())
    page = c.get(f"/a/t-wv/runs/{rid}").text
    assert '<details class="panel fold" id="effort" open>' in page and "data-workout-chart" in page
    assert page.index('id="effort"') < page.index("data-workout-chart")                 # the chart comes after the effort
    assert "Workouts page" not in page and "set by your coach" in page                  # said to the athlete
    db = SessionLocal()
    db.get(Activity, rid).rpe, db.get(Activity, rid).feel = 6, "Normal"
    db.commit()
    db.close()
    page = c.get(f"/a/t-wv/runs/{rid}").text
    assert '<details class="panel fold" id="effort">' in page and "✓ Effort 6 · Normal" in page

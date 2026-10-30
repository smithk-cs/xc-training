"""FIT decoding, rep detection, heart-rate recovery, de-duplication, workout matching, basic math."""
import datetime as dt

import pytest
from fit_tool.profile.profile_type import LapTrigger

from fitgen import build_fit, flat, rep_workout
from xcapp import analysis as A
from xcapp.fitfile import FitError, parse_fit
from xcapp.garmin import summary_to_parsed
from xcapp.ingest import ingest, rematch_day
from xcapp.models import PlannedWorkout, Result

START = dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc)  # 3:30 pm in Sacramento


def test_time_and_event_helpers():
    assert A.parse_time("16:42.3") == 1002.3
    assert A.parse_time("4:31") == 271
    assert A.parse_time("1:02:03") == 3723
    for bad in ("", "abc", "16:72", "-3:00"):
        with pytest.raises(ValueError):
            A.parse_time(bad)
    assert A.fmt_time(1002.34) == "16:42.3"
    assert A.fmt_time(59.96) == "1:00.0"
    assert A.normalize_event("1600m") == ("1 Mile", 1609.344 / 1600)
    assert A.normalize_event("5k") == ("5K", 1.0)
    assert A.normalize_event(" 2 Mile ") == ("2 Mile", 1.0)
    with pytest.raises(ValueError):
        A.normalize_event("800m")


def test_seasons_weeks_and_ratio():
    assert A.season_for(dt.date(2026, 8, 19)) == "2026 XC"
    assert A.season_for(dt.date(2026, 12, 5)) == "2027 Track"
    assert A.season_for(dt.date(2027, 3, 1)) == "2027 Track"
    assert A.week_starts(dt.date(2026, 10, 25), 3) == [dt.date(2026, 10, 5), dt.date(2026, 10, 12), dt.date(2026, 10, 19)]
    r = A.load_ratios([10, 10, 10, 10, 13, 20])
    assert r[:4] == [None] * 4 and r[4] == pytest.approx(1.3) and r[5] == pytest.approx(20 / 10.75)
    assert A.load_ratios([0, 0, 0, 0, 5])[4] is None


def test_structured_workout_reps_and_recovery(session, athlete):
    reps = [(1000, 200, 188, 35), (1000, 199, 190, 30), (1000, 201, 192, 22), (1000, 200, 193, 18), (1000, 198, 195, 0)]
    p = parse_fit(rep_workout(START, reps, structured=True))
    assert p.device == "Garmin Forerunner 265"
    assert len(p.laps) == 11 and p.is_running
    assert not hasattr(p, "lat") and not hasattr(p.laps[0], "lat")  # no positions kept
    session.add(PlannedWorkout(date=dt.date(2026, 10, 20), title="5 x 1K", rep_distance_m=1000, pace_event="5K"))
    session.flush()
    act, outcome = ingest(session, athlete, p, "upload")
    assert outcome == "created" and act.local_date == dt.date(2026, 10, 20)
    rep_laps = [l for l in act.laps if l.is_rep]
    assert [l.rep_num for l in rep_laps] == [1, 2, 3, 4, 5]
    assert [l.hr_drop_60s for l in rep_laps] == [35, 30, 22, 18, None]
    assert act.planned_workout_id is not None


def test_manual_laps_slow_athlete_threshold_reps(session, athlete):
    """The case that broke the first version: warm-up pace close to rep pace."""
    reps = [(1609, 495, 175, 28), (1609, 494, 177, 26), (1609, 496, 178, 0)]
    p = parse_fit(rep_workout(START, reps, structured=False, wu=(2400, 2400 / 2.95), cd=(2400, 2400 / 2.95)))
    assert {l.intensity for l in p.laps} == {"active"}
    act, _ = ingest(session, athlete, p, "upload")
    assert [l.hr_drop_60s for l in act.laps if l.is_rep] == [28, 26, None]


def test_easy_and_progression_runs_have_no_reps(session, athlete):
    easy = [{"dist": 1609.344, "dur": d, "hr": flat(145), "trigger": LapTrigger.DISTANCE} for d in (492, 488, 495, 486, 490)]
    act, _ = ingest(session, athlete, parse_fit(build_fit(START, easy)), "upload")
    assert not any(l.is_rep for l in act.laps)
    assert act.distance_m == pytest.approx(5 * 1609.344, rel=1e-3)
    prog = [{"dist": 1609.344, "dur": 1609.344 / v, "hr": flat(150)} for v in (3.0, 3.2, 3.45, 3.7)]
    act2, _ = ingest(session, athlete, parse_fit(build_fit(START + dt.timedelta(days=1), prog, serial=99)), "upload")
    assert not any(l.is_rep for l in act2.laps)


def test_bad_files_rejected():
    good = rep_workout(START, [(1000, 200, 180, 30), (1000, 200, 182, 0)])
    with pytest.raises(FitError):
        parse_fit(b"this is not a fit file at all")
    broken = bytearray(good)
    broken[len(broken) // 2] ^= 0xFF
    with pytest.raises(FitError):
        parse_fit(bytes(broken))
    with pytest.raises(FitError):
        parse_fit(b"")


def test_duplicates_and_summary_upgrade(session, athlete):
    data = rep_workout(START, [(1000, 200, 180, 30), (1000, 200, 182, 25), (1000, 200, 184, 0)])
    summary = summary_to_parsed({"summaryId": "s1", "activityId": 777, "activityType": "RUNNING",
                                 "startTimeInSeconds": int(START.timestamp()) + 20, "durationInSeconds": 1900,
                                 "distanceInMeters": 6000, "deviceName": "Forerunner 265"})
    a1, o1 = ingest(session, athlete, summary, "garmin_summary")
    assert o1 == "created" and not a1.laps and a1.device == "Garmin Forerunner 265"
    a2, o2 = ingest(session, athlete, parse_fit(data), "garmin_file")
    assert o2 == "upgraded" and a2.id == a1.id and sum(l.is_rep for l in a2.laps) == 3
    _, o3 = ingest(session, athlete, parse_fit(data), "upload")
    _, o4 = ingest(session, athlete, summary, "garmin_summary")
    assert (o3, o4) == ("duplicate", "duplicate")


def test_rematch_and_target_pace(session, athlete):
    act, _ = ingest(session, athlete, parse_fit(rep_workout(START, [(1000, 200, 180, 30), (1000, 200, 182, 0)])), "upload")
    assert act.planned_workout_id is None
    pw = PlannedWorkout(date=dt.date(2026, 10, 20), title="Reps", rep_distance_m=1000, pace_event="5K", pace_factor=1.0, groups="JV")
    session.add(pw)
    session.flush()
    assert rematch_day(session, pw.date) == 0  # Varsity athlete, JV-only workout
    pw.groups = "Varsity"
    assert rematch_day(session, pw.date) == 1 and act.planned_workout_id == pw.id
    session.add(Result(athlete_id=athlete.id, event="5K", time_s=1000.0, date=dt.date(2026, 10, 3), season="2026 XC"))
    session.flush()
    assert A.target_pace(session, athlete.id, pw) == pytest.approx(1000 / 5000 * 1609.344)


def easy_run(start, miles, serial):
    """A plain run with no reps: one lap per mile."""
    return parse_fit(build_fit(start, [{"dist": 1609.344, "dur": 480, "hr": flat(150)} for _ in range(miles)], serial=serial))


def test_assigned_workout_by_default(session, athlete):
    from xcapp.models import Activity
    pw = PlannedWorkout(date=dt.date(2026, 10, 20), title="Long run", rep_distance_m=8047, groups="Varsity")
    session.add(pw)
    session.flush()
    short, _ = ingest(session, athlete, easy_run(START - dt.timedelta(hours=8), 2, 1), "upload")
    assert short.planned_workout_id == pw.id                        # no reps, but it's the group's workout that day
    long, _ = ingest(session, athlete, easy_run(START, 5, 2), "upload")
    assert long.planned_workout_id == pw.id and short.planned_workout_id is None   # the longer run counts
    long.planned_workout_id, long.workout_locked = None, True       # the athlete says the long run wasn't it
    short.planned_workout_id, short.workout_locked = pw.id, True
    session.flush()
    later, _ = ingest(session, athlete, easy_run(START + dt.timedelta(hours=2), 6, 3), "upload")
    assert later.planned_workout_id is None and short.planned_workout_id == pw.id   # a choice made by hand is kept
    session.query(Activity).update({"workout_locked": False, "planned_workout_id": None})
    assert rematch_day(session, pw.date) == 1 and later.planned_workout_id == pw.id  # re-matching picks the longest
    pw.groups = "JV"
    assert rematch_day(session, pw.date) == 1 and later.planned_workout_id is None   # not this athlete's group


def test_zone_seconds_saved_on_arrival(session, athlete):
    import json
    athlete.vdot_override = 60
    session.flush()
    act, _ = ingest(session, athlete, parse_fit(rep_workout(START, [(1000, 200, 180, 30)] * 4)), "upload")
    zones = json.loads(act.zone_seconds)
    assert act.zone_vdot == 60 and zones.get("Interval", 0) + zones.get("Repetition", 0) > 400   # 4 x 200 s reps
    assert zones.get("Easy", 0) > 0 and sum(zones.values()) == pytest.approx(act.duration_s, rel=0.1)

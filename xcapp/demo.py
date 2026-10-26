"""Load the 62-athlete sample team so the app can be tried before Garmin access is approved.

The sample files hold weekly mileage and per-rep workout numbers. This loader rebuilds them into individual
runs, with laps and second-by-second heart rate for each rep workout, and feeds every run through the same
ingest, rep-detection and heart-rate-recovery code that real Garmin files go through. The test suite then
checks that the app's numbers come back out identical to the sample files.

Two of the four workouts are recorded as structured watch workouts (laps tagged interval / rest) and two as
manual lap presses, so both rep-detection paths are exercised.
"""
from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analysis import METERS_PER_MILE, season_for
from .config import settings
from .fitfile import ParsedActivity, ParsedLap
from .ingest import ingest
from .models import AiRun, Athlete, Goal, Injury, Insight, PainReport, PlannedWorkout, Result
from .security import new_link_token

DEMO_DIR = Path(__file__).parent / "demo"
# Dates for the sample season bests (the sample file has bests but no dates).
RESULT_DATES = {
    ("2025 XC", "1 Mile"): dt.date(2025, 8, 20), ("2025 XC", "1.5 Mile"): dt.date(2025, 9, 10),
    ("2025 XC", "2 Mile"): dt.date(2025, 9, 24), ("2025 XC", "5K"): dt.date(2025, 10, 25),
    ("2026 Track", "1 Mile"): dt.date(2026, 4, 18), ("2026 Track", "2 Mile"): dt.date(2026, 5, 2),
    ("2026 XC", "1 Mile"): dt.date(2026, 8, 19), ("2026 XC", "1.5 Mile"): dt.date(2026, 9, 9),
    ("2026 XC", "2 Mile"): dt.date(2026, 9, 23), ("2026 XC", "5K"): dt.date(2026, 10, 17),
}
# workout title -> (pace event, pace factor, rest seconds, structured watch workout?)
WORKOUT_SETUP = {
    "6 x 1K @ 5K pace": ("5K", 1.00, 90, True),
    "3 x 1 mile @ threshold": ("5K", 1.07, 90, False),
    "8 x 600m @ 2-mile pace": ("2 Mile", 1.00, 120, True),
    "5 x 1K @ 5K pace": ("5K", 1.00, 120, False),
}
REST_JOG_M = 200.0
EASY_FACTOR = 1.30  # easy pace is about 30% slower than 5K race pace
PAIN_SAMPLES = [("Willa Moreno", dt.date(2026, 10, 21), "Lower leg", 5, "Shin is sore on the left side"),
                ("Camila Takahashi", dt.date(2026, 10, 22), "Foot", 3, "")]


def _read(name: str) -> list[dict]:
    with open(DEMO_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _utc(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    local = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=settings().tz)
    return local.astimezone(dt.timezone.utc)


def _workout_activity(day: dt.date, reps: list[dict], rest_s: float, structured: bool, wu_m: float, ext: str, easy: float) -> ParsedActivity:
    start = _utc(day, 15, 30)
    laps: list[ParsedLap] = []
    hr: list[tuple[float, int]] = []
    t = 0.0

    def add_lap(dist, dur, avg, mx, intensity):
        nonlocal t
        laps.append(ParsedLap(start=start + dt.timedelta(seconds=t), elapsed_s=dur, timer_s=dur, distance_m=dist,
                              avg_hr=avg, max_hr=mx, intensity=intensity if structured else "active",
                              trigger="session_end" if structured else "manual"))
        t += dur

    wu_s = wu_m / easy
    for k in range(0, int(wu_s), 10):
        hr.append((float(k), 138 + (k % 30) // 10))
    add_lap(wu_m, wu_s, 140, 148, "warmup")
    for i, r in enumerate(reps):
        dist = float(r["rep_m"])
        dur = float(r["pace_sec_mi"]) * dist / METERS_PER_MILE
        avg, mx = int(r["avg_hr"]), int(r["max_hr"])
        rep_start = t
        # Heart rate rises through the rep and peaks at the max at the end of it.
        n = max(2, int(dur // 2))
        for k in range(n):
            frac = k / n
            hr.append((rep_start + frac * dur, int(round(avg - 6 + (mx - (avg - 6)) * frac))))
        hr.append((rep_start + dur, mx))
        add_lap(dist, dur, avg, mx, "interval" if structured else "active")
        if i < len(reps) - 1:
            drop = int(r["hr_drop_60s"]) if r["hr_drop_60s"] not in ("", None) else 30
            end = t
            for k in range(1, int(rest_s // 2)):
                s = k * 2.0
                if s >= rest_s:
                    break
                if s <= 60:
                    val = mx - drop * (s / 60.0)
                else:
                    val = mx - drop - 4 * ((s - 60) / max(1.0, rest_s - 60))
                hr.append((end + s, int(round(val))))
            hr.append((end + 60.0, mx - drop))
            add_lap(REST_JOG_M, rest_s, mx - drop, mx, "rest")
    cd_s = wu_m / easy
    for k in range(0, int(cd_s), 10):
        hr.append((t + k, 135))
    add_lap(wu_m, cd_s, 136, 150, "cooldown")
    hr.sort()
    dedup = {}
    for s, v in hr:
        dedup[round(s, 3)] = v
    total = sum(l.distance_m for l in laps)
    return ParsedActivity(external_id=ext, start=start, duration_s=t, distance_m=total, sport="running",
                          avg_hr=150.0, max_hr=float(max(int(r["max_hr"]) for r in reps)), device="Garmin Forerunner 265",
                          laps=laps, hr_samples=sorted(dedup.items()))


def _easy_run(day: dt.date, meters: float, ext: str, easy: float) -> ParsedActivity:
    start = _utc(day, 15, 45)
    dur = meters / easy
    lap = ParsedLap(start=start, elapsed_s=dur, timer_s=dur, distance_m=meters, avg_hr=145, max_hr=158, intensity="active", trigger="session_end")
    return ParsedActivity(external_id=ext, start=start, duration_s=dur, distance_m=meters, sport="running",
                          avg_hr=145.0, max_hr=158.0, device="Garmin Forerunner 265", laps=[lap], hr_samples=[])


def _sample_effort(db: Session) -> None:
    """Invented effort ratings for about 70% of sample runs: workouts hard, Saturday long runs moderate, the rest easy."""
    import random
    from .models import Activity
    rng = random.Random(42)
    tired = {"Lucia Sorensen", "Adrian Iverson", "Ruby Haddad"}
    for act in db.query(Activity).all():
        if rng.random() > 0.7:
            continue
        late = act.local_date >= dt.date(2026, 10, 19) and act.athlete.name in tired
        base = 7 if act.planned_workout_id else (5 if act.local_date.weekday() == 5 else 3)
        act.rpe = min(10, base + rng.choice([-1, 0, 0, 1]) + (2 if late else 0))
        act.feel = "Heavy" if late else rng.choice(["Fresh", "Normal", "Normal", "Normal", "Heavy"])
    db.flush()


def load_demo(db: Session) -> dict:
    if db.scalar(select(func.count(Athlete.id))):
        raise RuntimeError("The database already has athletes. Load the demo into an empty database.")
    roster = _read("roster.csv")
    by_code: dict[str, Athlete] = {}
    for r in roster:
        first, last = r["athlete"].split(" ", 1)
        a = Athlete(first_name=first, last_name=last, gender=r["gender"], grade=int(r["grade"]), group=r["group"],
                    hr_max=int(r["hr_max"]), hr_rest=int(r["hr_rest"]), link_token=new_link_token(),
                    ai_consent_at=dt.datetime(2026, 9, 1), ai_consent_note="Sample athlete (invented).")
        db.add(a)
        db.flush()
        if a.code != r["athlete_id"]:
            raise RuntimeError("Demo ids did not line up; use an empty database.")
        by_code[r["athlete_id"]] = a

    easy_speed: dict[str, float] = {}
    for b in _read("benchmarks.csv"):
        if b["season"] == "2026 XC" and b["event"] == "5K":
            easy_speed[b["athlete_id"]] = 5000.0 / float(b["time_sec"]) / EASY_FACTOR
        d = RESULT_DATES[(b["season"], b["event"])]
        assert season_for(d) == b["season"]
        db.add(Result(athlete_id=by_code[b["athlete_id"]].id, event=b["event"], time_s=float(b["time_sec"]), date=d,
                      season=b["season"], course="Sample course", kind="track" if "Track" in b["season"] else "race"))
    for g in _read("goals.csv"):
        db.add(Goal(athlete_id=by_code[g["athlete_id"]].id, event=g["event"], season="2026 XC", goal_s=float(g["goal_sec"])))
    for i in _read("injuries.csv"):
        db.add(Injury(athlete_id=by_code[i["athlete_id"]].id, onset_date=dt.date.fromisoformat(i["onset_date"]),
                      injury=i["injury"], body_area=i["body_area"], return_date=dt.date.fromisoformat(i["return_date"]),
                      logged_by="demo"))
    names = {a.name: a for a in by_code.values()}
    for name, day, area, sev, note in PAIN_SAMPLES:
        db.add(PainReport(athlete_id=names[name].id, date=day, body_area=area, severity=sev, note=note))

    segs = _read("segments.csv")
    plans: dict[str, PlannedWorkout] = {}
    for s in segs:
        if s["workout_id"] not in plans:
            ev, factor, _, _ = WORKOUT_SETUP[s["workout"]]
            pw = PlannedWorkout(date=dt.date.fromisoformat(s["date"]), title=s["workout"], rep_distance_m=float(s["rep_m"]),
                                pace_event=ev, pace_factor=factor, groups="All")
            db.add(pw)
            plans[s["workout_id"]] = pw
    db.flush()

    reps = defaultdict(list)
    for s in segs:
        reps[(s["athlete_id"], s["date"])].append(s)
    for v in reps.values():
        v.sort(key=lambda r: int(r["rep"]))

    created = 0
    skipped: list[tuple[str, str]] = []
    for w in _read("weekly_load.csv"):
        a = by_code[w["athlete_id"]]
        week = dt.date.fromisoformat(w["week_start"])
        total_m = float(w["miles"]) * METERS_PER_MILE
        days = [week + dt.timedelta(days=k) for k in range(6)]  # Monday to Saturday
        used = 0.0
        work_days = [d for d in days if (w["athlete_id"], d.isoformat()) in reps]
        for d in list(work_days):
            rr = reps[(w["athlete_id"], d.isoformat())]
            _, _, rest_s, structured = WORKOUT_SETUP[rr[0]["workout"]]
            core = sum(float(r["rep_m"]) for r in rr) + REST_JOG_M * (len(rr) - 1)
            if total_m - used < core + 800:
                # The sample file marks this whole week as an injury week with very low miles, but also lists
                # a workout before the injury began. The miles can't hold the workout, so it is left out.
                skipped.append((w["athlete_id"], d.isoformat()))
                work_days.remove(d)
                continue
            wu = min(2400.0, (total_m - used - core) / 4)
            act = _workout_activity(d, rr, rest_s, structured, wu, f"demo:{a.code}:{d}:w", easy_speed[w["athlete_id"]])
            ingest(db, a, act, "demo")
            used += act.distance_m
            created += 1
        remaining = total_m - used
        easy_days = [d for d in days if d not in work_days]
        weights = [1.6 if d.weekday() == 5 else 1.0 for d in easy_days]
        if remaining > 80:
            share = remaining / sum(weights)
            parts = [share * wt for wt in weights]
            parts[-1] = remaining - sum(parts[:-1])  # exact total
            for d, m in zip(easy_days, parts):
                if m > 1:
                    ingest(db, a, _easy_run(d, m, f"demo:{a.code}:{d}:e", easy_speed[w["athlete_id"]]), "demo")
                    created += 1

    _sample_effort(db)
    run = AiRun(kind="briefing", status="ok", model="sample output", created_at=dt.datetime(2026, 10, 23, 13, 0))
    db.add(run)
    for pos, r in enumerate(_read("ai_insights.csv"), start=1):
        aid = by_code[r["athlete_id"]].id if r["athlete_id"] else None
        run.insights.append(Insight(position=pos, priority=r["priority"], scope=r["scope"], athlete_id=aid,
                                    category=r["category"], headline=r["headline"], insight=r["insight"], action=r["action"]))
    db.flush()
    return {"athletes": len(by_code), "activities": created, "workouts": len(plans), "skipped_workouts": skipped}

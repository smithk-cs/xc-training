"""The AI checks each run against its team workout: only athletes who agreed, no names or typed notes, strict replies."""
import datetime as dt
import json
import types

from fitgen import rep_workout
from xcapp import ai as AI
from xcapp.fitfile import parse_fit
from xcapp.ingest import ingest
from xcapp.models import Athlete, PlannedWorkout

DAY = dt.date(2026, 10, 20)
START = dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=self.replies.pop(0))], stop_reason="end_turn",
                                     usage=types.SimpleNamespace(input_tokens=1000, output_tokens=200))


def reply(*verdicts):
    return json.dumps({"checks": [{"id": f"W{i}", "verdict": v, "note": f"4 of 4 reps at target ({v})."} for i, v in enumerate(verdicts, 1)]})


def setup(session):
    yes = Athlete(first_name="Lucia", last_name="Sorensen", gender="Girls", grade=11, group="Varsity", link_token="t1",
                  vdot_override=60, ai_consent_at=dt.datetime(2026, 10, 1))
    no = Athlete(first_name="Ruby", last_name="Haddad", gender="Girls", grade=11, group="Varsity", link_token="t2", vdot_override=55)
    pw = PlannedWorkout(date=DAY, title="Lucia's SECRET-TITLE repeats", rep_distance_m=1000, groups="All",
                        parts=json.dumps([{"sets": 1, "reps": 4, "dist": 1000, "zone": "Interval", "rest": "SECRET-REST jog"}]))
    session.add_all([yes, no, pw])
    session.flush()
    reps = [(1000, 205, 180, 30)] * 4
    a1, _ = ingest(session, yes, parse_fit(rep_workout(START, reps)), "upload")
    a2, _ = ingest(session, no, parse_fit(rep_workout(START, reps)), "upload")
    assert a1.planned_workout_id == a2.planned_workout_id == pw.id
    return a1, a2, pw


def test_check_workouts(session):
    a1, a2, pw = setup(session)
    assert AI.pending_workout_checks(session, DAY) == [a1]                     # only the athlete who agreed to AI analysis
    client = FakeClient(["not json", reply("Done")])                          # a bad reply is retried once
    run = AI.check_workouts(session, client, DAY)
    assert run.status == "ok" and run.kind == "workout_check" and len(client.calls) == 2
    sent = client.calls[0]["messages"][0]["content"]
    for secret in ("Lucia", "Sorensen", "Ruby", "SECRET-TITLE", "SECRET-REST"):
        assert secret not in sent                                               # no names or typed text
    data = json.loads(sent.split("DATA:\n")[1].split("\n\n")[0])
    part = data["runs"][0]["workout"]["parts"][0]
    assert part["reps"] == 4 and part["meters"] == 1000 and part["zone"] == "Interval" and 300 < part["target_pace_s_per_mile"] < 400
    assert sum(l["rep"] for l in data["runs"][0]["run"]["laps"]) == 4
    assert (a1.workout_check, a1.workout_check_for) == ("Done", pw.id) and "4 of 4 reps" in a1.workout_check_note
    assert a2.workout_check == ""
    assert AI.check_workouts(session, FakeClient([]), DAY) is None            # nothing left to check: no request

    other = PlannedWorkout(date=DAY, title="Tempo", rep_distance_m=1600, groups="All")
    session.add(other)
    session.flush()
    a1.planned_workout_id = other.id                                            # linked to another workout: checked again
    assert AI.pending_workout_checks(session, DAY) == [a1]
    run = AI.check_workouts(session, FakeClient([reply("Partly")]), DAY)
    assert run.status == "ok" and (a1.workout_check, a1.workout_check_for) == ("Partly", other.id)


def test_bad_replies_fail_cleanly(session):
    a1, _, _ = setup(session)
    bad = [json.dumps({"checks": [{"id": "W1", "verdict": "Maybe", "note": "x"}]}), json.dumps({"checks": [{"id": "W9", "verdict": "Done", "note": "x"}]})]
    run = AI.check_workouts(session, FakeClient(bad), DAY)
    assert run.status == "failed" and "could not be used" in run.error and a1.workout_check == ""
    assert AI.pending_workout_checks(session, DAY) == [a1]                     # tried again next time


def test_pages_show_the_check():
    import re

    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    a1, a2, pw = setup(db)
    AI.check_workouts(db, FakeClient([reply("Partly")]), DAY)
    ids = (a1.athlete_id, a1.id, a2.athlete_id, a2.id)
    db.commit()
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get(f"/athletes/{ids[0]}/runs/{ids[1]}").text
    assert 'AI check against the workout' in page and '<span class="pill warn">Partly</span> 4 of 4 reps at target (Partly).' in page
    assert "hasn’t agreed to AI analysis" in c.get(f"/athletes/{ids[2]}/runs/{ids[3]}").text
    assert "1 partly" in c.get("/workouts/all").text

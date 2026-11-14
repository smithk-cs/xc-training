"""Running form put to work: focus and drills, progress over time, re-film reminders, and the fresh/tired fatigue check."""
import datetime as dt
import json
import pathlib
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import form_plan as FP
from xcapp.models import Athlete, FormAnalysis, StaffUser
from xcapp.security import hash_password

GOOD = {"overstride_deg": 3, "shin_deg": 4, "front_knee_deg": 160, "back_knee_deg": 80, "trunk_deg": 7, "head_deg": -10,
        "elbow_deg": 90, "cadence": 180, "oscillation_pct": 8}
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, hour=18)


def film(db, a, days_ago, condition="fresh", **changes):
    fa = FormAnalysis(athlete_id=a.id, by="Coach", pace_s=400, condition=condition, created_at=NOW - dt.timedelta(days=days_ago),
                      data=json.dumps({"metrics": {**GOOD, **changes}}), score=90)
    db.add(fa)
    db.flush()
    return fa


def team(db):
    sam = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="", link_token="tok-sam")
    ana = Athlete(first_name="Ana", last_name="Lee", gender="Boys", grade=10, group="", link_token="tok-ana")
    leo = Athlete(first_name="Leo", last_name="Park", gender="Boys", grade=9, group="", link_token="tok-leo")
    db.add_all([sam, ana, leo])
    db.flush()
    first = film(db, sam, 60, overstride_deg=16, cadence=158)
    latest = film(db, sam, 10, overstride_deg=11, cadence=162)
    tired = film(db, sam, 8, "tired", overstride_deg=13, cadence=160, trunk_deg=16)
    old = film(db, ana, 50, elbow_deg=140)
    db.flush()
    return sam, ana, leo, first, latest, tired, old


def test_focus_progress_due_and_fatigue(session):
    sam, ana, leo, first, latest, tired, old = team(session)
    assert FP.latest(session, [sam.id])[sam.id].id == latest.id                       # the latest filmed fresh, not the tired one
    assert [c["key"] for c in FP.focus(latest)] == ["overstride_deg", "cadence"]
    groups = {(g["key"], g["state"]): [a.first_name for a in g["people"]] for g in FP.focus_groups(session)}
    assert groups == {("overstride_deg", "high"): ["Sam"], ("cadence", "low"): ["Sam"], ("elbow_deg", "high"): ["Ana"]}
    h = FP.history(session, sam.id)
    assert len(h["rows"]) == 3 and h["moves"]["overstride_deg"] == "better" and h["moves"]["cadence"] == "better"
    assert h["moves"]["trunk_deg"] == "" and h["rows"][2]["condition"] == "tired"
    due = FP.due(session, A.team_today())
    assert [a.first_name for a, _ in due["stale"]] == ["Ana"] and [a.first_name for a in due["never"]] == ["Leo"]
    assert FP.refilm_on(session, sam.id) == FP.day_of(latest) + dt.timedelta(days=42) and FP.refilm_on(session, leo.id) is None
    f = FP.fatigue(session, sam.id)
    assert f["fresh"].id == latest.id and f["tired"].id == tired.id                    # the nearest fresh clip to the tired one
    verdicts = {r["key"]: r["verdict"] for r in f["rows"]}
    assert verdicts["trunk_deg"] == "fatigue" and verdicts["overstride_deg"] == "technique" and verdicts["elbow_deg"] == "ok"
    assert f["rows"][0]["key"] == "trunk_deg" and f["counts"]["fatigue"] == 1 and f["counts"]["technique"] == 2
    assert FP.fatigue(session, sam.id, latest)["tired"].id == tired.id                # viewed from the fresh clip: paired with it...
    assert FP.fatigue(session, sam.id, first) is None                                   # (52 days before the tired one: too far)
    assert FP.fatigue(session, ana.id) is None                                         # ...and no tired clip, no check
    film(session, ana, 5, "tired", elbow_deg=140)                                      # 45 days apart: too far to compare
    assert FP.fatigue(session, ana.id) is None


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c


def test_pages(session):
    sam, ana, leo, first, latest, tired, old = team(session)
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    sid, aid, tid, fid = sam.id, ana.id, tired.id, latest.id
    from xcapp.web import create_app
    app = create_app()
    c = login(app)
    form = c.get("/form").text
    assert '<select id="condition">' in form and "Tired (end of a workout)" in form and "Fatigue check:" in form
    assert "Due for a re-film" in form and "Ana Lee" in form.split('id="due"')[1].split("</details>")[0]
    due = form.split('id="due"')[1].split("</details>")[0]
    assert ">Leo Park<" in due and ">Sam Reyes<" not in due and 'class="pill warn">Tired' in form   # never filmed is due too
    workouts = c.get("/workouts").text
    assert "Form focus groups" in workouts and "Reaches out in front (overstriding)." in workouts and "A-skips" in workouts
    detail = c.get(f"/athletes/{sid}").text
    assert "Focus and drills" in detail and "Progress" in detail and "↑ better" in detail and "Next re-film due" in detail
    assert "Fatigue check" in detail and ">Fatigue</span>" in detail and ">Technique</span>" in detail
    report = c.get(f"/athletes/{sid}/form/{tid}").text
    assert "Tired: end of a workout" in report and "Fatigue check" in report
    assert "Fatigue check" in c.get(f"/athletes/{sid}/form/{fid}").text                # paired from the fresh side too
    assert "Due for a re-film" in c.get(f"/athletes/{aid}").text
    mine = TestClient(app).get("/a/tok-sam").text
    assert "What to work on" in mine and "Cue:" in mine and "Progress" in mine and "/a/tok-sam/form/" in mine
    # Saving a clip filmed tired keeps it as tired
    page = c.get(f"/form?athlete={sid}").text
    csrf = re.search(r'"csrf": "([^"]+)"', page).group(1)
    body = {**json.loads((pathlib.Path(__file__).parent / "form_sample.json").read_text()), "condition": "tired", "csrf": csrf}
    url = c.post(f"/athletes/{sid}/form", json=body).json()["url"]
    from xcapp.db import SessionLocal
    db = SessionLocal()
    assert db.get(FormAnalysis, int(url.rsplit("/", 1)[1])).condition == "tired"
    db.close()
    body["condition"] = "anything"
    url = c.post(f"/athletes/{sid}/form", json=body).json()["url"]
    db = SessionLocal()
    assert db.get(FormAnalysis, int(url.rsplit("/", 1)[1])).condition == "fresh"
    db.close()


def test_film_due_by_phase(session):
    from xcapp.models import Injury, SeasonWeek
    today = A.team_today()
    monday = A.week_monday(today)
    sam, ana, leo, first, latest, tired, old = team(session)     # Sam filmed fresh 10 days ago and tired 8 days ago
    session.add(Injury(athlete_id=ana.id, onset_date=today - dt.timedelta(days=3), injury="Shin splints", body_area="Shin"))
    for k in (-2, -1, 0, 1):                                     # Phase I since two weeks ago
        session.add(SeasonWeek(monday=monday + dt.timedelta(days=7 * k), phase=1, miles_pct=80))
    session.flush()
    f = FP.film_status(session, today)
    assert f["block"]["phase"] == 1 and f["block"]["start"] == monday - dt.timedelta(days=14) and f["kind"] == "fresh"
    assert f["status"] == {sam.id: "done", ana.id: "wait", leo.id: "due"}           # Sam's clip is inside the block
    # In Phase I's first week, nobody is due yet (a week of running first)
    for w in session.query(SeasonWeek).filter(SeasonWeek.monday < monday).all():
        session.delete(w)
    session.flush()
    f = FP.film_status(session, today)
    assert f["status"][leo.id] == "soon" and f["from"] == monday + dt.timedelta(days=7)
    # Phase III: a tired clip is what counts
    for w in session.query(SeasonWeek).all():
        w.phase = 3
    session.add(SeasonWeek(monday=monday - dt.timedelta(days=7), phase=3, miles_pct=100))
    session.flush()
    f = FP.film_status(session, today)
    assert f["kind"] == "tired" and f["status"][sam.id] == "done" and f["status"][leo.id] == "due"   # Sam's tired clip, 8 days ago
    # Phase IV: optional, nobody is marked
    for w in session.query(SeasonWeek).all():
        w.phase = 4
    session.flush()
    f = FP.film_status(session, today)
    assert f["kind"] is None and f["status"] == {} and f["due"] == []


def test_film_markers_on_pages(session):
    from xcapp.models import SeasonWeek
    today = A.team_today()
    sam, ana, leo, first, latest, tired, old = team(session)
    session.add(SeasonWeek(monday=A.week_monday(today) - dt.timedelta(days=7), phase=2, miles_pct=90))
    session.add(SeasonWeek(monday=A.week_monday(today), phase=2, miles_pct=90))
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    sid, lid = sam.id, leo.id
    from xcapp.web import create_app
    c = login(create_app())
    roster = c.get("/athletes").text
    assert f'<a class="pill warn" href="/form?athlete={lid}"' in roster and "Film fresh</a>" in roster
    assert "Film fresh due" in c.get(f"/athletes/{lid}").text and "Phase II re-film" in c.get(f"/athletes/{lid}").text
    form = c.get("/form").text
    assert "Film for Phase II" in form and "Leo Park" in form.split('id="due"')[1].split("</details>")[0]
    assert f'value="{lid}">Park, Leo · 9th grade · film fresh due' in form

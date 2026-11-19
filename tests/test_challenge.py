"""Mileage challenges (challenge.py): the leaderboard, streaks, goals and squads, names on the athlete's board, and the
pages. Names are made up."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import challenge as CH
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, MileageChallenge, Squad, SquadCompetition, SquadMember, StaffUser
from xcapp.security import hash_password

TODAY = A.team_today()
START = TODAY - dt.timedelta(days=9)


def run(db, a, days_ago, miles, sport="running"):
    d = TODAY - dt.timedelta(days=days_ago)
    db.add(Activity(athlete_id=a.id, source="upload", external_id=f"{a.id}-{days_ago}-{sport}", start_utc=dt.datetime(d.year, d.month, d.day, 15),
                    local_date=d, duration_s=miles * 480, distance_m=miles * 1609.344, sport=sport))


def setup(db):
    people = []
    for i, (first, g, show) in enumerate((("Sam", "Boys", True), ("Ben", "Boys", False), ("Ava", "Girls", True))):
        a = Athlete(first_name=first, last_name="Lowry", gender=g, grade=11, group="Varsity", link_token=f"t{i}", show_name=show)
        db.add(a)
        people.append(a)
    db.flush()
    sam, ben, ava = people
    for k in (0, 1, 2, 3, 6):
        run(db, sam, k, 5)                                  # 25 mi, streak 4 now
    for k in range(0, 8):
        run(db, ben, k, 4)                                  # 32 mi, 8 days in a row
    run(db, ben, 3, 10, sport="cycling")                    # not a run
    run(db, ava, 20, 10)                                    # before the challenge
    run(db, ava, 1, 6)
    comp = SquadCompetition(name="Fall squads", gender="All", start_date=START)
    db.add(comp)
    db.flush()
    red, blue = Squad(competition_id=comp.id, name="Red", position=0), Squad(competition_id=comp.id, name="Blue", position=1)
    db.add_all([red, blue])
    db.flush()
    db.add_all([SquadMember(competition_id=comp.id, squad_id=red.id, athlete_id=sam.id),
                SquadMember(competition_id=comp.id, squad_id=red.id, athlete_id=ava.id),
                SquadMember(competition_id=comp.id, squad_id=blue.id, athlete_id=ben.id)])
    c = MileageChallenge(name="Summer miles", start_date=START, end_date=TODAY + dt.timedelta(days=20), goal_miles=30, squads_id=comp.id)
    db.add(c)
    db.flush()
    return people, c


def test_board(session):
    (sam, ben, ava), c = setup(session)
    b = CH.board(session, c, TODAY)
    rows = {r["athlete"].first_name: r for r in b["rows"]}
    assert [r["athlete"].first_name for r in b["rows"]] == ["Ben", "Sam", "Ava"] and b["total"] == 63.0
    assert (rows["Ben"]["miles"], rows["Ben"]["days"], rows["Ben"]["best"], rows["Ben"]["now"], rows["Ben"]["pct"]) == (32.0, 8, 8, 8, 107)
    assert (rows["Sam"]["best"], rows["Sam"]["now"]) == (4, 4) and rows["Ava"]["miles"] == 6.0
    assert [(s["squad"].name, s["avg"]) for s in b["squads"]] == [("Blue", 32.0), ("Red", 15.5)]
    assert b["days_left"] == 20 and CH.current(session, TODAY, "Girls") is c
    c.team = "Boys"
    assert [r["athlete"].first_name for r in CH.board(session, c, TODAY)["rows"]] == ["Ben", "Sam"]
    assert CH.current(session, TODAY, "Girls") is None
    assert CH.shown_name(ben, sam.id) == "Teammate" and CH.shown_name(sam, ben.id) == "Sam Lowry"


def test_pages():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    setup(db)
    db.commit()
    db.close()
    from xcapp.web import create_app
    app = create_app()
    me = TestClient(app).get("/a/t0").text                             # Sam
    assert 'id="challenge"' in me and "Summer miles" in me and "25.0 of 30 miles" in me and "Teammate" in me
    assert "#2 of 3" in me
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/challenge").text
    assert 'href="/challenge"' in page and "Leaderboard" in page and "Ben Lowry" in page and "✓ 107%" in page and "Blue" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    new = c.get("/challenge?new=1").text
    assert "Set up a challenge" in new
    c.post("/challenge", data={"csrf": csrf, "name": "Bad", "start_date": "2026-08-10", "end_date": "2026-08-01"})
    assert "The last day is before the first" in c.get("/challenge").text
    r = c.post("/challenge", data={"csrf": csrf, "name": "Winter base", "start_date": TODAY.isoformat(),
                                   "end_date": (TODAY + dt.timedelta(days=30)).isoformat(), "team": "Girls", "goal_miles": ""})
    db = SessionLocal()
    assert db.query(MileageChallenge).count() == 2
    w = db.query(MileageChallenge).filter_by(name="Winter base").one()
    assert w.team == "Girls" and w.goal_miles is None
    wid = w.id
    db.close()
    assert r.headers["location"] == f"/challenge?id={wid}"
    c.post("/challenge", data={"csrf": csrf, "id": str(wid), "action": "delete"})
    db = SessionLocal()
    assert db.query(MileageChallenge).count() == 1
    db.close()

"""Team place and points for each race at a meet (such as Boys Frosh/Soph)."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp.db import SessionLocal
from xcapp.models import Athlete, MeetConditions, RaceResult, RaceTeamResult, StaffUser
from xcapp.security import hash_password

DAY = dt.date(2026, 10, 24)


def test_team_place_and_points():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    for i in range(6):
        a = Athlete(first_name=f"Run{i}", last_name="Ner", gender="Boys", grade=10, group="JV", link_token=f"t{i}")
        db.add(a)
        db.flush()
        db.add(RaceResult(athlete_id=a.id, meet="Flat SAC", date=DAY, race="Boys Frosh/Soph", distance_m=5000,
                          distance_label="5,000 Meters", time_s=1100 + i * 10, place=3 + i * 4, points=float(2 + i * 3)))
    db.commit()
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/races?season=all").text
    assert "Team place and points" in page and "<th class=\"num\">Points</th>" in page
    assert 'name="points" value="40"' in page and "top five runners’ points" in page        # 2 + 5 + 8 + 11 + 14 suggested
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    base = {"csrf": csrf, "date": DAY.isoformat(), "meet": "Flat SAC", "race": "Boys Frosh/Soph"}
    c.post("/races/team", data={**base, "place": "5", "teams": "3", "points": ""})
    assert "Team place and number of teams" in c.get("/races?season=all").text            # 5th of 3 is refused
    c.post("/races/team", data={**base, "place": "3", "teams": "12", "points": "41"})
    page = c.get("/races?season=all").text
    assert "Team: <b>3rd</b> of 12 teams · <b>41</b> points" in page
    assert "Team results (1)" in page and "<td>Boys Frosh/Soph</td>" in page

    # Renaming the race and the meet carries the team result (and race-day conditions) along.
    db = SessionLocal()
    db.add(MeetConditions(meet="Flat SAC", date=DAY, temp_f=80))
    db.commit()
    db.close()
    c.post("/races/edit-race", data={**base, "new_race": "Boys F/S", "meters": "5000", "distance": "5,000 Meters"})
    c.post("/races/edit-meet", data={"csrf": csrf, "date": DAY.isoformat(), "meet": "Flat SAC", "name": "Flat SAC Invite",
                                     "new_date": DAY.isoformat(), "course": ""})
    db = SessionLocal()
    t = db.query(RaceTeamResult).one()
    assert (t.meet, t.race, t.place, t.points) == ("Flat SAC Invite", "Boys F/S", 3, 41)
    assert db.query(MeetConditions).one().meet == "Flat SAC Invite"
    db.close()
    c.post("/races/delete", data={"csrf": csrf, "date": DAY.isoformat(), "meet": "Flat SAC Invite"})
    db = SessionLocal()
    assert db.query(RaceTeamResult).count() == 0 and db.query(MeetConditions).count() == 0
    db.close()

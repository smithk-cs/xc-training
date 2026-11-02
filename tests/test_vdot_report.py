import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import vdot_report as VR
from xcapp.models import Athlete, RaceResult, VdotChange

MEET = dt.date(2026, 10, 17)


def runner(db, first, **kw):
    a = Athlete(first_name=first, last_name="Runner", gender="Boys", grade=11, group="Varsity", link_token=f"t-{first}", **kw)
    db.add(a)
    db.flush()
    return a


def race(db, a, day, meet, secs, meters=5000):
    db.add(RaceResult(athlete_id=a.id, meet=meet, date=day, race="Boys Varsity", distance_m=meters, distance_label="5K", time_s=secs))


def test_coach_vdot_history():
    ch = [VdotChange(date=dt.date(2026, 9, 1), vdot=50, previous=None), VdotChange(date=dt.date(2026, 10, 1), vdot=54, previous=50)]
    assert VR.coach_vdot_on(ch, 54, dt.date(2026, 8, 1)) is None
    assert VR.coach_vdot_on(ch, 54, dt.date(2026, 9, 15)) == 50 and VR.coach_vdot_on(ch, 54, dt.date(2026, 10, 16)) == 54
    assert VR.coach_vdot_on([], 52, dt.date(2026, 10, 16)) == 52            # set before any history was kept


def test_report_and_page(session):
    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    up, same, down, new = (runner(session, n) for n in ("Up", "Same", "Down", "New"))
    coach = runner(session, "Coach")
    for a, t in ((up, 1020), (same, 1000), (down, 980), (coach, 1000)):
        race(session, a, dt.date(2026, 10, 3), "Early Invite", t)
    for a, t in ((up, 990), (same, 1000), (down, 1010), (new, 1100), (coach, 1000)):
        race(session, a, MEET, "League Meet", t)
    session.add(VdotChange(athlete_id=coach.id, date=dt.date(2026, 9, 1), vdot=65, previous=None))
    coach.vdot_override = 65
    session.flush()
    rep = VR.report(session, MEET, "League Meet", MEET - dt.timedelta(days=1), {"up", "same", "down", "new"}, MEET)
    by = {x["a"].first_name: x for x in rep["rows"]}
    assert by["Up"]["change"] > 0 and by["Same"]["change"] == 0 and by["Down"]["change"] < 0 and by["New"]["kind"] == "new"
    assert by["Coach"]["was"] == 65 and by["Coach"]["source"] == "set by a coach" and by["Coach"]["change"] < 0
    assert rep["counts"] == {"up": 1, "same": 1, "down": 2, "new": 1}
    assert [x["a"].first_name for x in rep["rows"]][0] == "Up"                        # biggest gain first
    only = VR.report(session, MEET, "League Meet", MEET - dt.timedelta(days=1), {"up", "new"}, MEET)
    assert {x["a"].first_name for x in only["rows"]} == {"Up", "New"}
    # Comparing with the day of the meet itself: VDOT is the fastest result, so a faster meet is the VDOT already (no
    # change), and a slower one still shows below the earlier best
    same_day = VR.report(session, MEET, "League Meet", MEET, {"up", "same", "down", "new"}, MEET)
    assert {x["a"].first_name: x["kind"] for x in same_day["rows"] if x["a"].first_name != "Coach"} == {
        "Up": "same", "Same": "same", "Down": "down", "New": "same"}
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert 'href="/reports/vdot"' in c.get("/reports").text
    page = c.get("/reports/vdot").text                                          # newest meet, the day before it
    assert "League Meet" in page and 'value="2026-10-16"' in page and "1 up" in page and "2 down" in page
    page = c.get("/reports/vdot?race=2026-10-17|League Meet&before=2026-10-16&show=down").text
    assert page.count("<tr>") == 3 and "Down Runner" in page and "Up Runner" not in page    # header + 2 rows
    assert '<span class="arrow down" aria-label="down' in page and ">Athlete</th><th>Group</th><th class=\"num\">VDOT</th><th class=\"num\">Distance</th>" in page
    every = c.get("/reports/vdot?race=2026-10-17|League Meet&before=2026-10-16").text
    assert '<span class="arrow up"' in every and '<span class="arrow same" aria-label="no change">–</span>' in every and '<span class="arrow new"' in every
    assert ">Early Invite</option>" not in page and "· Early Invite</option>" in page


def test_ai_text_in_minutes_and_seconds():
    assert A.mmss_text("Reps averaged 85 seconds; pace 412 s/mi, 48 s rest, HR 170 bpm") == "Reps averaged 1:25; pace 6:52/mi, 48 s rest, HR 170 bpm"

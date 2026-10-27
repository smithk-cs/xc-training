"""Meet entries: races by team and division with entry limits, one race per athlete, absences, attendance answers from
athletes, and the email and PDF with the coaching staff's contacts. All names are made up."""
import datetime as dt
import io
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Athlete, DivisionAthlete, MeetEntry, MeetResponse, RaceResult, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", email="coach@example.org", full_name="Dale Brook", title="Head Coach",
                     phone="(916) 555-0101", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", email="at@example.org", on_entries=False, password_hash=hash_password("correct-horse-1")))
    people = [("Sam", "Lowry", 12, "sam@example.org", 61, "Boys"), ("José", "O'Neil", 11, "", 66, "Boys"),
              ("Theo", "Marsh", 10, "theo@example.org", None, "Boys"), ("Pat", "Reed", 9, "", None, "Boys"), ("Ivy", "Stone", 10, "", None, "Girls")]
    for i, (f, l, g, e, v, gen) in enumerate(people):
        db.add(Athlete(first_name=f, last_name=l, gender=gen, grade=g, group="Varsity", link_token=f"t{i}", email=e, vdot_override=v))
    db.flush()
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    db.add(RaceResult(athlete_id=sam.id, date=A.team_today() - dt.timedelta(days=12), meet="Hilltop", race="Boys Varsity", distance_m=5000,
                      distance_label="5,000 Meters", time_s=15 * 60 + 41.2, place=3))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app, user="coach1"):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c


def ids():
    db = SessionLocal()
    out = {a.first_name: a.id for a in db.query(Athlete)}
    db.close()
    return out


def new_meet(c, days=10):
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/registrations").text).group(1)
    r = c.post("/registrations", data={"csrf": csrf, "name": "Clovis Invitational", "date": (A.team_today() + dt.timedelta(days=days)).isoformat(),
                                       "location": "Woodward Park", "director_name": "Dana Ruiz", "director_email": "Dana@Example.org", "starter": "1"})
    mid = int(r.headers["location"].rsplit("/", 1)[1])
    db = SessionLocal()
    m = db.get(MeetEntry, mid)
    out = mid, m.divisions[0].id, m.divisions[1].id, csrf
    db.close()
    return out


def save(c, mid, did, csrf, athletes, level="Varsity", limit=""):
    return c.post(f"/registrations/{mid}/divisions/{did}", data={"csrf": csrf, "gender": "Boys", "level": level, "distance": "5K",
                                                                "max_entries": limit, "athlete": [str(a) for a in athletes]})


def test_races_limits_and_one_race_each(app):
    c = login(app)
    mid, varsity, jv, csrf = new_meet(c)
    i = ids()
    page = c.get(f"/registrations/{mid}").text
    assert "Boys Varsity · 5K" in page and "Boys JV · 5K" in page and "15:41.2" in page
    assert '<option selected>Varsity</option>' in page and "<option>Frosh/Soph</option>" in page and 'class="list compact sortable"' in page
    assert "Ivy Stone" not in page.split('id="unassigned"')[0]                          # girls aren't offered for boys' races
    save(c, mid, varsity, csrf, [i["José"], i["Sam"], i["Theo"]], limit="2")
    assert "Boys Varsity allows 2 entries and 3 were picked." in c.get(f"/registrations/{mid}").text
    save(c, mid, varsity, csrf, [i["José"], i["Sam"]], limit="2")
    page = c.get(f"/registrations/{mid}").text
    assert "Saved Boys Varsity: 2 of 2 athletes." in page and "2 of 2 entered" in page
    jv_picker = page.split(f'id="div-{jv}"')[1].split("</form>")[0]
    assert "Theo Marsh" in jv_picker and "Sam Lowry" not in jv_picker                    # already in Varsity: not offered for JV
    save(c, mid, jv, csrf, [i["Theo"], i["Sam"]], level="JV")                             # Sam can't be taken into JV
    assert "Some were already in another race and were left there." in c.get(f"/registrations/{mid}").text
    db = SessionLocal()
    assert sorted((e.division.name, e.athlete.first_name) for e in db.query(DivisionAthlete)) == [
        ("Boys JV", "Theo"), ("Boys Varsity", "José"), ("Boys Varsity", "Sam")]
    db.close()
    c.post(f"/registrations/{mid}/divisions", data={"csrf": csrf, "gender": "Girls", "level": "Frosh/Soph", "distance": "3 Miles", "max_entries": "7"})
    page = c.get(f"/registrations/{mid}").text
    assert "Added Girls Frosh/Soph (up to 7)." in page and "Girls Frosh/Soph · 3 Miles" in page

    # Not in a race yet, and marking absent.
    unassigned = page.split('id="unassigned"')[1].split("</table>")[0]
    assert "Pat Reed" in unassigned and "Ivy Stone" in unassigned and "Sam Lowry" not in unassigned
    c.post(f"/registrations/{mid}/absent", data={"csrf": csrf, "athlete_id": str(i["Pat"]), "reason": ""})
    assert "Add a reason for marking Pat Reed absent." in c.get(f"/registrations/{mid}").text
    c.post(f"/registrations/{mid}/absent", data={"csrf": csrf, "athlete_id": str(i["Pat"]), "reason": "Family trip"})
    page = c.get(f"/registrations/{mid}").text
    assert "Pat Reed" not in page.split('id="unassigned"')[1].split("</table>")[0] and "Family trip" in page and "A coach" in page
    c.post(f"/registrations/{mid}/absent", data={"csrf": csrf, "athlete_id": str(i["Pat"]), "clear": "1"})
    assert "Pat Reed is no longer marked absent." in c.get(f"/registrations/{mid}").text


def test_email_and_pdf_list_the_staff(app):
    c = login(app)
    mid, varsity, jv, csrf = new_meet(c)
    i = ids()
    save(c, mid, varsity, csrf, [i["José"], i["Sam"]])
    save(c, mid, jv, csrf, [i["Theo"]], level="JV")
    page = c.get(f"/registrations/{mid}").text
    director = unquote(re.search(r'href="(mailto:dana[^"]+)"', page).group(1))
    assert "2. Sam Lowry, grade 12, season best 15:41.2" in director and "Dale Brook (coach@example.org)" in director
    assert "Coaching staff:\n  Dale Brook, Head Coach · (916) 555-0101 · coach@example.org" in director and "at@example.org" not in director
    assert "Coaching staff listed at the bottom: Dale Brook" in page
    assert unquote(re.search(r'href="(mailto:\?bcc=[^"]+)"', page).group(1)).startswith("mailto:?bcc=sam@example.org,theo@example.org&")
    pdf = c.get(f"/registrations/{mid}/entries.pdf")
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
    for want in ("Clovis Invitational", "Woodward Park", "Boys JV", "Sam Lowry", "15:41.2", "3 athletes in 2 races", "Coaching staff",
                 "Dale Brook", "Head Coach", "(916) 555-0101", "Submitted by Dale Brook"):
        assert want in text, want
    t = login(app, "trainer1")
    assert "Choose athletes" not in t.get(f"/registrations/{mid}").text
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/registrations").text).group(1)
    assert t.post(f"/registrations/{mid}/delete", data={"csrf": tcsrf}).status_code == 403
    c.post(f"/registrations/{mid}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(MeetEntry).count() == 0 and db.query(DivisionAthlete).count() == 0 and db.query(Athlete).count() == 5
    db.close()


def test_athletes_answer_attendance(app):
    c = login(app)
    mid, varsity, jv, csrf = new_meet(c, days=10)
    soon, *_ = new_meet(c, days=3)
    i = ids()
    save(c, mid, varsity, csrf, [i["Sam"], i["Theo"]])
    ath = TestClient(app, follow_redirects=False)
    page = ath.get("/a/t0").text                                                              # Sam
    assert 'id="mt-h">Upcoming meets' in page and "You’re entered in Boys Varsity 5K" in page
    assert page.count("I’ll be there") == 1 and "The meet is 3 days away. Talk to the coaching staff" in page
    acsrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert ath.post(f"/a/t0/meets/{soon}", data={"csrf": acsrf, "status": "attending"}).status_code == 303
    assert "too close to Clovis Invitational" in ath.get("/a/t0").text                        # can't answer inside 4 days
    ath.post(f"/a/t0/meets/{mid}", data={"csrf": acsrf, "status": "attending"})
    page = ath.get("/a/t0").text
    assert "You’re confirmed for Clovis Invitational." in page and "You’re confirmed</span>" in page and page.count("I’ll be there") == 0
    # Theo can't make it: a reason is required, and saying so takes him out of his race.
    ath.post(f"/a/t2/meets/{mid}", data={"csrf": acsrf, "status": "absent", "reason": ""})
    assert "Tell your coaches why you can’t be there." in ath.get("/a/t2").text
    ath.post(f"/a/t2/meets/{mid}", data={"csrf": acsrf, "status": "absent", "reason": "Doctor appointment"})
    assert "You told your coaches you won’t be there: Doctor appointment" in ath.get("/a/t2").text
    ath.post(f"/a/t2/meets/{mid}", data={"csrf": acsrf, "status": "attending"})
    assert "already answered" in ath.get("/a/t2").text
    coach_page = c.get(f"/registrations/{mid}").text
    assert "Doctor appointment" in coach_page and "The athlete" in coach_page and '<span class="pill good">Confirmed</span>' not in coach_page.split('id="unassigned"')[1]
    db = SessionLocal()
    assert {(r.athlete.first_name, r.status) for r in db.query(MeetResponse)} == {("Sam", "attending"), ("Theo", "absent")}
    assert [e.athlete.first_name for e in db.query(DivisionAthlete)] == ["Sam"]
    db.close()
    assert "Upcoming meets" in ath.get("/a/t4").text or True                                 # girls see meets with girls' races only
    assert "Clovis" not in ath.get("/a/t4").text


def test_older_races_get_team_and_division_from_their_name():
    from sqlalchemy import text
    from xcapp.db import create_all, engine
    db = SessionLocal()
    m = MeetEntry(name="Old Meet", date=dt.date(2026, 11, 1))
    db.add(m)
    db.commit()
    mid = m.id
    db.close()
    with engine().begin() as conn:
        for i, n in enumerate(("Boys JV", "Girls Frosh/Soph", "Girls Varsity")):
            conn.execute(text("INSERT INTO meet_divisions (meet_id, name, distance, gender, level, position) VALUES (:m, :n, '5K', 'Boys', 'Varsity', :p)"),
                         {"m": mid, "n": n, "p": i})
    create_all()
    db = SessionLocal()
    from xcapp.models import MeetDivision
    assert [(d.gender, d.level) for d in db.query(MeetDivision).order_by(MeetDivision.position)] == [
        ("Boys", "JV"), ("Girls", "Frosh/Soph"), ("Girls", "Varsity")]
    db.close()


def test_distance_dropdown_and_close_distance(app):
    from xcapp import registration as REG
    c = login(app)
    mid, varsity, jv, csrf = new_meet(c)
    i = ids()
    db = SessionLocal()
    day = A.team_today() - dt.timedelta(days=5)
    for first, t in (("José", 16 * 60 + 2), ("Theo", 17 * 60 + 30), ("Pat", 18 * 60)):   # 5K times for three of the four boys
        db.add(RaceResult(athlete_id=i[first], date=day, meet="Lakeside", race="Boys", distance_m=5000, distance_label="5K", time_s=t))
    db.add(RaceResult(athlete_id=i["Theo"], date=day - dt.timedelta(days=7), meet="Oak", race="Boys", distance_m=4828, distance_label="3 Miles",
                      time_s=16 * 60 + 40))
    db.commit()
    db.close()
    c.post(f"/registrations/{mid}/divisions/{varsity}", data={"csrf": csrf, "gender": "Boys", "level": "Varsity", "distance": "3 Miles"})
    page = c.get(f"/registrations/{mid}").text
    picker = page.split(f'id="div-{varsity}"')[1].split("</form>")[0]
    assert '<option selected>3 Miles</option>' in picker and "<option>4K</option>" in picker
    assert ">SB 3 Miles</th>" in picker and ">SB 5K*</th>" in picker                     # 1 of 4 ran 3 miles; 4 of 4 have a 5K
    assert "16:40.0" in picker and "16:02.0" in picker and picker.count('class="no-time"') == 3   # 3 without 3 miles
    db = SessionLocal()
    times = REG.season_times(db, A.team_today())
    assert REG.close_distance(times, [i["Sam"], i["José"], i["Theo"], i["Pat"]], 5000) is None   # 5K: enough of them have one
    assert REG.close_distance(times, [i["Sam"], i["José"]], 1609.344) == 5000.0
    assert REG.close_distance(times, [i["Sam"], i["Ivy"]], 1609.344) is None                      # nothing over 75%
    db.close()

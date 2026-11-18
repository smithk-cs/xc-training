"""Student emails pasted in bulk, matched by last name, first initial and graduation year (made-up names and addresses)."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import student_emails as SEM
from xcapp.models import Athlete, StaffUser
from xcapp.security import hash_password

TODAY = dt.date(2026, 11, 17)     # the 2026-27 school year: seniors are the class of 2027


def roster(db):
    people = [("Lena", "Alder", 2027, 12), ("Bo", "Saint-Evens", 2028, 11), ("Wes", "Benning", None, 9),   # grade 9 → class of 2030
              ("Sam", "Grey", 2029, 10), ("Sid", "Grey", 2029, 10), ("Cal", "Dunn", 2029, 10)]
    out = {}
    for i, (first, last, year, grade) in enumerate(people):
        a = Athlete(first_name=first, last_name=last, gender="Boys", grade=grade, group="", grad_year=year, link_token=f"t{i}")
        db.add(a)
        out[first] = a
    out["Cal"].email = "cal.dunn@example.org"
    db.flush()
    return out


def test_parse_and_match(session):
    p = roster(session)
    text = """alderl27@student.example.edu
    SaintEvensB28@student.example.edu, benningw30@student.example.edu
    greys29@student.example.edu   nobodyx28@student.example.edu  coach@example.org  alderl27@student.example.edu
    dunnc29@student.example.edu"""
    emails = SEM.parse(text)
    assert emails[:3] == ["alderl27@student.example.edu", "saintevensb28@student.example.edu", "benningw30@student.example.edu"]
    assert len(emails) == 7                                                   # the repeat counted once
    rows = {r["email"].split("@")[0]: r for r in SEM.match(session, emails, TODAY)}
    assert rows["alderl27"]["athlete"] is p["Lena"] and rows["saintevensb28"]["athlete"] is p["Bo"]
    assert rows["benningw30"]["athlete"] is p["Wes"]                          # class year from the grade
    assert rows["greys29"]["status"] == "many" and {a.first_name for a in rows["greys29"]["candidates"]} == {"Sam", "Sid"}
    assert rows["nobodyx28"]["status"] == "none" and rows["coach"]["status"] == "odd"
    result = SEM.apply(list(rows.values()), replace=False)
    assert {r["athlete"].first_name for r in result["saved"]} == {"Lena", "Bo", "Wes"}
    assert [r["athlete"].first_name for r in result["kept"]] == ["Cal"] and p["Cal"].email == "cal.dunn@example.org"
    assert p["Lena"].email == "alderl27@student.example.edu"
    again = SEM.apply(SEM.match(session, emails, TODAY), replace=True)
    assert len(again["same"]) == 3 and [r["athlete"].first_name for r in again["saved"]] == ["Cal"]
    assert p["Cal"].email == "dunnc29@student.example.edu"


def test_page(session):
    roster(session)
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/athletes").text
    assert "Add student emails" in page and 'action="/athletes/emails"' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/athletes/emails", data={"csrf": csrf, "emails": "alderl27@student.example.edu\ngreys29@student.example.edu\ndunnc29@student.example.edu"})
    assert r.status_code == 200
    assert "<b>1 added</b>" in r.text and "1 not replaced" in r.text and "<b>1 not matched</b>" in r.text
    assert "More than one athlete fits" in r.text and "cal.dunn@example.org" in r.text
    assert "alderl27@student.example.edu" in c.get("/athletes/1").text
    assert "Paste at least one" in (c.post("/athletes/emails", data={"csrf": csrf, "emails": "no addresses here"}) and c.get("/athletes").text)


def test_near_matches_are_confirmed_not_guessed(session):
    """lina28 could be Andrew Lin (not on the team), not Amy Li: an athlete's page link is emailed to the address saved."""
    amy = Athlete(first_name="Amy", last_name="Li", gender="Girls", grade=11, group="", grad_year=2028, link_token="t-amy")
    jo = Athlete(first_name="Jordan", last_name="Lee", gender="Boys", grade=12, group="", grad_year=2027, link_token="t-jo")
    session.add_all([amy, jo])
    session.flush()
    rows = {r["email"].split("@")[0]: r for r in SEM.match(session, SEM.parse(
        "lina28@student.example.edu leej27@student.example.edu leesonj27@student.example.edu"), TODAY)}
    assert rows["lina28"]["status"] == "check" and rows["lina28"]["athlete"] is amy        # offered, not saved
    # Jordan's own address fits exactly, but Jamie Leeson's nearly fits him too: neither is saved
    assert rows["leej27"]["status"] == rows["leesonj27"]["status"] == "many"
    assert rows["leej27"]["clash"] == ["leesonj27@student.example.edu"]
    result = SEM.apply(list(rows.values()), replace=True)
    assert result["saved"] == [] and not amy.email and not jo.email


def test_confirming_a_near_match(session):
    amy = Athlete(first_name="Amy", last_name="Li", gender="Girls", grade=11, group="", grad_year=2028, link_token="t-amy")
    session.add(amy)
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/athletes").text).group(1)
    r = c.post("/athletes/emails", data={"csrf": csrf, "emails": "lina28@student.example.edu"})
    assert "<b>0 added</b>" in r.text and "<b>1 to confirm</b>" in r.text and "Confirm these" in r.text
    assert f'value="{amy.id}:lina28@student.example.edu"' in r.text
    session.refresh(amy)
    assert not amy.email
    c.post("/athletes/emails/confirm", data={"csrf": csrf})                                  # nothing ticked
    session.refresh(amy)
    assert not amy.email and "Nothing was saved." in c.get("/athletes").text
    c.post("/athletes/emails/confirm", data={"csrf": csrf, "pick": f"{amy.id}:lina28@student.example.edu"})
    session.refresh(amy)
    assert amy.email == "lina28@student.example.edu"

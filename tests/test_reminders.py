"""Daily reminder emails: opt-in on the athlete page, what's in them, sending through Resend once a day. Names are made up."""
import datetime as dt
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import config
from xcapp import reminders as RM
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, MeetEntry, StaffUser
from xcapp.security import hash_password


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("EMAIL_FROM", "Team XC <reminders@example.org>")
    config.reload_settings()
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", email="coach@example.org", password_hash=hash_password("correct-horse-1")))
    today = A.team_today()
    for i, (f, l, email) in enumerate((("Sam", "Lowry", "sam@example.org"), ("Theo", "Marsh", ""), ("Pat", "Reed", "pat@example.org"))):
        a = Athlete(first_name=f, last_name=l, gender="Boys", grade=11, group="Varsity", link_token=f"t{i}", email=email)
        db.add(a)
        db.flush()
        if f != "Pat":
            for k in (1, 2):
                d = today - dt.timedelta(days=k)
                db.add(Activity(athlete_id=a.id, source="upload", external_id=f"x{i}{k}", start_utc=dt.datetime(d.year, d.month, d.day, 15),
                                local_date=d, duration_s=1800, distance_m=6000, sport="running"))
    db.add(MeetEntry(name="Clovis Invitational", date=today + dt.timedelta(days=6)))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


class Resend:
    def __init__(self, status=200):
        self.sent, self.status = [], status

    def __call__(self, req):
        self.sent.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer re_test" and str(req.url) == RM.RESEND_URL
        return httpx.Response(self.status, json={"id": "em_1"} if self.status == 200 else {"message": "Domain not verified"})


def login(app):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    return c, csrf


def test_opt_in_and_daily_send(app, monkeypatch):
    ath = TestClient(app, follow_redirects=False)
    page = ath.get("/a/t0").text
    assert "Email me reminders at sam@example.org" in page and "My parent or guardian is OK with this." in page
    assert "Ask your coach to add your email address" in ath.get("/a/t1").text               # no email: no opt-in
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    ath.post("/a/t0/reminders", data={"csrf": csrf, "on": "1"})
    ath.post("/a/t2/reminders", data={"csrf": csrf, "on": "1"})
    assert "You’ll get a reminder email at sam@example.org" in ath.get("/a/t0").text
    assert "Resend, Inc." in ath.get("/privacy").text

    db = SessionLocal()
    got = {a.first_name: lines for a, lines in RM.recipients(db, A.team_today())}
    assert list(got) == ["Sam", "Pat"]                                                       # Theo has no email
    assert got["Sam"][0] == "Rate how hard 2 runs felt (effort 0–10)."
    assert any(x.startswith("Tell your coaches whether you’ll be at Clovis Invitational") for x in got["Pat"])
    mock = Resend()
    http = httpx.Client(transport=httpx.MockTransport(mock))
    assert RM.run(db, A.team_today(), http=http) == {"sent": 2, "errors": 0}
    assert RM.run(db, A.team_today(), http=http) == {"skipped": "already sent today"}
    db.commit()
    db.close()
    sam = next(m for m in mock.sent if m["to"] == ["sam@example.org"])
    assert sam["from"] == "Team XC <reminders@example.org>" and sam["subject"].endswith("2 things to finish")
    assert "https://xc.test/a/t0" in sam["text"] and "Hi Sam," in sam["html"]

    c, _ = login(app)
    page = c.get("/reminders").text
    ccsrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert "Opted in" in page and "last sent " + A.team_today().isoformat() in page
    c.post("/reminders", data={"csrf": ccsrf, "enabled": "1", "item_effort": "1"})
    db = SessionLocal()
    assert RM.enabled(db) and RM.chosen_items(db) == ["effort"]
    db.close()


def test_errors_and_not_set_up(app, monkeypatch):
    db = SessionLocal()
    for a in db.query(Athlete):
        a.email_reminders = True
    db.commit()
    http = httpx.Client(transport=httpx.MockTransport(Resend(status=403)))
    assert RM.run(db, A.team_today(), http=http) == {"sent": 0, "errors": 2}
    db.commit()
    db.close()
    c, csrf = login(app)
    assert "Domain not verified" in c.get("/sync").text
    monkeypatch.setenv("RESEND_API_KEY", "")
    config.reload_settings()
    with pytest.raises(RM.EmailError):
        RM.send_email("x@example.org", "s", "t", "h")
    from xcapp.web import create_app
    c2, _ = login(create_app())                                                               # settings are read when the app starts
    assert "Email isn’t set up yet." in c2.get("/reminders").text and "Resend, Inc." not in c2.get("/privacy").text

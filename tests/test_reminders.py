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
    assert RM.run(db, A.team_today(), http=http) == {"sent": 2, "errors": 0, "pushed": 0}
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
    assert RM.run(db, A.team_today(), http=http) == {"sent": 0, "errors": 2, "pushed": 0}
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


def test_reply_to(app):
    c, csrf = login(app)
    page = c.get("/reminders").text
    assert "Replies go to" in page and "No reply address yet" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/reminders", data={"csrf": csrf, "action": "reply", "reply_to": "not an address"})
    assert "doesn’t look like an email address" in c.get("/reminders").text
    c.post("/reminders", data={"csrf": csrf, "action": "reply", "reply_to": "coach@school.example.org"})
    page = c.get("/reminders").text
    assert 'value="coach@school.example.org"' in page and "Replies to the app’s emails now go to coach@school.example.org" in page
    db = SessionLocal()
    for a in db.query(Athlete).filter(Athlete.email != "").all():
        a.email_reminders = True
    mock = Resend()
    RM.run(db, A.team_today(), http=httpx.Client(transport=httpx.MockTransport(mock)))
    db.commit()
    assert mock.sent and all(m["reply_to"] == ["coach@school.example.org"] for m in mock.sent)
    db.close()
    mock2 = Resend()
    RM.send_email("x@example.org", "s", "t", "<p>h</p>", http=httpx.Client(transport=httpx.MockTransport(mock2)))
    assert "reply_to" not in mock2.sent[0]                                           # none given: none sent


def test_email_athletes_their_links(app, monkeypatch):
    sent = []
    monkeypatch.setattr(RM, "send_email", lambda to, subj, text, html, http=None, reply="": sent.append((to, subj, text, html, reply)) or "id")
    db = SessionLocal()
    sam = db.query(Athlete).filter_by(first_name="Sam").one()
    subject, text, html = RM.compose_link(sam)
    assert subject.endswith("your training page") and "https://xc.test/a/t0" in text and "Connect Garmin" in text
    assert "Add to Home Screen" in html and "please don’t share it" in html and "Hi Sam," in html
    db.close()
    c, _ = login(app)
    page = c.get("/athletes/1").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert "Email this link to sam@example.org" in page and "Not emailed yet." in page
    c.post("/reminders", data={"csrf": csrf, "action": "reply", "reply_to": "coach@school.example.org"})
    c.post("/athletes/1/email-link", data={"csrf": csrf})
    assert sent[-1][0] == "sam@example.org" and sent[-1][4] == "coach@school.example.org"
    assert "Emailed Sam Lowry their page link" in c.get("/athletes/1").text and "Last emailed" in c.get("/athletes/1").text
    assert "Add an email address above" in c.get("/athletes/2").text                    # Theo has none
    roster = c.get("/athletes").text
    assert "Email links to those not sent yet (1)" in roster and "Email everyone’s link again (2)" in roster
    c.post("/athletes/email-links", data={"csrf": csrf, "which": "unsent"})             # Pat only: Sam already has his
    assert [s[0] for s in sent] == ["sam@example.org", "pat@example.org"]
    c.post("/athletes/email-links", data={"csrf": csrf, "which": "unsent"})
    assert "already been sent their link" in c.get("/athletes").text and len(sent) == 2
    c.post("/athletes/email-links", data={"csrf": csrf, "which": "all"})
    assert len(sent) == 4
    c.post("/athletes/1/new-link", data={"csrf": csrf})                                  # a new link: not sent yet
    db = SessionLocal()
    sam = db.get(Athlete, 1)
    assert sam.link_sent_at is None and sam.link_token != "t0"
    db.close()
    assert "Email links to those not sent yet (1)" in c.get("/athletes").text


def test_link_email_needs_email_set_up(app, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    config.reload_settings()
    from xcapp.web import create_app
    c, _ = login(create_app())                                                           # an app started without the key
    page = c.get("/athletes/1").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert 'title="Email isn’t set up yet"' in page
    c.post("/athletes/1/email-link", data={"csrf": csrf})
    assert "Email isn’t set up yet" in c.get("/athletes/1").text

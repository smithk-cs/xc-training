"""The weekly newsletter (docs/plans/newsletter.md, privacy.md): drafted from data, approved by a coach, sent by hand,
with consent applied to every name and photo."""
import datetime as dt
import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import newsletter as NL
from xcapp import newsletter_web as NW
from xcapp.db import SessionLocal
from xcapp.models import (Athlete, MeetEntry, NewsletterIssue, NewsletterPhoto, NewsletterSend, PlannedWorkout, RaceResult,
                          RaceTeamResult, RecoveryTip, Spotlight, StaffUser, SyncLog)
from xcapp.security import hash_password

MON = A.week_monday(A.team_today()) - dt.timedelta(days=7)   # last week: the week the issue reports on


@pytest.fixture
def team():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", email="coach1@example.org", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    ok = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t1",
                 show_name=True, show_photo=True, show_spotlight=True)
    shy = Athlete(first_name="Pat", last_name="Reed", gender="Boys", grade=10, group="Varsity", link_token="t2")   # no consent
    db.add_all([ok, shy])
    db.flush()
    sat = MON + dt.timedelta(days=5)
    for a, t, place, before in ((ok, 16 * 60 + 30, 4, 16 * 60 + 50), (shy, 17 * 60, 9, 17 * 60 + 20)):
        db.add(RaceResult(athlete_id=a.id, date=sat - dt.timedelta(days=21), meet="Early Meet", race="Boys Varsity", distance_m=5000,
                          distance_label="5K", time_s=before, place=place + 2))
        db.add(RaceResult(athlete_id=a.id, date=sat, meet="Hilltop Classic", race="Boys Varsity", distance_m=5000,
                          distance_label="5K", time_s=t, place=place, course="Hilltop Park"))
    db.add(RaceTeamResult(meet="Hilltop Classic", date=sat, race="Boys Varsity", place=2, teams=14, points=61))
    db.add(MeetEntry(name="League Finals", date=MON + dt.timedelta(days=12), location="Ancil Hoffman"))
    db.add(PlannedWorkout(date=MON + dt.timedelta(days=8), title="6 x 1K", rep_distance_m=1000, pace_event="5K", pace_factor=1.0, groups="All"))
    db.commit()
    ids = ok.id, shy.id
    db.close()
    from xcapp.web import create_app
    return create_app(), ids


def login(app, who="coach1"):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": who, "password": "correct-horse-1"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/newsletter").text).group(1)


def new_issue(c, csrf):
    r = c.post("/newsletter/new", data={"csrf": csrf, "week": MON.isoformat()})
    return int(r.headers["location"].rsplit("/", 1)[-1])


def test_draft_is_built_from_the_week_and_consent_hides_names(team):
    app, (ok, shy) = team
    c, csrf = login(app)
    iid = new_issue(c, csrf)
    view = c.get(f"/newsletter/{iid}/view").text
    assert "Race recap" in view and "Team 2nd of 14 · 61 points" in view and "Sam Lowry" in view
    assert "Personal records" in view and "16:30.0" in view and "League Finals" in view and "6 x 1K" in view
    assert "Pat Reed" not in view                                                     # no name consent: never shown
    edit = c.get(f"/newsletter/{iid}").text
    assert "Left out for now (no name consent): Pat Reed" in edit                     # the coach is told why
    for out in (c.get(f"/newsletter/{iid}/email").text, c.get(f"/newsletter/{iid}/issue.pdf").content.decode("latin-1")):
        assert "Pat Reed" not in out
    db = SessionLocal()
    team_numbers = [s for s in db.get(NewsletterIssue, iid).sections if s.key == "team_numbers"][0].data_snapshot
    assert "Lowry" not in team_numbers and "Reed" not in team_numbers                 # team totals carry no names
    db.close()


def test_week_without_a_race_skips_recap_prs_and_course(team):
    app, _ = team
    db = SessionLocal()
    issue = NL.create_issue(db, MON - dt.timedelta(days=28), "coach1")
    keys = [s["key"] for s in NL.rendered(db, issue)]
    db.close()
    assert "race_recap" not in keys and "prs" not in keys and "course_snapshot" not in keys


def test_editing_an_approved_issue_returns_it_to_draft_and_refresh_keeps_text(team):
    app, _ = team
    c, csrf = login(app)
    iid = new_issue(c, csrf)
    c.post(f"/newsletter/{iid}/save", data={"csrf": csrf, "enabled_coach_note": "1", "text_coach_note": "Great week, team."})
    c.post(f"/newsletter/{iid}/status", data={"csrf": csrf, "action": "approved"})
    db = SessionLocal()
    i = db.get(NewsletterIssue, iid)
    assert i.status == "approved" and i.approved_by == "coach1"
    db.close()
    c.post(f"/newsletter/{iid}/save", data={"csrf": csrf, "enabled_coach_note": "1", "text_coach_note": "Great week, everyone."})
    db = SessionLocal()
    i = db.get(NewsletterIssue, iid)
    assert i.status == "draft" and i.approved_by == "" and i.approved_at is None
    db.close()
    c.post(f"/newsletter/{iid}/refresh", data={"csrf": csrf})
    assert "Great week, everyone." in c.get(f"/newsletter/{iid}/view").text


def test_snapshot_does_not_change_when_a_result_is_corrected(team):
    app, (ok, _) = team
    c, csrf = login(app)
    iid = new_issue(c, csrf)
    c.post(f"/newsletter/{iid}/status", data={"csrf": csrf, "action": "approved"})
    db = SessionLocal()
    r = db.query(RaceResult).filter_by(athlete_id=ok, meet="Hilltop Classic").one()
    r.time_s = 15 * 60
    db.commit()
    db.close()
    view = c.get(f"/newsletter/{iid}/view").text
    assert "16:30.0" in view and "15:00.0" not in view


def test_sending_needs_approval_and_a_coach_and_is_logged(team, monkeypatch):
    app, (ok, _) = team
    sent = []
    monkeypatch.setattr(NW, "send_email", lambda to, subj, text, html: sent.append((to, subj, html)) or "id")
    c, csrf = login(app)
    iid = new_issue(c, csrf)
    c.post(f"/newsletter/{iid}/save", data={"csrf": csrf, "enabled_spotlight": "1", "spotlight_athlete": str(ok),
                                            "text_spotlight": "Sam leads every warm-up."})
    c.post(f"/newsletter/{iid}/send", data={"csrf": csrf})
    assert not sent and "Approve the issue before sending it." in c.get(f"/newsletter/{iid}").text
    t, tcsrf = login(app, "trainer1")
    assert t.post(f"/newsletter/{iid}/status", data={"csrf": tcsrf, "action": "approved"}).status_code == 403
    c.post(f"/newsletter/{iid}/status", data={"csrf": csrf, "action": "approved"})
    assert t.post(f"/newsletter/{iid}/send", data={"csrf": tcsrf}).status_code == 403
    c.post(f"/newsletter/{iid}/send", data={"csrf": csrf})
    assert [x[0] for x in sent] == ["coach1@example.org"] and "Sam leads every warm-up." in sent[0][2]
    db = SessionLocal()
    i = db.get(NewsletterIssue, iid)
    assert i.status == "sent" and db.query(NewsletterSend).one().recipients == 1
    assert db.query(Spotlight).one().athlete_id == ok                                  # so the next turn goes to someone else
    assert any("sent the issue" in l.message for l in db.query(SyncLog).filter_by(kind="newsletter").all())
    db.close()
    c.post(f"/newsletter/{iid}/save", data={"csrf": csrf, "text_coach_note": "changed"})
    assert "can’t change" in c.get(f"/newsletter/{iid}").text                        # sent issues are read-only


def test_email_has_no_style_sheets_and_stays_600_wide(team):
    app, _ = team
    c, csrf = login(app)
    iid = new_issue(c, csrf)
    html = c.get(f"/newsletter/{iid}/email").text
    assert "<style" not in html and "<link" not in html and 'width="600"' in html
    widths = [int(w) for w in re.findall(r'width="(\d+)"', html)]
    assert max(widths) <= 600


def test_photos_need_every_tagged_athletes_consent_and_lose_location(team):
    from PIL import Image
    app, (ok, shy) = team
    c, csrf = login(app)
    iid = new_issue(c, csrf)
    img = Image.new("RGB", (1600, 900), "red")
    exif = Image.Exif()
    exif[0x010F] = "Garmin"                          # camera make
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (38.0, 35.0, 0.0)           # GPS latitude
    raw = io.BytesIO()
    img.save(raw, "JPEG", exif=exif)
    assert b"Exif" in raw.getvalue()
    for who in ([ok], [ok, shy]):
        c.post(f"/newsletter/{iid}/photos", data={"csrf": csrf, "alt": "Start line", "athletes": [str(x) for x in who]},
               files={"photo": ("a.jpg", raw.getvalue(), "image/jpeg")})
    db = SessionLocal()
    photos = db.query(NewsletterPhoto).order_by(NewsletterPhoto.id).all()
    assert b"Exif" not in photos[0].data and Image.open(io.BytesIO(photos[0].data)).size == (1200, 675)
    p1, p2 = photos[0].id, photos[1].id
    db.close()
    view = c.get(f"/newsletter/{iid}/view").text
    assert f"/photos/{p1}.jpg" in view and f"/photos/{p2}.jpg" not in view            # one athlete without consent blocks it
    from xcapp.config import settings
    anon = TestClient(app)
    assert anon.get(f"/newsletter/photo/{p1}/{NL.photo_token(settings().secret_key, p1)}.jpg").status_code == 200
    assert anon.get(f"/newsletter/photo/{p2}/{NL.photo_token(settings().secret_key, p2)}.jpg").status_code == 404
    assert anon.get(f"/newsletter/photo/{p1}/{'0' * 32}.jpg").status_code == 404
    r = c.post(f"/newsletter/{iid}/photos", data={"csrf": csrf, "alt": ""}, files={"photo": ("a.jpg", raw.getvalue(), "image/jpeg")})
    assert "Describe the photo" in c.get(f"/newsletter/{iid}").text                   # alt text required


def test_blocked_topics_never_get_in(team):
    app, _ = team
    assert NL.blocked_words("Weight room on Tuesday") == []                           # strength training is fine
    assert NL.blocked_words("Stay hydrated and skip the diet soda") == ["diet"]
    assert NL.blocked_words("lose weight fast") == ["lose weight"]
    c, csrf = login(app)
    c.post("/newsletter-tips", data={"csrf": csrf, "text": "Count calories after runs"})
    db = SessionLocal()
    assert db.query(RecoveryTip).count() == 0
    db.close()
    iid = new_issue(c, csrf)
    c.post(f"/newsletter/{iid}/save", data={"csrf": csrf, "enabled_coach_note": "1", "text_coach_note": "Watch your body weight"})
    c.post(f"/newsletter/{iid}/status", data={"csrf": csrf, "action": "approved"})
    db = SessionLocal()
    assert db.get(NewsletterIssue, iid).status == "draft"
    db.close()


def test_tips_need_approval_and_rotate(team):
    app, _ = team
    c, csrf = login(app)
    c.post("/newsletter-tips", data={"csrf": csrf, "starter": "1"})
    db = SessionLocal()
    assert db.query(RecoveryTip).count() == len(NW.STARTER_TIPS) and NL.pick_tip(db) is None
    assert all(not NL.blocked_words(t) for _, t in NW.STARTER_TIPS)
    a, b = db.query(RecoveryTip).order_by(RecoveryTip.id).limit(2).all()
    a.approved_by, b.approved_by, a.last_used = "coach1", "trainer1", A.team_today()
    db.commit()
    assert NL.pick_tip(db).id == b.id                                                 # least recently used first
    db.close()


def test_consent_page_records_changes_and_the_school_release(team):
    app, (ok, shy) = team
    c, csrf = login(app)
    c.post("/newsletter-consent", data={"csrf": csrf, "name_%d" % ok: "1", "name_%d" % shy: "1"})
    db = SessionLocal()
    s, p = db.get(Athlete, ok), db.get(Athlete, shy)
    assert s.show_name and not s.show_photo and p.show_name and p.consent_by == "coach1"
    db.close()
    c.post("/newsletter-consent", data={"csrf": csrf, "everyone": "release", "basis": "school media release"})
    db = SessionLocal()
    p = db.get(Athlete, shy)
    assert (p.show_name, p.show_photo, p.show_spotlight, p.show_vdot) == (True, True, True, False)
    assert "school media release" in p.consent_by
    assert db.query(SyncLog).filter_by(kind="consent").count() == 2
    db.close()
    t, tcsrf = login(app, "trainer1")
    assert t.post("/newsletter-consent", data={"csrf": tcsrf, "everyone": "release"}).status_code == 403


def test_spotlight_suggests_whose_turn_it_is(team):
    app, (ok, shy) = team
    db = SessionLocal()
    db.get(Athlete, shy).show_spotlight = db.get(Athlete, shy).show_name = True
    issue = NL.create_issue(db, MON - dt.timedelta(days=14), "coach1")
    db.add(Spotlight(athlete_id=shy, issue_id=issue.id))
    db.commit()
    assert NL.suggest_spotlight(db).id == ok          # Pat had a turn; Sam is next
    db.close()


def test_recap_suggests_the_latest_race_and_coaches_pick_races(team):
    app, (ok, shy) = team
    c, csrf = login(app)
    # The week after the race: no race that week, so the recap suggests the latest one in the two weeks before.
    r = c.post("/newsletter/new", data={"csrf": csrf, "week": (MON + dt.timedelta(days=7)).isoformat()})
    iid = int(r.headers["location"].rsplit("/", 1)[-1])
    view = c.get(f"/newsletter/{iid}/view").text
    assert "Hilltop Classic" in view and "Sam Lowry" in view and ">PR<" in view
    assert "2 PRs" in view and "Pat Reed" not in view                                # counted for the team, not named (no consent)
    edit = c.get(f"/newsletter/{iid}").text
    assert 'value="' + (MON + dt.timedelta(days=5)).isoformat() + '|Hilltop Classic" checked' in edit
    assert 'value="' + (MON - dt.timedelta(days=16)).isoformat() + '|Early Meet">' in edit   # offered, not ticked
    early = (MON - dt.timedelta(days=16)).isoformat() + "|Early Meet"
    included = {f"enabled_{s.key}": "1" for s in NL.SECTIONS}                       # as the form sends them
    c.post(f"/newsletter/{iid}/save", data={"csrf": csrf, **included, "recap_form": "1", "recap_meet": [early]})
    view = c.get(f"/newsletter/{iid}/view").text
    assert "Early Meet" in view and "Hilltop Classic" not in view.split("Race recap")[1].split("Personal records")[0]


def test_captains_write_from_their_page_and_coaches_choose(team):
    app, (ok, shy) = team
    c, csrf = login(app)
    me = TestClient(app)
    assert "Captains’ corner" not in me.get("/a/t1").text
    assert me.post("/a/t1/captain-post", data={"csrf": "x", "text": "hi"}).status_code in (400, 404)
    c.post(f"/athletes/{ok}/captain", data={"csrf": csrf, "captain": "1"})
    page = me.get("/a/t1").text
    assert "Captains’ corner" in page
    mcsrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    me.post("/a/t1/captain-post", data={"csrf": mcsrf, "text": "Big thanks to the parents who ran the water stop!"})
    assert "never mentions body weight or dieting" in me.post("/a/t1/captain-post", data={"csrf": mcsrf, "text": "Watch your diet"}).text
    week = NL.default_week(A.team_today())
    iid = int(c.post("/newsletter/new", data={"csrf": csrf, "week": week.isoformat()}).headers["location"].rsplit("/", 1)[-1])
    edit = c.get(f"/newsletter/{iid}").text
    assert "Big thanks to the parents who ran the water stop!" in edit and "From Sam Lowry" in edit
    from xcapp.models import CaptainPost
    db = SessionLocal()
    pid = db.query(CaptainPost).one().id
    db.close()
    c.post(f"/newsletter/{iid}/captain/{pid}", data={"csrf": csrf, "action": "use"})
    view = c.get(f"/newsletter/{iid}/view").text
    assert "Big thanks to the parents who ran the water stop! (from Sam Lowry)" in view
    assert "in the newsletter" in me.get("/a/t1").text
    assert "captain_notes" in me.get("/a/t1/my-data.json").text


def test_schedule_milestones_and_team_totals(team):
    from xcapp.models import Activity
    app, (ok, shy) = team
    db = SessionLocal()
    s0, _ = A.season_range(MON)
    db.add(Activity(athlete_id=ok, source="upload", external_id="old", start_utc=dt.datetime(s0.year, s0.month, s0.day, 15),
                    local_date=s0, duration_s=3600 * 10, distance_m=95 * 1609.344, sport="running"))
    db.add(Activity(athlete_id=ok, source="upload", external_id="new", start_utc=dt.datetime(MON.year, MON.month, MON.day, 15),
                    local_date=MON, duration_s=3600, distance_m=8 * 1609.344, sport="running"))
    db.add(MeetEntry(name="State Meet", date=MON + dt.timedelta(days=26), location="Woodward Park"))
    db.commit()
    issue = NL.create_issue(db, MON, "coach1")
    secs = {s["key"]: s for s in NL.rendered(db, issue)}
    assert secs["milestones"]["data"]["milestones"] == [{"athlete_id": ok, "name": "Sam Lowry", "miles": 100}]
    assert [m["name"] for m in secs["schedule"]["data"]["meets"]] == ["League Finals", "State Meet"]
    assert secs["team_numbers"]["data"]["runs"] == 1
    db.close()

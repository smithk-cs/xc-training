"""Fixes from a security review, each tested the way the review showed the problem: a new athlete link cuts off phones
and Garmin sign-ins made with the old one; Garmin's callback can't be replayed; zip bombs; sections turned off for a
staff member hide their data everywhere; and secret links stay out of the access log. Names and addresses are made up."""
import datetime as dt
import io
import json
import logging
import re
import struct
import zipfile
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from test_push import Phone          # the browser side of a push subscription: decrypts what arrives
from xcapp import fitfile as FF
from xcapp import push as PU
from xcapp import sections as SEC
from xcapp.db import SessionLocal
from xcapp.models import Athlete, Injury, OAuthState, PainReport, PushSubscription, StaffUser
from xcapp.security import RedactSecrets, hash_password, redact_path


def mkapp(hidden=""):
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1"), hidden_sections=hidden))
    a = Athlete(first_name="Ava", last_name="Lowry", gender="Girls", grade=11, group="Varsity", link_token="old-token-ava")
    db.add(a)
    db.flush()
    db.add(Injury(athlete_id=a.id, onset_date=dt.date(2026, 11, 1), injury="Tibial bone stress injury", body_area="Lower leg", logged_by="t"))
    db.add(PainReport(athlete_id=a.id, date=dt.date.today(), body_area="Knee", severity=8, note="hurts on stairs"))
    db.commit()
    aid = a.id
    db.close()
    from xcapp.web import create_app
    return create_app(), aid


def login(c):
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})


def coach_csrf(coach, aid):
    return re.search(r'name="csrf" value="([^"]+)"', coach.get(f"/athletes/{aid}").text).group(1)


def new_link(app, aid):
    coach = TestClient(app, follow_redirects=False)
    login(coach)
    csrf = coach_csrf(coach, aid)
    coach.post(f"/athletes/{aid}/new-link", data={"csrf": csrf})
    db = SessionLocal()
    token = db.get(Athlete, aid).link_token
    db.close()
    return coach, csrf, token


def turn_on_phone(client, token, endpoint):
    csrf = re.search(r'data-csrf="([^"]+)"', client.get(f"/a/{token}").text).group(1)
    phone = Phone()
    r = client.post(f"/a/{token}/push", data={"csrf": csrf, "endpoint": endpoint, "p256dh": phone.p256dh, "auth": PU.b64u(phone.auth)})
    assert r.json() == {"ok": True}
    return phone


# ---------- 1. a new link cuts off phones that turned notifications on with the old one ----------

def test_new_link_turns_off_notifications_made_with_the_old_link(monkeypatch):
    app, aid = mkapp()
    attacker = TestClient(app, follow_redirects=False)       # someone holding a leaked link
    phone = turn_on_phone(attacker, "old-token-ava", "https://fcm.googleapis.com/fcm/send/attacker")
    coach, csrf, new = new_link(app, aid)
    assert attacker.get("/a/old-token-ava").status_code == 404
    page = coach.get(f"/athletes/{aid}").text
    assert "Phone notifications are off on their phone until Ava turns them on again from the new link." in page
    db = SessionLocal()
    assert db.query(PushSubscription).count() == 0
    db.close()

    sent = []

    def service(req):
        sent.append((str(req.url), json.loads(phone.decrypt(req.content)) if req.url.path.endswith("attacker") else None))
        return httpx.Response(201)
    real = httpx.Client
    monkeypatch.setattr(PU.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(service)))
    coach.post("/reminders/notify", data={"csrf": csrf, "message": "Practice moved to 3:30", "team": "All"})
    assert sent == []                                         # the attacker's phone gets nothing, so never the new link

    # Ava turns notifications on again from her new link, and hears from her coaches as before.
    turn_on_phone(TestClient(app, follow_redirects=False), new, "https://fcm.googleapis.com/fcm/send/ava")
    coach.post("/reminders/notify", data={"csrf": csrf, "message": "Practice moved to 3:30", "team": "All"})
    assert [u for u, _ in sent] == ["https://fcm.googleapis.com/fcm/send/ava"]


def test_new_link_cancels_garmin_sign_ins_started_with_the_old_link():
    app, aid = mkapp()
    attacker = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', attacker.get("/a/old-token-ava").text).group(1)
    attacker.post("/a/old-token-ava/connect", data={"csrf": csrf, "consent": "1"})
    new_link(app, aid)
    db = SessionLocal()
    assert db.query(OAuthState).count() == 0
    db.close()


# ---------- 2. /garmin/callback can't be used as a lasting alias for the athlete's link ----------

def start_garmin(client, token):
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get(f"/a/{token}").text).group(1)
    r = client.post(f"/a/{token}/connect", data={"csrf": csrf, "consent": "1"})
    url = re.search(r'href="([^"]*state=[^"]+)"', r.text).group(1).replace("&amp;", "&")
    return parse_qs(urlparse(url).query)["state"][0]


def test_garmin_callback_after_a_new_link_reveals_nothing():
    app, aid = mkapp()
    attacker = TestClient(app, follow_redirects=False)
    state = start_garmin(attacker, "old-token-ava")
    _, _, new = new_link(app, aid)
    assert attacker.get("/a/old-token-ava").status_code == 404
    fresh = TestClient(app, follow_redirects=False)          # no cookies at all
    for _ in range(2):
        r = fresh.get(f"/garmin/callback?state={state}&error=access_denied")
        assert r.status_code == 400 and new not in r.text and new not in r.headers.get("location", "")
    # The attacker's own browser opened the old link, which no longer works: it isn't sent there (or anywhere) either.
    r = attacker.get(f"/garmin/callback?state={state}&error=access_denied")
    assert r.status_code == 400 and "location" not in r.headers and new not in r.text


def test_cancelled_garmin_sign_in_is_used_up():
    app, aid = mkapp()
    state = start_garmin(TestClient(app, follow_redirects=False), "old-token-ava")
    fresh = TestClient(app, follow_redirects=False)
    r = fresh.get(f"/garmin/callback?state={state}&error=access_denied")
    assert r.status_code == 303 and r.headers["location"] == "/a/old-token-ava"     # the athlete sees why, once
    db = SessionLocal()
    assert db.get(OAuthState, state) is None
    db.close()
    r = TestClient(app, follow_redirects=False).get(f"/garmin/callback?state={state}&error=access_denied")
    assert r.status_code == 400 and "old-token-ava" not in r.text and "location" not in r.headers


@pytest.mark.parametrize("query", ["error=access_denied", "code=abc", ""])
def test_expired_garmin_sign_in_reveals_nothing(query):
    app, aid = mkapp()
    state = start_garmin(TestClient(app, follow_redirects=False), "old-token-ava")
    db = SessionLocal()
    db.get(OAuthState, state).created_at -= dt.timedelta(minutes=16)
    db.commit()
    r = TestClient(app, follow_redirects=False).get(f"/garmin/callback?state={state}&{query}")
    assert r.status_code == 400 and "old-token-ava" not in r.text and "location" not in r.headers
    db.expire_all()
    assert db.get(OAuthState, state) is None
    db.close()


# ---------- 3. zip bombs ----------

def zipped(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(scope="module")
def chunk():
    return b"\0" * FF.MAX_FIT_BYTES          # exactly the per-file limit, so each one alone is accepted


def test_zip_bomb_is_refused(chunk):
    n = FF.MAX_UNPACKED_BYTES // FF.MAX_FIT_BYTES + 1
    bomb = zipped([(f"run{i}.fit", chunk) for i in range(n)])
    assert len(bomb) < 1024 * 1024                           # a small upload that unpacks to over the limit
    with pytest.raises(FF.FitError, match=r"That upload unpacks to more than 100 MB\. Upload it in smaller parts\."):
        FF.fit_files("export.zip", bomb)
    # Zips inside zips count together, the inner zips' own bytes too.
    inner = zipped([("a.fit", chunk), ("b.fit", chunk)])
    with pytest.raises(FF.FitError, match="unpacks to more than"):
        FF.fit_files("export.zip", zipped([("part1.zip", inner), ("part2.zip", inner)]))
    # Under the limit is fine.
    assert len(FF.fit_files("export.zip", zipped([("a.fit", chunk), ("b.fit", chunk)]))) == 2


def test_zip_with_false_sizes_is_refused(chunk):
    """A zip can state a smaller size than it unpacks to; what's actually read is counted and checked."""
    data = bytearray(zipped([("run.fit", chunk)]))
    for sig, at in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):   # local header and central directory: uncompressed size
        i = data.index(sig)
        data[i + at:i + at + 4] = struct.pack("<I", 1000)
    with pytest.raises(FF.FitError, match="is not a readable .zip file"):
        FF.fit_files("liar.zip", bytes(data))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        assert zf.infolist()[0].file_size == 1000


def test_too_many_files_in_a_zip():
    assert FF.MAX_FITS_PER_UPLOAD <= 1000
    with pytest.raises(FF.FitError, match=f"more than {FF.MAX_FITS_PER_UPLOAD:,} activity files"):
        FF.fit_files("many.zip", zipped([(f"{i}.fit", b"x") for i in range(FF.MAX_FITS_PER_UPLOAD + 1)]))


def test_zip_bomb_upload_gets_a_clear_message(chunk, monkeypatch):
    """The upload page reads one file at a time, to a larger total (a whole Garmin export); past it, a clear message.
    The limit is lowered here so the test needn't unpack gigabytes."""
    from xcapp import web
    monkeypatch.setattr(web, "MAX_STREAM_UNPACKED_BYTES", 100 * 1024 * 1024)
    app, aid = mkapp()
    c = TestClient(app, follow_redirects=False)
    half = zipped([(f"run{i}.fit", chunk) for i in range(3)])   # 75 MB each: two together are over the limit
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/a/old-token-ava").text).group(1)
    r = c.post("/a/old-token-ava/upload", data={"csrf": csrf}, files=[("file", ("a.zip", half)), ("file", ("b.zip", half))])
    assert r.status_code == 303
    msgs = re.findall(r'<div class="flash (\w+)"[^>]*>([^<]*)', c.get("/a/old-token-ava").text)
    assert msgs == [("error", "That upload unpacks to more than 100 MB. Upload it in smaller parts.")]


# ---------- 4. a section turned off hides its data everywhere ----------

def test_dataset_csv_follows_hidden_sections():
    from xcapp.datasets import COLUMNS
    assert set(SEC.DATASETS) == set(COLUMNS) and set(SEC.DATASETS.values()) <= SEC.KEYS
    app, aid = mkapp(hidden="dashboard,athletes,injuries,charts,ai,reports,week,goals,workouts")
    c = TestClient(app, follow_redirects=False)
    login(c)
    assert c.get("/injuries").status_code == 403 and c.get(f"/athletes/{aid}").status_code == 403
    for name in COLUMNS:
        r = c.get(f"/api/datasets/{name}.csv")
        assert r.status_code == 403 and "Ava Lowry" not in r.text, name
        assert "is turned off for your account" in r.text


def test_injuries_off_hides_injury_data_but_not_the_rest():
    app, aid = mkapp(hidden="injuries")
    c = TestClient(app, follow_redirects=False)
    login(c)
    for name in ("injuries", "pain_reports"):
        assert c.get(f"/api/datasets/{name}.csv").status_code == 403
    r = c.get("/api/datasets/roster.csv")
    assert r.status_code == 200 and "Ava Lowry" in r.text
    load = c.get("/api/datasets/weekly_load.csv").text.splitlines()
    assert load[0].endswith(",injured,srpe") and all(line.split(",")[-2] == "" for line in load[1:])
    home = c.get("/").text
    for secret in ("Tibial", "Pain 8/10", "Knee", "injured now"):
        assert secret not in home, secret


def test_injuries_on_shows_the_watch_list_reasons():
    app, aid = mkapp()
    c = TestClient(app, follow_redirects=False)
    login(c)
    home = c.get("/").text
    assert "Injured: Tibial bone stress injury" in home and "Pain 8/10: Knee" in home and "injured now" in home
    assert c.get("/api/datasets/injuries.csv").status_code == 200


# Staff pages that belong to no menu section, and why. Anything else a staff member can open must belong to a section
# (sections.SECTIONS), so turning that section off for someone closes it.
NO_SECTION = {
    "/": "the home page: what it shows from a section is checked against user.sees() in the page itself",
    "/api/datasets/{name}.csv": "one address for several sections: the route checks sections.DATASETS for each dataset",
    "/staff": "staff accounts (GET and POST): admins only, and admins always have every section",
    "/staff/sections": "admins only",
    "/staff/archive": "admins only",
    "/staff/{uid}/password": "a staff account's own settings (the route checks it's theirs, or an admin)",
    "/staff/{uid}/details": "a staff account's own settings (the route checks it's theirs, or an admin)",
    "/staff/{uid}/email": "a staff account's own settings (the route checks it's theirs, or an admin)",
    "/staff/{uid}/delete": "admins only",
    "/branding/logo": "admins only: uploads the team logo",
    "/branding/logo/delete": "admins only",
    "/reminders": "reminder emails and phone notifications, coaches and admins only: a page of its own, not in the menu",
    "/reminders/notify": "sends a phone notification the coach writes (coaches and admins); shows no athlete data",
    "/weather/update": "fetches meet forecasts now rather than tonight (coaches and admins); shows no athlete data",
    "/announcements": "posts an announcement from the home page (coaches and admins)",
}


def test_every_staff_page_belongs_to_a_section():
    app, _ = mkapp()

    def staff_only(dependant):
        return any(getattr(d.call, "staff_only", False) or staff_only(d) for d in dependant.dependencies)

    loose = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and staff_only(route.dependant) and SEC.section_for(route.path) is None:
            loose.add(route.path)
    assert loose - set(NO_SECTION) == set(), "Add these to a section in sections.py, or to NO_SECTION with the reason"
    assert set(NO_SECTION) - loose == set(), "These are in a section now (or gone): take them off NO_SECTION"


# ---------- 5. secret links stay out of the access log ----------

def test_access_log_hides_athlete_links_and_the_webhook_secret(caplog):
    mkapp()          # create_app puts the filter on uvicorn's access logger
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, RedactSecrets) for f in access.filters) == 1
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for path, status in (("/a/old-token-ava/runs/12", 200), ("/garmin/webhook/hooksecret", 200),
                             ("/garmin/callback?code=onetime&state=abc", 303), ("/athletes/3", 200)):
            access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.5:4100", "GET", path, "1.1", status)
    text = caplog.text
    assert "old-token-ava" not in text and "hooksecret" not in text and "onetime" not in text
    assert '"GET /a/…/runs/12 HTTP/1.1" 200' in text and '"GET /garmin/webhook/… HTTP/1.1" 200' in text
    assert '"GET /garmin/callback?… HTTP/1.1" 303' in text and '"GET /athletes/3 HTTP/1.1" 200' in text   # still logged
    assert redact_path("/team-calendar/abc/All.ics") == "/team-calendar/…/All.ics" and redact_path("/a") == "/a"


def test_injuries_off_hides_injury_details_on_athlete_and_run_pages():
    """The other pages the review noted: the athlete page (pain column, injuries box, film-after-injury note), the
    run page's pain pill, and the athletes list and Running form page's "film later" notes."""
    from xcapp import form_plan as FP
    from xcapp.models import Activity, MissedDay
    for hidden, shown in (("injuries", False), ("", True)):
        app, aid = mkapp(hidden=hidden)
        db = SessionLocal()
        d = dt.date.today() - dt.timedelta(days=1)
        act = Activity(athlete_id=aid, source="upload", external_id=f"r{hidden}", start_utc=dt.datetime(d.year, d.month, d.day, 23),
                       local_date=d, duration_s=1800, distance_m=6000, rpe=7)
        db.add(act)
        db.flush()
        db.add(PainReport(athlete_id=aid, date=d, body_area="Hip", severity=6, note="sharp after hills", activity_id=act.id))
        db.add(MissedDay(athlete_id=aid, date=d - dt.timedelta(days=2), reason="Injury", body_area="Achilles", side="Left",
                         description="couldn't push off"))
        db.commit()
        rid = act.id
        db.close()
        c = TestClient(app, follow_redirects=False)
        login(c)
        page = c.get(f"/athletes/{aid}").text
        run = c.get(f"/athletes/{aid}/runs/{rid}").text
        for secret in ("Injuries and pain reports", "Tibial", "hurts on stairs", "sharp after hills", "6/10 Hip", "achilles", "<th>Pain</th>"):
            assert (secret in page) == shown, (hidden, secret)
        assert ("Pain 6/10: Hip" in run) == shown
        db = SessionLocal()
        db.query(Activity).delete()
        db.query(PainReport).delete()
        db.query(MissedDay).delete()
        db.query(Injury).delete()
        db.query(Athlete).delete()
        db.query(StaffUser).delete()
        db.commit()
        db.close()
    film = {"block": None, "kind": "fresh", "from": None, "label": "", "status": {1: "due", 2: "wait", 3: "done"},
            "due": ["a"], "wait": ["b"], "done": ["c"], "soon": [], "team": 3}
    hidden = FP.hide_injured(film)
    assert hidden["status"] == {1: "due", 3: "done"} and hidden["wait"] == [] and hidden["due"] == ["a"]
    assert film["wait"] == ["b"]                                         # the original isn't changed

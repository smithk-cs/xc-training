"""Sessions end on a password change, your own password needs the current one, security headers, safe CSV cells."""
import re

from fastapi.testclient import TestClient

from xcapp.security import csv_safe


def setup_app():
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    db.add(StaffUser(username="boss", role="admin", password_hash=hash_password("correct-horse-1")))
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="=HYPERLINK", last_name="x", gender="Boys", grade=11, group="Varsity", link_token="t1"))
    db.commit()
    ids = {u.username: u.id for u in db.query(StaffUser).all()}
    db.close()
    return create_app(), ids


def login(app, name, pw="correct-horse-1"):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": name, "password": pw})
    return c


def token(c, page="/"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(page).text).group(1)


def test_password_change_signs_out_other_devices():
    app, ids = setup_app()
    phone, laptop = login(app, "boss"), login(app, "boss")
    assert phone.get("/week").status_code == 200 and laptop.get("/week").status_code == 200
    # Your own password: the current one is needed
    r = laptop.post(f"/staff/{ids['boss']}/password", data={"csrf": token(laptop, "/staff"), "password": "brand-new-pass-2"})
    assert "did not match" in laptop.get("/staff").text
    laptop.post(f"/staff/{ids['boss']}/password", data={"csrf": token(laptop, "/staff"), "current": "correct-horse-1",
                                                         "password": "brand-new-pass-2"})
    assert laptop.get("/week").status_code == 200                     # the device that changed it stays signed in
    assert phone.get("/week").headers["location"] == "/login"        # every other device is signed out
    assert login(app, "boss", "brand-new-pass-2").get("/week").status_code == 200
    # An admin resetting someone else's password doesn't need theirs, and signs them out too
    coach = login(app, "coach1")
    laptop.post(f"/staff/{ids['coach1']}/password", data={"csrf": token(laptop, "/staff"), "password": "reset-by-admin-3"})
    assert coach.get("/week").headers["location"] == "/login"


def test_security_headers_and_race_day_permissions():
    app, _ = setup_app()
    c = login(app, "boss")
    h = c.get("/week").headers
    assert "camera=()" in h["permissions-policy"] and "geolocation=()" in h["permissions-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in h                       # tests run with SECURE_COOKIES=false (plain http)
    csrf = token(c, "/race-day")
    c.post("/race-day/new", data={"csrf": csrf, "label": "Meet"})
    tap = re.search(r'href="/rd/([A-Za-z0-9]+)"', c.get("/race-day").text).group(1)
    assert "camera=(self)" in c.get(f"/rd/{tap}").headers["permissions-policy"]


def test_hsts_on_https(monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    from xcapp import config
    config.reload_settings()
    from xcapp.web import create_app
    r = TestClient(create_app(), base_url="https://testserver").get("/login")
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")


def test_csv_cells_cannot_run_formulas():
    assert csv_safe("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")" and csv_safe("+1 555") == "'+1 555" and csv_safe("@SUM") == "'@SUM"
    assert csv_safe("-3.5") == "-3.5" and csv_safe(-3.5) == -3.5 and csv_safe("Sam") == "Sam" and csv_safe(None) is None
    app, _ = setup_app()
    c = login(app, "boss")
    assert "'=HYPERLINK x" in c.get("/athletes/links.csv").text

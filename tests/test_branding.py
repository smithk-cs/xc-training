"""The team logo, uploaded by an admin and kept in the database (hosts like Cloudflare Containers have no lasting disk)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_web import csrf_of, login
from xcapp import config
from xcapp.db import SessionLocal
from xcapp.models import Athlete, StaffUser, SyncLog
from xcapp.security import hash_password

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82")
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="#a6192e"/></svg>'


def make_app():
    from xcapp.web import create_app
    return create_app()


@pytest.fixture
def app():
    db = SessionLocal()
    for name, role in (("admin1", "admin"), ("coach1", "coach")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava"))
    db.commit()
    db.close()
    return make_app()


def upload(c, data, name="logo.png"):
    return c.post("/branding/logo", data={"csrf": csrf_of(c, "/staff")}, files={"logo": (name, data)})


def test_upload_show_replace_and_remove(app):
    c = login(app, "admin1")
    assert c.get("/branding/logo").status_code == 404
    assert 'src="/branding/logo"' not in c.get("/staff").text

    r = upload(c, PNG)
    assert r.status_code == 303 and r.headers["location"] == "/staff"
    assert "Logo updated" in c.get("/staff").text
    r = c.get("/branding/logo")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    assert r.headers["cache-control"] == "public, max-age=300"
    for page in ("/", "/staff"):
        assert 'src="/branding/logo"' in c.get(page).text
    athlete = TestClient(app)
    assert 'src="/branding/logo"' in athlete.get("/a/tok-ava").text and 'src="/branding/logo"' in athlete.get("/login").text

    # Kept in the database, so a fresh start (a new container) still has it.
    assert TestClient(make_app()).get("/branding/logo").content == PNG

    upload(c, SVG, "logo.svg")
    r = c.get("/branding/logo")
    assert r.content == SVG and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"]

    page = c.get("/staff").text
    assert "Remove uploaded logo" in page
    c.post("/branding/logo/delete", data={"csrf": csrf_of(c, "/staff")})
    assert c.get("/branding/logo").status_code == 404
    page = c.get("/staff").text
    assert "Uploaded logo removed." in page and "Remove uploaded logo" not in page
    db = SessionLocal()
    msgs = [x.message for x in db.scalars(select(SyncLog).where(SyncLog.kind == "admin"))]
    db.close()
    assert msgs == ["admin1 uploaded a new team logo."] * 2 + ["admin1 removed the uploaded team logo."]


def test_refused_uploads(app):
    coach = login(app, "coach1")
    assert coach.post("/branding/logo", data={"csrf": csrf_of(coach, "/")}, files={"logo": ("l.png", PNG)}).status_code == 403
    assert coach.post("/branding/logo/delete", data={"csrf": csrf_of(coach, "/")}).status_code == 403

    c = login(app, "admin1")
    assert c.post("/branding/logo", data={"csrf": "wrong"}, files={"logo": ("l.png", PNG)}).status_code == 400
    for data, name, message in ((b"not an image", "logo.png", "not a PNG, JPEG, WebP or SVG"),
                                (b"<html><svg></svg></html>", "logo.svg", "not a PNG, JPEG, WebP or SVG"),
                                (PNG + b"\0" * (1024 * 1024), "big.png", "over 1 MB")):
        upload(c, data, name)
        assert message in c.get("/staff").text
    c.post("/branding/logo", data={"csrf": csrf_of(c, "/staff")})
    assert "Choose an image file first." in c.get("/staff").text
    assert c.get("/branding/logo").status_code == 404


def test_logo_file_setting_still_works(app, tmp_path, monkeypatch):
    path = tmp_path / "team-logo.svg"
    path.write_bytes(SVG)
    monkeypatch.setenv("TEAM_LOGO_FILE", str(path))
    config.reload_settings()
    app = make_app()
    c = login(app, "admin1")
    assert c.get("/branding/logo").content == SVG
    assert "Remove uploaded logo" not in c.get("/staff").text
    upload(c, PNG)  # an uploaded logo takes priority over the file
    assert c.get("/branding/logo").content == PNG
    c.post("/branding/logo/delete", data={"csrf": csrf_of(c, "/staff")})
    assert "The logo file from the server settings shows instead." in c.get("/staff").text
    assert c.get("/branding/logo").content == SVG

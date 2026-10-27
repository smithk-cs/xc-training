"""Athlete photos: matched from a .zip by file name, shrunk, metadata removed, shown on the roster and athlete pages."""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from xcapp import photos as P
from xcapp.db import SessionLocal
from xcapp.models import Athlete, AthletePhoto, StaffUser
from xcapp.security import hash_password


def jpeg(w=1200, h=1600, gps=False):
    img = Image.new("RGB", (w, h), (150, 20, 40))
    buf = io.BytesIO()
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {1: "N", 2: (38.0, 35.0, 0.0)}
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def zipped(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def app():
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    db.add_all([Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam"),
                Athlete(first_name="José", last_name="O'Neil", gender="Boys", grade=11, group="JV", link_token="t-jose"),
                Athlete(first_name="Theo", last_name="Marsh", gender="Boys", grade=10, group="JV", link_token="t-theo")])
    db.commit()
    db.close()
    from xcapp.web import create_app
    return create_app()


def login(app, user):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c


def test_prepare_and_names():
    out = P.prepare(jpeg(gps=True))
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG" and max(img.size) == 400 and img.size == (300, 400) and not img.getexif()
    with pytest.raises(P.PhotoError):
        P.prepare(b"not an image")
    assert P.keys_for_file("Lowry, Sam") == ["samlowry", "lowrysam"]
    assert P.keys_for_file("sam-lowry_2026")[0] == "samlowry" and P.keys_for_file("Jose ONeil")[0] == P.name_key("José O'Neil")


def test_zip_import_and_pages(app):
    c = login(app, "coach1")
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/athletes").text).group(1)
    z = zipped([("roster/Lowry, Sam.jpg", jpeg()), ("roster/jose-oneil.JPG", jpeg()), ("roster/Pat Reed.jpg", jpeg()),
                ("roster/Theo Marsh.jpg", b"broken"), ("readme.txt", b"hi"), ("__MACOSX/._x.jpg", b"")])
    assert c.post("/athletes/photos", data={"csrf": csrf}, files={"file": ("photos.zip", z, "application/zip")}).status_code == 303
    page = c.get("/athletes").text
    assert "Added 2 photos." in page and "Pat Reed.jpg" in page and "Theo Marsh.jpg" in page
    db = SessionLocal()
    ids = {a.first_name: a.id for a in db.query(Athlete)}
    assert db.query(AthletePhoto).count() == 2
    db.close()
    assert f'src="/athletes/{ids["Sam"]}/photo?v=' in page and 'class="avatar blank"' in page
    r = c.get(f"/athletes/{ids['Sam']}/photo")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"].startswith("private")
    assert c.get(f"/athletes/{ids['Theo']}/photo").status_code == 404
    assert TestClient(app).get(f"/athletes/{ids['Sam']}/photo", follow_redirects=False).status_code in (302, 303, 401)
    assert TestClient(app).get("/a/t-sam/photo").status_code == 200 and 'class="hero-photo"' in TestClient(app).get("/a/t-sam").text

    detail = c.get(f"/athletes/{ids['Theo']}").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', detail).group(1)
    c.post(f"/athletes/{ids['Theo']}/photo", data={"csrf": csrf}, files={"photo": ("t.png", jpeg(50, 50), "image/png")})
    assert "Saved Theo Marsh&#39;s photo." in c.get(f"/athletes/{ids['Theo']}").text
    c.post(f"/athletes/{ids['Theo']}/photo", data={"csrf": csrf, "remove": "1"})
    assert c.get(f"/athletes/{ids['Theo']}/photo").status_code == 404
    assert "roster photo" in c.get("/privacy").text

    t = login(app, "trainer1")
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/athletes").text).group(1)
    assert "Add athlete photos" not in t.get("/athletes").text
    assert t.post("/athletes/photos", data={"csrf": tcsrf}, files={"file": ("p.zip", z)}).status_code == 403

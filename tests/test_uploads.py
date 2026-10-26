"""Uploading many runs at once: several .fit files, or .zip files of them (including Garmin's data export)."""
import datetime as dt
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from fit_tool.profile.profile_type import Sport

from fitgen import build_fit, flat, rep_workout
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, StaffUser
from xcapp.security import hash_password


def run(day, hour=14):
    return rep_workout(dt.datetime(2026, 10, day, hour, 0, tzinfo=dt.timezone.utc), [(1000, 200, 180, 20)] * 3)


def ride(day):
    return build_fit(dt.datetime(2026, 10, day, 15, 0, tzinfo=dt.timezone.utc),
                     [{"dist": 20000, "dur": 2400, "hr": flat(130)}], sport=Sport.CYCLING)


def zipped(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def client():
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="tok-ava"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    return TestClient(create_app(), follow_redirects=False)


def upload(c, files, url="/a/tok-ava/upload", page="/a/tok-ava"):
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(page).text).group(1)
    r = c.post(url, data={"csrf": csrf}, files=[("file", f) for f in files])
    assert r.status_code == 303
    return re.findall(r'<div class="flash (\w+)"[^>]*>([^<]*)', c.get(page).text)


def runs():
    db = SessionLocal()
    out = sorted(a.local_date.day for a in db.query(Activity).all())
    db.close()
    return out


def test_several_files_at_once(client):
    page = client.get("/a/tok-ava").text
    assert 'accept=".fit,.zip" multiple' in page and "Export Your Data" in page
    msgs = upload(client, [("a.fit", run(20)), ("b.fit", run(21)), ("c.fit", ride(22))])
    assert msgs == [("ok", "Added 2 runs (Oct 20 to Oct 21), 1 skipped because they weren&#39;t runs.")]
    assert runs() == [20, 21]


def test_a_zip_and_a_garmin_style_export(client):
    plain = zipped([("Oct/run1.fit", run(13)), ("Oct/run2.FIT", run(14)), ("notes.txt", b"hello"),
                    ("ride.fit", ride(15)), ("broken.fit", b"not a fit file")])
    msgs = upload(client, [("runs.zip", plain)])
    assert msgs == [("ok", "Added 2 runs (Oct 13 to Oct 14), 1 skipped because they weren&#39;t runs, 1 could not be read.")]

    # Garmin's export: a big zip holding other zips of uploaded files, among lots of other data.
    inner = zipped([("UploadedFiles_0-_Part1/a.fit", run(16)), ("UploadedFiles_0-_Part1/b.fit", run(13))])
    export = zipped([("DI_CONNECT/DI-Connect-Wellness/sleep.json", b"{}"), ("DI_CONNECT/DI-Connect-User/user.json", b"{}"),
                     ("DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip", inner)])
    msgs = upload(client, [("export.zip", export)])
    assert msgs == [("ok", "Added 1 run, 1 already here.")]
    assert runs() == [13, 14, 16]


def test_single_file_keeps_its_detailed_message(client):
    msgs = upload(client, [("run.fit", run(20))])
    assert len(msgs) == 1 and msgs[0][0] == "ok" and msgs[0][1].startswith("Added a ") and "with 3 reps" in msgs[0][1]
    assert upload(client, [("run.fit", run(20))]) == [("ok", "That run is already in the system.")]


def test_bad_uploads_explain_themselves(client):
    assert upload(client, [("x.zip", b"PK\x03\x04 truncated")]) == [("error", "x.zip is not a readable .zip file.")]
    assert upload(client, [("docs.zip", zipped([("readme.txt", b"hi")]))]) == [
        ("error", "No .fit activity files were found in that upload.")]
    assert upload(client, [("rides.zip", zipped([("r.fit", ride(3)), ("r2.fit", ride(4))]))]) == [
        ("error", "Added 0 runs, 2 skipped because they weren&#39;t runs.")]
    assert upload(client, [("ride.zip", zipped([("r.fit", ride(3))]))]) == [  # one file inside: the one-file message
        ("error", "That file is not a run, so it was not added.")]
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/a/tok-ava").text).group(1)
    client.post("/a/tok-ava/upload", data={"csrf": csrf})
    assert "Choose a .fit or .zip file first." in client.get("/a/tok-ava").text
    assert runs() == []


def test_coaches_can_upload_a_batch_for_an_athlete(client):
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    db = SessionLocal()
    aid = db.query(Athlete).one().id
    db.close()
    page = client.get(f"/athletes/{aid}").text
    assert 'accept=".fit,.zip" multiple' in page
    msgs = upload(client, [("runs.zip", zipped([("1.fit", run(1)), ("2.fit", run(2))]))],
                  url=f"/athletes/{aid}/upload", page=f"/athletes/{aid}")
    assert ("ok", "Added 2 runs (Oct 1 to Oct 2).") in msgs and runs() == [1, 2]

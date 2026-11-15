"""Course flyovers: building one from a GPX or FIT track, the built-in courses, and the pages for coaches and athletes."""
import datetime as dt
import json
import math
import re

import pytest

from xcapp import flyover as FL


def fake_tile(url):
    """A stand-in map tile: a different color for each tile, with a little detail so it isn't 'no imagery' filler."""
    from io import BytesIO

    from PIL import Image
    z, y, x = (int(v) for v in url.rsplit("/tile/", 1)[1].split("/"))
    im = Image.new("RGB", (256, 256), ((x * 37) % 256, (y * 53) % 256, 90))
    for k in range(0, 256, 16):
        im.putpixel((k, k), (255, 255, 255))
    out = BytesIO()
    im.save(out, "JPEG")
    return out.getvalue()


@pytest.fixture(autouse=True)
def no_real_tiles(monkeypatch):
    """Tests never reach the real imagery services."""
    monkeypatch.setattr(FL, "fetch_tile", fake_tile)
    monkeypatch.setattr(FL.satellite_image, "__defaults__", (fake_tile,))


def loop_track(n=600, laps=1.0, with_ele=True):
    """A loop of about 1.9 miles near Folsom with three rolling hills."""
    out = []
    for i in range(n):
        t = i / (n - 1) * 2 * math.pi * laps
        out.append((38.65 + 0.004 * math.sin(t), -121.15 + 0.006 * math.cos(t) + 0.00001 * i,
                    100 + 15 * math.sin(3 * t) if with_ele else None))
    return out


def gpx(track, route=False) -> bytes:
    tag = "rtept" if route else "trkpt"
    pts = "".join(f'<{tag} lat="{a:.6f}" lon="{b:.6f}">' + (f"<ele>{e:.1f}</ele>" if e is not None else "") + f"</{tag}>"
                  for a, b, e in track)
    body = f"<rte>{pts}</rte>" if route else f"<trk><name>Course</name><trkseg>{pts}</trkseg></trk>"
    return ('<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="test" '
            f'xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>').encode()


def test_build_from_gpx():
    d = FL.build(FL.read_track("course.gpx", gpx(loop_track())))
    assert 1.85 < d["total"] < 1.95 and d["heights"] == "track"
    assert d["P"][0][2] == 0 and d["P"][-1][2] == d["total"] and len(d["P"]) < 400        # thinned
    xs, ys = [p[0] for p in d["P"]], [p[1] for p in d["P"]]
    assert min(xs) >= 0 and max(xs) <= FL.SIDE and min(ys) >= 0 and max(ys) <= FL.SIDE
    assert [m[1] for m in d["MK"]] == ["½ mi", "1 mi", "1½ mi"]
    assert 250 < d["climb_ft"] < 320                                                       # 3 hills of about 98 ft
    assert d["fmin"] < d["low"][1] < d["high"][1] < d["fmax"] and d["grid"]
    assert "High point" in FL.default_notes(d) and FL.default_notes(d).startswith("0.00 Start")
    assert FL.build(FL.read_track("route.gpx", gpx(loop_track(), route=True)))["total"] == d["total"]   # route points too


def test_heights_from_the_map_or_none():
    track = loop_track(with_ele=False)
    d = FL.build(track, lookup=lambda coords: [50.0 + i for i in range(len(coords))])
    assert d["heights"] == "map" and d["E"][-1][1] > d["E"][0][1]
    flat = FL.build(track, lookup=lambda coords: None)
    assert flat["heights"] == "none" and {e for _, e in flat["E"]} == {0.0}
    assert "flat" in FL.about_text(flat, "x.gpx")


def test_bad_tracks():
    with pytest.raises(FL.FlyoverError, match="document type"):
        FL.read_track("x.gpx", b'<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY a "aaaa">]><gpx>&a;</gpx>')
    with pytest.raises(FL.FlyoverError, match="can't be read"):
        FL.read_track("x.gpx", b"<gpx><trk>")
    with pytest.raises(FL.FlyoverError, match="too few"):
        FL.read_track("x.gpx", gpx(loop_track()[:5]))
    with pytest.raises(FL.FlyoverError, match="GPX file"):
        FL.read_track("map.pdf", b"%PDF-1.4")
    with pytest.raises(FL.FlyoverError, match="shorter than 400 m"):
        FL.build([(38.65 + i * 1e-6, -121.15, None) for i in range(50)], lookup=None)


def test_track_from_a_fit_file():
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer
    ms0 = int(dt.datetime(2026, 9, 1, 16, tzinfo=dt.timezone.utc).timestamp()) * 1000
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    f = FileIdMessage()
    f.type, f.manufacturer, f.time_created, f.serial_number = FileType.ACTIVITY, Manufacturer.GARMIN.value, ms0, 1
    b.add(f)
    recs = []
    for i, (lat, lon, ele) in enumerate(loop_track(n=300)):
        r = RecordMessage()
        r.timestamp, r.position_lat, r.position_long, r.altitude = ms0 + i * 1000, lat, lon, ele
        recs.append(r)
    b.add_all(recs)
    pts = FL.read_track("run.fit", b.build().to_bytes())
    assert len(pts) == 300 and abs(pts[0][0] - 38.65) < 1e-4 and abs(pts[0][1] - (-121.144)) < 1e-3
    assert pts[0][2] is not None and abs(pts[0][2] - 100) < 1
    assert 1.8 < FL.build(pts)["total"] < 2.0


def test_satellite_photo():
    from io import BytesIO

    from PIL import Image
    d = FL.build(loop_track())
    lat, lon = FL.to_lat_lon(d["geo"], d["P"][0][0], d["P"][0][1])          # the drawing maps back to the earth
    assert abs(lat - 38.65) < 1e-6 and abs(lon - (-121.144)) < 1e-5
    urls = []
    img, credit = FL.satellite_image(d["geo"], fetch=lambda u: urls.append(u) or fake_tile(u))
    assert credit.startswith("Imagery: USGS") and urls[0].startswith("https://basemap.nationalmap.gov/")
    w, h = Image.open(BytesIO(img)).size
    assert w == h and 1000 <= w <= FL.MAX_IMG_PX and len(urls) <= FL.MAX_TILES
    z = int(urls[0].rsplit("/tile/", 1)[1].split("/")[0])
    assert 15 <= z <= 18

    filler = BytesIO()
    Image.new("RGB", (256, 256), (210, 210, 210)).save(filler, "JPEG")

    def usgs_blank(u):                                     # USGS has only its plain filler here: no photo (USGS only)
        assert "nationalmap" in u
        return filler.getvalue()
    assert FL.satellite_image(d["geo"], fetch=usgs_blank) is None
    assert [c for c, _ in FL.TILE_SOURCES] == ["Imagery: USGS The National Map"]
    assert FL.satellite_image(d["geo"], fetch=lambda u: None) is None
    assert FL.satellite_image(None) is None


def test_notes_and_sections():
    assert FL.parse_notes("1.0 Big climb\n0 - Flat start\nnot a note\n9 mi: Kick", 3.1) == [
        [0.0, "Flat start"], [1.0, "Big climb"], [3.1, "Kick"]]
    secs = FL.parse_sections("1 School loop\n0.2 Back hills: Start, A, B\n1.56 Reservoir lap: H, D", 3.06)
    assert secs == [[0.0, 1.0, "Back hills", "Start, A, B"], [1.0, 1.56, "School loop", ""], [1.56, 3.06, "Reservoir lap", "H, D"]]
    assert FL.sections_text(secs) == "0 Back hills: Start, A, B\n1 School loop\n1.56 Reservoir lap: H, D"


def test_built_in_courses():
    ps = FL.presets()
    assert set(ps) == {"willow", "frogtown", "wildfire"} and ps["willow"]["distance"] == "5K" and ps["frogtown"]["distance"] == "3 mi"
    data, notes, sections = FL.preset_record("willow")
    assert data["overlay"]["href"] == "/static/flyover/willow-satellite.jpg" and data["base"] and data["CP"]
    assert "Big climb out of G" in notes and sections.startswith("0 Back hills: Start, A, B")
    assert FL.preset_record("frogtown")[0]["official"]["href"] == "/static/flyover/frogtown-official.jpg"
    wild = FL.preset_record("wildfire")[0]                     # no heights yet: a flat profile the page shows as such
    assert ps["wildfire"]["distance"] == "5K" and wild["heights"] == "none" and wild["followW"] == 380
    assert wild["overlay"]["href"] == "/static/flyover/wildfire-satellite.jpg" and wild["E"][-1][0] == wild["total"]
    assert "Into the woods loop" in FL.preset_record("wildfire")[1]


def test_flyover_pages_for_coaches_and_athletes():
    from fastapi.testclient import TestClient

    from xcapp import analysis as A
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, Course, CourseFlyover, DivisionAthlete, MeetDivision, MeetEntry, StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    db.add_all([Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-ava"),
                Athlete(first_name="Bea", last_name="Lane", gender="Girls", grade=9, group="JV", link_token="t-bea")])
    course = Course(name="Willow Hill", location="Folsom", distances="5K,2 mi")
    db.add(course)
    db.commit()
    cid = course.id
    db.close()
    from xcapp.web import create_app
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    edit = c.get(f"/courses?edit={cid}").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', edit).group(1)
    assert 'id="flyovers"' in edit and "No flyovers yet." in edit and '<option value="willow">Willow Hills 5K</option>' in edit

    def make(dist, name=None, data=None, preset=""):
        files = {"file": (name, data, "application/gpx+xml")} if name else None
        return c.post(f"/courses/{cid}/flyovers", data={"csrf": csrf, "distance": dist, "preset": preset}, files=files)

    assert make("2 mi", "map.pdf", b"%PDF-1.4").status_code == 303
    assert "Upload the course as a GPX file" in c.get("/courses").text
    make("3 mi", "c.gpx", gpx(loop_track()))                                     # not raced here
    assert "Pick the distance this flyover is for." in c.get("/courses").text
    make("2 mi", "Willow 2 mile.gpx", gpx(loop_track()))
    assert "Saved the 2 mi flyover for Willow Hill." in c.get("/courses").text
    make("5K", preset="willow")
    db = SessionLocal()
    flys = {f.distance: f for f in db.query(CourseFlyover).all()}
    assert set(flys) == {"5K", "2 mi"} and flys["5K"].preset == "willow" and flys["2 mi"].filename == "Willow 2 mile.gpx"
    assert flys["2 mi"].notes.startswith("0.00 Start")
    f5, f2 = flys["5K"].id, flys["2 mi"].id
    m = MeetEntry(name="Folsom Invite", date=A.team_today() + dt.timedelta(days=4), location="Folsom", course_id=cid)
    d5 = MeetDivision(name="Girls Varsity", gender="Girls", level="Varsity", distance="5,000 Meters", position=0)
    m.divisions.append(d5)
    db.add(m)
    db.flush()
    d5.entries.append(DivisionAthlete(athlete_id=db.query(Athlete).filter_by(first_name="Ava").one().id, position=0))
    db.commit()
    db.close()

    # coaches: notes and sections, the page
    c.post(f"/courses/{cid}/flyovers", data={"csrf": csrf, "distance": "2 mi", "notes": "0 Off the line\n1.2 <b>Big</b> hill",
                                            "sections": "0 Out: A, B\n1 Back: C"})
    assert "Saved the notes for the 2 mi flyover." in c.get("/courses").text
    assert f'href="/courses/{cid}/flyovers/{f2}"' in c.get("/courses").text
    page = c.get(f"/courses/{cid}/flyovers/{f2}")
    assert page.status_code == 200 and "/static/flyover.js" in page.text
    view = json.loads(re.search(r'<script type="application/json" id="fly-data">(.*?)</script>', page.text, re.S).group(1))
    assert view["satellite"]["href"].startswith(f"/courses/{cid}/flyovers/{f2}/satellite.jpg?v=") and "geo" not in view
    assert view["satellite"]["credit"].startswith("Imagery: USGS") and view["satellite"]["x"] == -FL.IMG_PAD
    sat = c.get(view["satellite"]["href"])
    assert sat.status_code == 200 and sat.headers["content-type"] == "image/jpeg" and sat.content[:2] == b"\xff\xd8"
    assert "🛰 Satellite photo saved" in c.get(f"/courses?edit={cid}").text
    assert view["title"] == "Willow Hill 2 mi" and view["place"] == "Folsom" and view["heights"] == "track"
    assert view["notes"] == [[0.0, "Off the line"], [1.2, "<b>Big</b> hill"]] and "<b>" not in page.text   # escaped in the page
    assert [s[2] for s in view["sections"]] == ["Out", "Back"]
    v5 = json.loads(re.search(r'id="fly-data">(.*?)</script>', c.get(f"/courses/{cid}/flyovers/{f5}").text, re.S).group(1))
    assert v5["overlay"]["href"].endswith("willow-satellite.jpg") and v5["place"].startswith("Willow Hills Reservoir")
    assert "satellite" not in v5 and v5["overlay"]["label"] == "Official map"
    assert c.get(f"/courses/{cid}/flyovers/{f5}/satellite.jpg").status_code == 404
    assert c.get("/static/flyover/willow-satellite.jpg").status_code == 200
    assert c.get(f"/courses/{cid + 1}/flyovers/{f5}").status_code == 404
    anon = TestClient(app, follow_redirects=False)
    assert anon.get(f"/courses/{cid}/flyovers/{f5}").status_code in (303, 401, 403)

    # athletes: their distance's flyover on the meet, or every one before they're entered
    ava = anon.get("/a/t-ava").text
    assert f'href="/a/t-ava/flyovers/{f5}">🏃 5K course flyover</a>' in ava and "2 mi course flyover" not in ava
    bea = anon.get("/a/t-bea").text
    assert "🏃 5K course flyover" in bea and "🏃 2 mi course flyover" in bea
    athlete_view = anon.get(f"/a/t-ava/flyovers/{f2}")
    assert athlete_view.status_code == 200 and f'"/a/t-ava/flyovers/{f2}/satellite.jpg?v=' in athlete_view.text
    assert anon.get(f"/a/t-ava/flyovers/{f2}/satellite.jpg").status_code == 200
    assert anon.get(f"/a/nope/flyovers/{f2}/satellite.jpg").status_code == 404
    assert anon.get(f"/courses/{cid}/flyovers/{f2}/satellite.jpg").status_code in (303, 401, 403)
    assert anon.get(f"/a/nope/flyovers/{f2}").status_code == 404

    # a new track keeps the notes; removing; the course takes its flyovers with it
    make("2 mi", "again.gpx", gpx(loop_track(n=400)))
    db = SessionLocal()
    assert db.get(CourseFlyover, f2).notes.startswith("0 Off the line") and db.get(CourseFlyover, f2).filename == "again.gpx"
    db.close()
    c.post(f"/courses/{cid}/flyovers/{f2}/delete", data={"csrf": csrf})
    assert "Removed the 2 mi flyover." in c.get("/courses").text
    db = SessionLocal()
    db.delete(db.get(Course, cid))
    db.commit()
    assert db.query(CourseFlyover).count() == 0
    db.close()


def test_saved_esri_photos_are_removed():
    from xcapp import db as DBM
    from xcapp.db import SessionLocal
    from xcapp.models import Course, CourseFlyover, FlyoverImage
    s = SessionLocal()
    c = Course(name="Test Park")
    s.add(c)
    s.flush()
    for i, credit in enumerate(("Imagery: USGS The National Map", "Imagery: Esri, Maxar, Earthstar Geographics")):
        f = CourseFlyover(course_id=c.id, distance=f"{i + 2} mi")
        s.add(f)
        s.flush()
        s.add(FlyoverImage(flyover_id=f.id, data=b"jpeg", credit=credit, size=100))
    s.commit()
    s.close()
    DBM.create_all()                                         # at startup: Esri's photos go, USGS's stay
    s = SessionLocal()
    assert [x.credit for x in s.query(FlyoverImage).all()] == ["Imagery: USGS The National Map"]
    s.close()

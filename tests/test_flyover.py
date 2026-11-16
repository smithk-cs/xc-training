"""Course flyovers: building one from a GPX or FIT track, the built-in courses, and the pages for coaches and athletes."""
import datetime as dt
import json
import math
import re

import pytest

from xcapp import flyover as FL

REAL_USGS = FL.fetch_usgs_elevations    # before the tests' stand-in that keeps USGS out of every other test


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


def usgs_post(calls=None, missing=lambda k: False):
    """A stand-in for USGS 3DEP's getSamples: the ground rises with latitude (1 m per 0.0001°), samples out of order."""
    def post(url, form):
        assert url == FL.USGS_ELEVATION_URL and form["geometryType"] == "esriGeometryMultipoint" and form["f"] == "json"
        g = json.loads(form["geometry"])
        assert g["spatialReference"] == {"wkid": 4326}
        if calls is not None:
            calls.append(len(g["points"]))
        samples = [{"locationId": k, "value": "NoData" if missing(k) else str(30 + (lat - 38.64) * 10000)}
                   for k, (lon, lat) in enumerate(g["points"])]          # points are [lon, lat]
        return {"samples": list(reversed(samples))}
    return post


def test_usgs_elevations():
    coords = [(38.64 + i * 0.0001, -121.15) for i in range(400)]
    calls = []
    got = REAL_USGS(coords, post=usgs_post(calls))
    assert calls == [150, 150, 100] and len(got) == 400                    # in batches, back in order
    assert abs(got[0] - 30) < 1e-6 and abs(got[399] - 429) < 1e-6
    gappy = REAL_USGS(coords, post=usgs_post(missing=lambda k: k in (5, 6)))
    assert abs(gappy[5] - got[5]) < 1e-6 and abs(gappy[6] - got[6]) < 1e-6   # a few gaps filled from neighbours
    assert REAL_USGS(coords, post=usgs_post(missing=lambda k: k % 3 == 0)) is None   # too many
    def down(url, form):
        raise OSError("unreachable")
    assert REAL_USGS(coords, post=down) is None and REAL_USGS([], post=down) is None


def test_flyover_heights_from_usgs():
    track = loop_track(with_ele=True)
    usgs = lambda coords: REAL_USGS(coords, post=usgs_post())
    d = FL.build(track, usgs=usgs)
    assert d["heights"] == "usgs" and "USGS elevation data" in FL.about_text(d, "x.gpx")
    span_ft = (max(p[0] for p in track) - min(p[0] for p in track)) * 10000 * FL.FEET_PER_METER   # the loop's north-south rise
    assert abs((d["high"][1] - d["low"][1]) - span_ft) < 0.1 * span_ft
    gps = FL.build(track)                                                    # USGS unreachable: the file's GPS heights
    assert gps["heights"] == "track"
    # A flyover already made without heights gets USGS heights later; the route stays
    flat = FL.build(loop_track(with_ele=False), lookup=lambda c: None)
    assert flat["heights"] == "none" and flat["climb_ft"] == 0
    P = [p[:] for p in flat["P"]]
    assert FL.reheight(flat, usgs=usgs) and flat["heights"] == "usgs" and flat["climb_ft"] > 50 and flat["P"] == P
    assert not FL.reheight({"P": P}, usgs=usgs)                              # no map position (a built-in course)
    assert not FL.reheight(FL.build(track, lookup=lambda c: None), usgs=lambda c: None)


def test_get_usgs_heights_button(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import Course, CourseFlyover, StaffUser
    from xcapp.security import hash_password
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    course = Course(name="Test Park")
    db.add(course)
    db.flush()
    fly = CourseFlyover(course_id=course.id, distance="2 mi", data=json.dumps(FL.build(loop_track(with_ele=False), lookup=lambda c: None)))
    db.add(fly)
    db.commit()
    cid, fid = course.id, fly.id
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get(f"/courses?edit={cid}").text
    assert "No heights (flat profile)" in page and "Get USGS heights" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post(f"/courses/{cid}/flyovers/{fid}/heights", data={"csrf": csrf})          # USGS unreachable (tests): nothing changes
    assert "couldn’t be reached" in c.get(f"/courses?edit={cid}").text
    monkeypatch.setattr(FL, "fetch_usgs_elevations", lambda coords, post=None: REAL_USGS(coords, post=usgs_post()))
    c.post(f"/courses/{cid}/flyovers/{fid}/heights", data={"csrf": csrf})
    page = c.get(f"/courses?edit={cid}").text
    assert "now has USGS heights" in page and "Heights from USGS elevation data" in page and "Refresh USGS heights" in page


def test_area_photo_and_densify():
    from io import BytesIO

    from PIL import Image
    n, w, s, e = FL.area_box(38.65, -121.15, 1500)
    assert abs(FL._hav((n, -121.15), (s, -121.15)) - 1500) < 15 and abs(FL._hav((38.65, w), (38.65, e)) - 1500) < 15
    img, credit, box = FL.area_image(38.65, -121.15, 1500)
    assert credit.startswith("Imagery: USGS") and box == (n, w, s, e)
    pw, ph = Image.open(BytesIO(img)).size
    assert pw == ph and 1000 <= pw <= FL.MAX_IMG_PX
    assert FL.area_image(38.65, -121.15, 1234) is None and FL.area_image(95, 0, 1500) is None   # only the offered sizes
    assert FL.area_image(38.65, -121.15, 1500, fetch=lambda u: None) is None                   # no imagery
    # The photo is linear in Web Mercator: the tracing page's formula (course-trace.js) lands where the tiles say
    merc = lambda lat: math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    for fy in (0.0, 0.25, 0.5, 0.9):
        y = merc(n) - fy * (merc(n) - merc(s))
        lat = math.degrees(math.atan(math.sinh(y)))
        (_, ty0), (_, ty1), (_, t) = FL._tile_xy(n, w, 17), FL._tile_xy(s, e, 17), FL._tile_xy(lat, w, 17)
        assert abs((t - ty0) / (ty1 - ty0) - fy) < 1e-9
    dense = FL.densify([(38.65, -121.15), (38.651, -121.15), (38.651, -121.149)])
    gaps = [FL._hav(a, b) for a, b in zip(dense, dense[1:])]
    assert max(gaps) <= 5.01 and dense[0][:2] == (38.65, -121.15) and dense[-1][:2] == (38.651, -121.149)
    assert all(p[2] is None for p in dense)


def test_trace_a_route_into_a_flyover(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import Course, CourseArea, CourseFlyover, StaffUser
    from xcapp.security import hash_password
    monkeypatch.setattr(FL, "fetch_usgs_elevations", lambda coords, post=None: REAL_USGS(coords, post=usgs_post()))
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    course = Course(name="Test Park", distances="5K,2 mi", latitude=38.65, longitude=-121.15)
    db.add(course)
    db.commit()
    cid = course.id
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get(f"/courses?edit={cid}").text
    assert "Satellite view and route tracing" in page and "Get the satellite photo" in page and 'name="map_url"' not in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post(f"/courses/{cid}/area", data={"csrf": csrf, "size": "1500", "lat": "", "lon": ""})   # the course's own location
    page = c.get(f"/courses?edit={cid}").text
    assert "Saved the satellite photo of the area" in page and 'id="tr-img"' in page and "course-trace.js" in page
    jpg = c.get(f"/courses/{cid}/area.jpg")
    assert jpg.status_code == 200 and jpg.headers["content-type"] == "image/jpeg" and jpg.content[:2] == b"\xff\xd8"
    # A loop traced in about 30 taps, about 1 mile around
    loop = [(38.65 + 0.0035 * math.sin(2 * math.pi * k / 30), -121.15 + 0.0045 * math.cos(2 * math.pi * k / 30)) for k in range(31)]
    r = c.post(f"/courses/{cid}/trace", json={"csrf": csrf, "distance": "2 mi", "points": loop})
    assert r.status_code == 200 and r.json()["next"] == f"/courses?edit={cid}#flyovers"
    db = SessionLocal()
    fly = db.query(CourseFlyover).one()
    data = json.loads(fly.data)
    assert fly.distance == "2 mi" and fly.filename == "Traced on the satellite photo" and data["heights"] == "usgs"
    assert 1.0 < data["measured_mi"] < 1.6 and data["geo"] and fly.satellite is not None and fly.notes
    fly.notes = "0.5 Big hill"
    db.commit()
    db.close()
    assert "Saved the 2 mi flyover for Test Park" in (pg := c.get(f"/courses?edit={cid}").text) and "traced on the satellite photo" in pg
    c.post(f"/courses/{cid}/trace", json={"csrf": csrf, "distance": "2 mi", "points": loop[:20]})   # traced again: notes stay
    db = SessionLocal()
    assert db.query(CourseFlyover).count() == 1 and db.query(CourseFlyover).one().notes == "0.5 Big hill"
    db.close()
    # Refused: off the photo, too few points, an unknown distance, a stale session
    assert "aren’t on the photo" in c.post(f"/courses/{cid}/trace", json={"csrf": csrf, "distance": "2 mi", "points": [[40, -100], [40, -100.1]]}).json()["error"]
    assert "at least two" in c.post(f"/courses/{cid}/trace", json={"csrf": csrf, "distance": "2 mi", "points": loop[:1]}).json()["error"]
    assert "Pick the distance" in c.post(f"/courses/{cid}/trace", json={"csrf": csrf, "distance": "10K", "points": loop}).json()["error"]
    assert c.post(f"/courses/{cid}/trace", json={"csrf": "nope", "distance": "2 mi", "points": loop}).status_code == 400
    db = SessionLocal()
    assert db.query(CourseArea).count() == 1
    db.close()


def place_preset(pid, lat0, lon0, turn_deg=0.0):
    """Where the placing page puts a built-in course: its start at lat0, lon0, north up (turned turn_deg clockwise),
    at its real length. Returns (route, checkpoints, labels) as latitudes and longitudes."""
    p = FL.placing_data()[pid]
    units = sum(math.dist(a, b) for a, b in zip(p["P"], p["P"][1:]))
    m_u = p["total"] * FL.METERS_PER_MILE / units
    t = math.radians(turn_deg)
    x0, y0 = p["P"][0]

    def ll(x, y):
        dx, dy = (x - x0) * m_u, (y - y0) * m_u                    # meters east, meters south
        ex, sy = dx * math.cos(t) - dy * math.sin(t), dx * math.sin(t) + dy * math.cos(t)
        return lat0 - sy / 110574.0, lon0 + ex / (111320.0 * math.cos(math.radians(lat0)))
    return ([list(ll(*q)) for q in p["P"]], [[c[0], *ll(c[1], c[2])] for c in p["CP"]], [[c[0], *ll(c[1], c[2])] for c in p["LAB"]])


def test_place_a_built_in_course(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp.db import SessionLocal
    from xcapp.models import Course, CourseFlyover, StaffUser
    from xcapp.security import hash_password
    pd = FL.placing_data()
    assert set(pd) == {"frogtown", "wildfire", "willow"} and pd["willow"]["distance"] == "5K" and len(pd["willow"]["CP"]) == 11
    geo = FL.build(loop_track(), lookup=lambda c: None, usgs=None)["geo"]
    u, v = FL.to_draw(geo, *FL.to_lat_lon(geo, 123.4, 321.0))
    assert abs(u - 123.4) < 1e-6 and abs(v - 321.0) < 1e-6                      # to_draw undoes to_lat_lon
    monkeypatch.setattr(FL, "fetch_usgs_elevations", lambda coords, post=None: REAL_USGS(coords, post=usgs_post()))
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    course = Course(name="Willow Hill", distances="5K,2 mi", latitude=38.672, longitude=-121.155)
    db.add(course)
    db.commit()
    cid = course.id
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/courses?edit={cid}").text).group(1)
    c.post(f"/courses/{cid}/area", data={"csrf": csrf, "size": "2500", "lat": "", "lon": ""})
    page = c.get(f"/courses?edit={cid}").text
    assert "Or place a built-in course on the photo" in page and "Willow Hills 5K" in page and '"place": "/courses/' in page
    route, cp, lab = place_preset("willow", 38.672, -121.155, turn_deg=15)
    r = c.post(f"/courses/{cid}/place", json={"csrf": csrf, "preset": "willow", "distance": "5K", "points": route, "cp": cp, "lab": lab})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    fly = db.query(CourseFlyover).one()
    data = json.loads(fly.data)
    assert fly.distance == "5K" and fly.preset == "" and fly.filename == "Willow Hills 5K, placed on the satellite photo"
    assert abs(data["measured_mi"] - 3.057) < 0.03 and data["heights"] == "usgs" and data["geo"] and fly.satellite is not None
    assert [x[0] for x in data["CP"]] == [x[0] for x in cp] and len(data["LAB"]) == 3
    # the checkpoints land where they were placed
    lat, lon = FL.to_lat_lon(data["geo"], data["CP"][0][1], data["CP"][0][2])
    assert abs(lat - cp[0][1]) < 2e-5 and abs(lon - cp[0][2]) < 2e-5
    assert "Flat start along the reservoir" in fly.notes and "Reservoir lap" in fly.sections      # the built-in notes and sections
    db.close()
    far = [[p[0] + 1, p[1]] for p in route]
    assert "isn’t on the photo" in c.post(f"/courses/{cid}/place", json={"csrf": csrf, "preset": "willow", "distance": "5K", "points": far}).json()["error"]
    assert "built-in" in c.post(f"/courses/{cid}/place", json={"csrf": csrf, "preset": "nope", "distance": "5K", "points": route}).json()["error"]

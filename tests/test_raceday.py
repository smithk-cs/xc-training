"""Race Day: boards, the tap and watch links, saving taps, the clock, scoring (Python and the page's JavaScript on the
same cases) and the GPS helpers."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from xcapp import raceday as RD

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEAMS = [{"id": "a", "name": "Jesuit", "color": "#a6192e"}, {"id": "b", "name": "Rival", "color": "#1b4fa0"},
         {"id": "c", "name": "Third", "color": "#ffcc00"}]


def taps(seq):
    return [{"id": f"x{i}", "at": 1000 * (i + 1), "team": t} for i, t in enumerate(seq)]


CASES = {
    # a: 1,3,5,7,9 = 25; b: 2,4,6,8,10 = 30; c has 2 runners and is skipped; "_other" is skipped
    "basic": taps(["a", "b", "c", "a", "_other", "b", "a", "b", "c", "a", "b", "a", "b"]),
    # a tie on 15 vs 15 broken by the 6th runner; the 8th runner of a is skipped
    "tie": taps(["a", "b", "b", "a", "a", "b", "b", "a", "a", "b", "b", "a", "a", "a", "b"]),
    "none": taps(["a", "b", "a"]),
}


def test_python_scoring():
    r = RD.score(CASES["basic"], TEAMS)
    assert [(s["id"], s["score"]) for s in r["standings"]] == [("a", 25), ("b", 30)]
    assert r["waiting"] == [{"id": "c", "n": 2}] and r["total"] == 13
    assert [o["tp"] for o in r["order"][:6]] == [1, 2, None, 3, None, 4]
    a = r["standings"][0]
    assert a["spread"] == a["places"][4]["at"] - a["places"][0]["at"]
    t = RD.score(CASES["tie"], TEAMS)
    s = {x["id"]: x for x in t["standings"]}
    assert len(s["a"]["places"]) == 7 and len(s["b"]["places"]) == 7
    assert RD.score(CASES["none"], TEAMS)["standings"] == []


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_page_scoring_matches_python(tmp_path):
    # The repo's package.json says "type": "module", so load a copy as CommonJS
    src = tmp_path / "score.cjs"
    src.write_text((ROOT / "xcapp/static/raceday-score.js").read_text())
    run = ("const S = require(process.argv[1]); const cases = JSON.parse(process.argv[2]), teams = JSON.parse(process.argv[3]);"
           "const out = {}; for (const k in cases) { const r = S.score(cases[k], teams);"
           " r.standings.forEach(s => { if (s.sixth === Infinity) s.sixth = null; }); out[k] = r; }"
           "out.pins = [S.pinFromText('https://www.google.com/maps/place/x/@38.6123,-121.3456,17z'), S.pinFromText('38.61, -121.34'),"
           " S.pinFromText('https://maps.google.com/?q=38.5%2C-121.2'), S.pinFromText('hello')];"
           "out.m = Math.round(S.metres({lat: 38.6, lon: -121.3}, {lat: 38.601, lon: -121.3}));"
           "console.log(JSON.stringify(out));")
    js = json.loads(subprocess.run(["node", "-e", run, str(src), json.dumps(CASES), json.dumps(TEAMS)],
                                   capture_output=True, text=True, check=True).stdout)
    for k, case in CASES.items():
        py = RD.score(case, TEAMS)
        for s in py["standings"]:
            if s["sixth"] == float("inf"):
                s["sixth"] = None
        assert js[k] == json.loads(json.dumps(py)), k
    assert js["pins"] == [{"lat": 38.6123, "lon": -121.3456}, {"lat": 38.61, "lon": -121.34}, {"lat": 38.5, "lon": -121.2}, None]
    assert js["m"] == 111


def test_clean_race_and_pins():
    r = RD.clean_race({"name": " Meet ", "teams": TEAMS + [{"id": "a", "name": "dup"}, {"id": "bad id!", "name": "x"}],
                       "checkpoints": ["1 Mile", "", "Finish"], "pins": [{"lat": 38.6, "lon": -121.3}, {"lat": 0, "lon": 0}, "x"],
                       "stream_url": "youtu.be/abc123"})
    assert r["name"] == "Meet" and [t["id"] for t in r["teams"]] == ["a", "b", "c"]
    assert r["checkpoints"] == ["1 Mile", "Finish"] and r["pins"] == [{"lat": 38.6, "lon": -121.3}, None]
    assert r["stream_url"] == "https://youtu.be/abc123"
    with pytest.raises(ValueError):
        RD.clean_race({"teams": TEAMS, "checkpoints": []})
    with pytest.raises(ValueError):
        RD.clean_race({"teams": TEAMS, "checkpoints": ["Finish"], "stream_url": "https://evil.example/x"})
    assert RD.clean_pin({"lat": 91, "lon": 0}) is None and RD.clean_pin({"lat": True, "lon": 1.0}) is None


def login(app, name):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": name, "password": "correct-horse-1"})
    return c


def page_csrf(html):
    m = re.search(r'id="rd-csrf">"([^"]+)"', html) or re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1)


@pytest.fixture
def app():
    from xcapp.db import SessionLocal
    from xcapp.models import Course, StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    for name, role in (("boss", "admin"), ("coach1", "coach"), ("helper", "assistant")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    db.add(Course(name="Gibson Ranch", map_url="https://example.org/map.png"))
    db.commit()
    db.close()
    return create_app()


def make_board(coach):
    home = coach.get("/race-day")
    assert home.status_code == 200 and "Race Day" in home.text
    coach.post("/race-day/new", data={"csrf": page_csrf(home.text), "label": "Clovis Invite"})
    html = coach.get("/race-day").text
    tap = re.search(r'href="/rd/([A-Za-z0-9]+)"', html).group(1)
    watch = re.search(r'href="/rw/([A-Za-z0-9]+)"', html).group(1)
    return tap, watch


def test_full_race_flow(app):
    from xcapp.db import SessionLocal
    from xcapp.models import Course
    coach = login(app, "coach1")
    tap, watch = make_board(coach)
    page = coach.get(f"/rd/{tap}")
    assert page.status_code == 200 and "frame-src https://www.youtube-nocookie.com" in page.headers["content-security-policy"]
    assert "media-src 'self' blob: mediastream:" in page.headers["content-security-policy"]
    cfg = json.loads(re.search(r'id="rd-cfg">(.*?)</script>', page.text).group(1))
    assert cfg["canManage"] is True and cfg["courses"][0]["name"] == "Gibson Ranch"
    csrf = page_csrf(page.text)
    db = SessionLocal()
    cid = db.query(Course).one().id
    db.close()

    # Setting up and running the clock needs the form token
    body = {"name": "Varsity Boys", "teams": TEAMS, "checkpoints": ["1 Mile", "Finish"], "course_id": cid,
            "pins": [{"lat": 38.6, "lon": -121.3}, None], "stream_url": "https://www.youtube.com/live/abcdef123"}
    assert coach.post(f"/rd/{tap}/race", json=body).status_code == 400
    r = coach.post(f"/rd/{tap}/race", json={**body, "csrf": csrf})
    assert r.status_code == 200
    race_id = r.json()["race_id"]
    st = coach.get(f"/rd/{tap}/state").json()
    assert st["race"]["name"] == "Varsity Boys" and st["race"]["pins"] == [{"lat": 38.6, "lon": -121.3}, None]
    assert st["race"]["course"]["map_url"] == "https://example.org/map.png" and st["can_manage"] is True
    assert coach.get(f"/rd/{tap}/state").headers["cache-control"] == "no-store"

    now = st["now"]
    r = coach.post(f"/rd/{tap}/clock", json={"csrf": csrf, "action": "start", "at": now - 500})
    assert r.json()["start_at"] == now - 500
    coach.post(f"/rd/{tap}/clock", json={"csrf": csrf, "action": "nudge", "ms": 1000})
    assert coach.get(f"/rd/{tap}/state").json()["race"]["start_at"] == now + 500
    r = coach.post(f"/rd/{tap}/clock", json={"csrf": csrf, "action": "start", "at": now - 3_600_000})   # way off: server time
    assert abs(r.json()["start_at"] - RD.now_ms()) < 5000

    # A coach at the course, no account: taps saved and replaced whole
    anon = TestClient(app)
    assert anon.get(f"/rd/{tap}").status_code == 200
    log = {"race_id": race_id, "cp": 0, "device": "phone1", "coach": "Coach K", "taps": CASES["basic"]}
    assert anon.post(f"/rd/{tap}/log", json=log).json()["ok"] is True
    assert anon.post(f"/rd/{tap}/log", json={**log, "taps": CASES["basic"][:3]}).status_code == 200
    logs = anon.get(f"/rd/{tap}/state").json()["logs"]
    assert len(logs) == 1 and len(logs[0]["taps"]) == 3 and logs[0]["coach"] == "Coach K"
    assert anon.post(f"/rd/{tap}/log", json={**log, "race_id": "nope"}).status_code == 400
    assert anon.post(f"/rd/{tap}/log", json={**log, "cp": 99}).status_code == 400
    assert anon.post(f"/rd/{tap}/log", json={**log, "taps": [{"id": "x", "at": "soon", "team": "a"}]}).status_code == 400
    # ...but can't change the race or the clock
    assert anon.get(f"/rd/{tap}/state").json()["can_manage"] is False
    assert anon.post(f"/rd/{tap}/clock", json={"csrf": csrf, "action": "clear"}).status_code == 403
    assert anon.post(f"/rd/{tap}/race", json={**body, "csrf": csrf}).status_code == 403

    # Families: read only, by the watch code; the tap code doesn't work there and vice versa
    fam = TestClient(app)
    w = fam.get(f"/rw/{watch}")
    assert w.status_code == 200 and json.loads(re.search(r'id="rd-cfg">(.*?)</script>', w.text).group(1))["mode"] == "watch"
    assert fam.get(f"/rw/{watch}/state").json()["race"]["stream_url"] == "https://www.youtube.com/live/abcdef123"
    assert fam.get(f"/rw/{tap}").status_code == 404 and fam.get(f"/rd/{watch}").status_code == 404
    assert fam.post(f"/rw/{watch}/log", json=log).status_code in (404, 405)

    # Next race, same teams: the first is kept, and late taps for it still save
    r = coach.post(f"/rd/{tap}/race", json={**body, "csrf": csrf, "fresh": True, "name": "JV Boys"})
    new_id = r.json()["race_id"]
    assert new_id != race_id
    st = coach.get(f"/rd/{tap}/state").json()
    assert st["race"]["start_at"] is None and st["logs"] == [] and st["race"]["pins"][0] == {"lat": 38.6, "lon": -121.3}
    assert anon.post(f"/rd/{tap}/log", json=log).status_code == 200
    past = coach.get(f"/rd/{tap}/state?race={race_id}").json()
    assert past["race"]["name"] == "Varsity Boys" and past["race"]["current"] is False and len(past["logs"][0]["taps"]) == 13
    assert "Varsity Boys" in coach.get("/race-day").text

    # New links: the old ones stop working
    html = coach.get("/race-day").text
    bid = re.search(r'action="/race-day/(\d+)/codes"', html).group(1)
    coach.post(f"/race-day/{bid}/codes", data={"csrf": page_csrf(html)})
    assert anon.get(f"/rd/{tap}").status_code == 404 and fam.get(f"/rw/{watch}/state").status_code == 404
    coach.post(f"/race-day/{bid}/delete", data={"csrf": page_csrf(coach.get("/race-day").text)})
    assert "No boards yet" in coach.get("/race-day").text


def test_who_can_manage(app):
    from xcapp import sections as SEC
    coach = login(app, "coach1")
    tap, _ = make_board(coach)
    helper = login(app, "helper")       # an assistant can tap, not run the clock
    assert helper.get("/race-day").status_code == 200
    assert helper.get(f"/rd/{tap}/state").json()["can_manage"] is False
    assert helper.post("/race-day/new", data={"csrf": page_csrf(helper.get("/race-day").text), "label": "x"}).status_code in (302, 303, 403)
    # A coach with Race Day turned off can't manage either
    admin = login(app, "boss")
    page = admin.get("/staff").text
    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    db = SessionLocal()
    cid = db.query(StaffUser).filter_by(username="coach1").one().id
    db.close()
    admin.post("/staff/sections", data={"csrf": page_csrf(page), "users": [str(cid)],
                                        f"show_{cid}": [k for k, _, _ in SEC.SECTIONS if k != "raceday"]})
    assert coach.get("/race-day").status_code == 403
    assert coach.get(f"/rd/{tap}/state").json()["can_manage"] is False
    assert '<a href="/race-day"' not in coach.get("/").text

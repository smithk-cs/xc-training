"""Form drills (drills.py): every prescribed drill has a demonstration, the coaches' and athletes' drill pages, and the
coach's own video links. Names are made up."""
import json
import pathlib
import re

from fastapi.testclient import TestClient

from test_form import sample
from xcapp import drills as DR
from xcapp import form as F
from xcapp.db import SessionLocal
from xcapp.models import Athlete, FormAnalysis, StaffUser
from xcapp.security import hash_password

ANIM = (pathlib.Path(__file__).resolve().parents[1] / "xcapp" / "static" / "drill-anim.js").read_text()


def test_every_prescription_has_drills_and_every_drill_an_animation():
    assert set(DR.FOR_FEEDBACK) <= set(F.FEEDBACK)
    for (measure, state), fb in F.FEEDBACK.items():
        if state != "good" and not fb[3].startswith(("None needed", "Film again")):
            assert DR.FOR_FEEDBACK.get((measure, state)), (measure, state)          # something to show for each problem
    used = {k for keys in DR.FOR_FEEDBACK.values() for k in keys}
    assert used <= set(DR.DRILLS) and set(DR.DRILLS) <= used | {"strides"}
    animated = set(re.findall(r"^    ([a-z_]+): \{ cycle:", ANIM, re.M))
    assert animated == set(DR.DRILLS)
    for d in DR.DRILLS.values():
        assert d["name"] and d["for"] and len(d["steps"]) >= 3 and d["cue"] and d["dose"]
    assert "Lots of up-and-down bounce" in DR.used_for()["low_strides"]


def test_video_links(session):
    DR.set_video(session, "a_skip", "https://youtu.be/example")
    assert DR.videos(session) == {"a_skip": "https://youtu.be/example"}
    for bad in ("http://youtu.be/x", "javascript:alert(1)", "https://user@evil.example/x", "youtube.com/x"):
        try:
            DR.set_video(session, "b_skip", bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    DR.set_video(session, "a_skip", "")
    assert DR.videos(session) == {}


def setup_app():
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="tok-sam")
    b = Athlete(first_name="Ana", last_name="Lee", gender="Girls", grade=10, group="Varsity", link_token="tok-ana")
    db.add_all([a, b])
    db.flush()
    clean = F.clean(sample())
    db.add(FormAnalysis(athlete_id=a.id, by="Coach", pace_s=380, data=json.dumps(clean), score=F.report(clean["metrics"], 380)["score"]))
    db.commit()
    aid = a.id
    db.close()
    from xcapp.web import create_app
    return create_app(), aid, clean


def login(app, name):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": name, "password": "correct-horse-1"})
    return c


def test_drill_pages():
    app, aid, clean = setup_app()
    focus = F.report(clean["metrics"], 380)["focus"]
    first = DR.for_focus(focus[0]["key"], focus[0]["state"])[0]
    coach = login(app, "coach1")
    page = coach.get("/form/drills").text
    assert page.count('class="drill-anim"') == len(DR.DRILLS) and "drill-anim.js" in page
    assert "This season’s focus groups" in page and f'href="#drill-{first["key"]}"' in page and "your own video" in page
    assert 'href="/form/drills"' in coach.get("/form").text
    assert f'/form/drills#drill-{first["key"]}' in coach.get(f"/athletes/{aid}").text          # chips on the athlete page
    assert f'/form/drills#drill-{first["key"]}' in coach.get("/workouts").text                # and the focus groups
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    coach.post("/form/drills/a_skip/video", data={"csrf": csrf, "url": "ftp://nope"})
    assert "should start with https://" in coach.get("/form/drills").text
    coach.post("/form/drills/a_skip/video", data={"csrf": csrf, "url": "https://youtu.be/our-a-skip"})
    assert 'href="https://youtu.be/our-a-skip"' in coach.get("/form/drills").text
    assert coach.post("/form/drills/nope/video", data={"csrf": csrf, "url": ""}).status_code == 404
    trainer = login(app, "trainer1")
    assert trainer.get("/form/drills").status_code == 200 and 'action="/form/drills/' not in trainer.get("/form/drills").text
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', trainer.get("/form/drills").text).group(1)
    assert trainer.post("/form/drills/a_skip/video", data={"csrf": tcsrf, "url": ""}).status_code == 403
    # The athlete: their focus first, with the coach's video, then the rest
    athlete = TestClient(app)
    mine = athlete.get("/a/tok-sam/drills").text
    assert "From your latest running-form video" in mine and focus[0]["summary"] in mine and "More drills" in mine
    assert mine.index(f'id="drill-{first["key"]}"') < mine.index("More drills")
    assert 'href="https://youtu.be/our-a-skip"' in mine and mine.count('class="drill-anim"') >= len(DR.DRILLS)
    assert "Watch how to do my drills" in athlete.get("/a/tok-sam").text
    if clean["metrics"].get("cadence"):
        assert f'data-bpm="{round(clean["metrics"]["cadence"] * 1.05)}"' in mine
    other = athlete.get("/a/tok-ana/drills").text                     # no analysis yet: every drill
    assert "How to do the team’s form drills" in other and other.count('class="drill-anim"') == len(DR.DRILLS)
    assert athlete.get("/a/nope/drills").status_code == 404

import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A


def test_moves_count_the_gain_and_a_new_group():
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, StaffUser, TrainingGroup
    from xcapp.security import hash_password
    from xcapp.web import create_app
    today = A.team_today()
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    groups = {g.name: g for g in db.query(TrainingGroup).all()}
    # Two boys' cutoffs: a VDOT that sits just under the higher one, and one exactly on it
    ranked = sorted(groups.values(), key=lambda g: g.min_vdot_boys)
    low, high = ranked[-2], ranked[-1]
    a = Athlete(first_name="Sam", last_name="Up", gender="Boys", grade=12, group="Varsity", link_token="t-up")
    b = Athlete(first_name="Ben", last_name="Same", gender="Boys", grade=12, group="Varsity", link_token="t-same")
    db.add_all([a, b])
    db.flush()
    start = int(high.min_vdot_boys) - 1
    for x, v in ((a, start), (b, start - 6)):
        A.set_vdot(db, x, v, "coach1", "VDOT page")
    for c in db.query(A.VdotChange).all():
        c.date = today - dt.timedelta(days=20)                 # set weeks ago
    db.flush()
    A.set_vdot(db, a, start + 2, "coach1", "VDOT page")        # +2 and into the higher group
    A.set_vdot(db, b, start - 5, "coach1", "VDOT page")        # +1, same group
    db.commit()
    moves = A.vdot_moves(db, today)
    assert moves[a.id] == {"gain": 2, "from_group": low.name, "to_group": high.name}
    assert moves[b.id]["gain"] == 1 and moves[b.id]["to_group"] == ""
    aid = a.id
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    week = c.get("/week?team=Boys").text
    assert 'title="VDOT up 2 this week"' in week and f"New group this week: {high.name}" in week and ">NEW</span>" in week
    for page in (c.get("/athletes").text, c.get("/vdot").text, c.get(f"/athletes/{aid}").text):
        assert 'title="VDOT up 2 this week"' in page and 'class="pill good group-up"' in page
    me = TestClient(create_app()).get("/a/t-up").text
    assert 'class="hero-move"' in me and f"Up to {high.name}" in me

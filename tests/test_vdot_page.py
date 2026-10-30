"""VDOT from each athlete's most recent race (any distance), coach-set VDOT, and the coaches' VDOT page."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import Activity, Athlete, RaceResult, Result, StaffUser, TrainingGroup
from xcapp.security import hash_password

DAY = dt.date(2026, 10, 25)


@pytest.fixture
def app():
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    sam = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-sam")
    theo = Athlete(first_name="Theo", last_name="Marsh", gender="Boys", grade=11, group="Varsity", link_token="t-theo")
    pat = Athlete(first_name="Pat", last_name="Reed", gender="Boys", grade=9, group="JV", link_token="t-pat")
    db.add_all([sam, theo, pat])
    db.flush()
    # Sam: a fast 5K in September, then a slower 4K most recently: the 4K sets his VDOT.
    db.add(Result(athlete_id=sam.id, date=dt.date(2026, 9, 12), event="5K", time_s=15 * 60 + 2, kind="race", season="2026 XC"))
    db.add(RaceResult(athlete_id=sam.id, date=dt.date(2026, 10, 17), meet="Oak Hills", race="Boys Varsity", distance_m=4000,
                      distance_label="4,000 Meters", time_s=12 * 60 + 47.9, place=41))
    # Theo: only a 5K.
    db.add(Result(athlete_id=theo.id, date=dt.date(2026, 10, 10), event="5K", time_s=16 * 60 + 31.7, kind="race", season="2026 XC"))
    db.commit()
    ids = (sam.id, theo.id, pat.id)
    db.close()
    from xcapp.web import create_app
    return create_app(), ids


def login(app, user):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": user, "password": "correct-horse-1"})
    return c


def test_most_recent_race_sets_vdot(app):
    _, (sam, theo, pat) = app
    db = SessionLocal()
    ab = A.abilities(db, DAY)
    assert A.vdot(5000, 902) > A.vdot(4000, 767.9)                       # the older 5K was the better race...
    assert ab[sam]["vdot"] == A.team_vdot(A.vdot(4000, 767.9), A.vdot_targets(db))   # ...but the latest race counts
    assert ab[sam]["source"] == "Oak Hills, 4,000 Meters 12:47.9 on Oct 17, 2026" and ab[sam]["group"] == "Silver"
    assert ab[theo]["source"].startswith("5K 16:31.7") and ab[pat]["vdot"] is None
    db.get(Athlete, theo).vdot_override = 70                              # coach-set VDOT wins and moves the group
    db.flush()
    ab = A.abilities(db, DAY)
    assert (ab[theo]["vdot"], ab[theo]["vdot_set"], ab[theo]["group"]) == (70, True, "Gold") and ab[theo]["race_vdot"] < 70
    db.rollback()
    db.close()


def test_paces_and_plans():
    assert [A.fmt_time(A.pace_at(v, A.EASY_PCT))[:-2] for v in (73, 68)] == ["6:17", "6:39"]       # the team sheet's values
    assert [A.fmt_time(A.pace_at(v, A.THRESHOLD_PCT))[:-2] for v in (72, 68)] == ["5:05", "5:20"]
    assert [A.weekly_plan_text(x) for x in ("7x8", "6 X 6", "7x8=56", "inj", "")] == ["7x8=56", "6x6=36", "7x8=56", "inj", ""]


def test_vdot_page_and_editing(app):
    web, (sam, theo, pat) = app
    c = login(web, "coach1")
    page = c.get("/vdot").text
    assert '<a href="/vdot" aria-current="page">VDOT</a>' in page
    assert '<th class="name">Gold</th><th>VDOT 68-72</th>' in page and "easy: 6:21-6:39" in page and "threshold: 5:05-5:20" in page
    assert "marathon: 5:23-5:39" in page and "interval: 4:41-4:54" in page and "repetition: 4:21-4:33" in page   # Gold's other ranges
    assert "long run" not in page and "tempo:" not in page and "Paste a VDOT list" not in page
    sam_paces = [A.fmt_time(z["mile"])[:-2] for z in A.training_paces(A.abilities(SessionLocal(), A.team_today())[sam]["vdot"])]
    assert "".join(f'<td class="pace">{x}</td>' for x in sam_paces) in page          # each athlete's own paces
    assert "<th>VDOT 73 +</th>" in page and "<th>VDOT 48 and below</th>" in page and "or slower" in page
    assert page.index("Platinum") < page.index("Gold") < page.index("Silver")
    assert "No VDOT yet" in page and "Pat Reed" in page
    edit = c.get("/vdot?edit=1").text
    assert f'name="vdot_{sam}"' in edit and f'name="plan_{sam}"' not in edit and "Weekly plan" not in edit
    assert '<button type="submit" form="vdot-form">Save and finish</button>' in edit and ">Done<" not in edit
    csrf = re.search(r'name="csrf" value="([^"]+)"', edit).group(1)
    data = {"csrf": csrf}
    for aid in (sam, theo, pat):
        data.update({f"vdot_{aid}": ""})
    data.update({f"vdot_{pat}": "55"})
    assert c.post("/vdot", data=data).headers["location"] == "/vdot?team=Boys"
    page = c.get("/vdot").text
    assert "Weekly plan" not in page and 'class="mark"' not in page
    assert "Set by coach" not in page and "No VDOT yet" not in page                   # Pat now has a VDOT of 55
    assert "Oak Hills" not in page and "Most recent race" not in page                    # no meet names or race times
    assert page.count('<colgroup><col class="name"><col class="v">') == page.count('<table class="vdot-group"')   # columns line up
    data[f"vdot_{theo}"] = "200"
    c.post("/vdot", data=data)
    assert "not changed for Theo Marsh" in c.get("/vdot?edit=1").text

    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/athletes/{sam}").text).group(1)
    c.post(f"/athletes/{sam}/vdot", data={"csrf": csrf, "vdot": "74"})
    assert "VDOT 74, set by a coach" in c.get(f"/athletes/{sam}").text
    c.post(f"/athletes/{sam}/vdot", data={"csrf": csrf, "vdot": ""})
    assert "worked out from their most recent race" in c.get(f"/athletes/{sam}").text
    me = TestClient(web).get("/a/t-sam").text
    assert "From your most recent race (Oak Hills" in me and 'My training paces <span class="muted"' in me
    assert all(f">{x}<small" in me for x in sam_paces) and " s per 400</span>" in me

    t = login(web, "trainer1")
    assert "Edit VDOT" not in t.get("/vdot").text and 'name="vdot_' not in t.get("/vdot?edit=1").text
    tcsrf = re.search(r'name="csrf" value="([^"]+)"', t.get("/vdot").text).group(1)
    assert t.post("/vdot", data={"csrf": tcsrf}).status_code == 403


def test_boys_and_girls_pages(app):
    web, (sam, theo, pat) = app
    db = SessionLocal()
    ava = Athlete(first_name="Ava", last_name="Cole", gender="Girls", grade=10, group="Varsity", link_token="t-ava", vdot_override=60)
    db.add(ava)
    db.flush()
    gold = db.query(TrainingGroup).filter_by(name="Gold").one()
    silver = db.query(TrainingGroup).filter_by(name="Silver").one()
    gold.weekly_miles, gold.weekly_miles_girls = 45, 0
    silver.min_vdot_girls, silver.weekly_miles, silver.weekly_miles_girls = 56, 40, 32   # girls reach Silver from VDOT 56
    db.query(TrainingGroup).filter_by(name="Bronze").one().min_vdot_girls = 52
    monday = A.week_monday(A.team_today())
    db.add(Activity(athlete_id=ava.id, source="upload", external_id="ava1", start_utc=dt.datetime(monday.year, monday.month, monday.day, 15),
                    local_date=monday, duration_s=3000, distance_m=8 * 1609.344, sport="running"))
    ava_id = ava.id
    db.commit()
    db.close()
    c = login(web, "coach1")

    boys = c.get("/vdot").text                                                         # boys by default
    assert "Boys VDOT training groups" in boys and "Ava Cole" not in boys and "Sam Lowry" in boys
    assert '<a class="button" href="/vdot?team=Boys" aria-current="page">Boys</a>' in boys
    assert "Gold<br><span class=\"small\" style=\"font-weight:400\">45 mi a week</span>" in boys
    assert "Silver<br><span class=\"small\" style=\"font-weight:400\">40 mi a week</span>" in boys

    girls = c.get("/vdot?team=Girls").text
    assert "Girls VDOT training groups" in girls and "Ava Cole" in girls and f'href="/athletes/{sam}"' not in girls
    assert '<a class="button" href="/vdot?team=Girls" aria-current="page">Girls</a>' in girls
    assert "<th>VDOT 56-67</th>" in girls and "32 mi a week" in girls and "45 mi a week" not in girls   # girls' cutoffs and goals
    assert girls.index("Silver") < girls.index("Ava Cole") < girls.index("Bronze")

    edit = c.get("/vdot?team=Girls&edit=1").text
    assert f'name="vdot_{ava_id}"' in edit and f'name="vdot_{sam}"' not in edit and 'name="team" value="Girls"' in edit
    csrf = re.search(r'name="csrf" value="([^"]+)"', edit).group(1)
    assert c.post("/vdot", data={"csrf": csrf, "team": "Girls", f"vdot_{ava_id}": "58"}).headers["location"] == "/vdot?team=Girls"
    girls = c.get("/vdot?team=Girls").text
    assert "Ava Cole" in girls[girls.index("VDOT changes"):] and f"/athletes/{pat}" not in girls   # changes listed per team
    assert c.post("/vdot/import", data={"csrf": csrf, "team": "Girls", "text": "Ava Cole\t59"}).headers["location"] == "/vdot?team=Girls"

    # The Groups page keeps a weekly goal for each, and athletes see their own team's goal.
    boys = c.get("/groups").text                                                       # boys by default
    assert "Boys training groups" in boys and '<a class="button" href="/groups?team=Boys" aria-current="page">Boys</a>' in boys
    assert f'name="miles_{silver.id}" value="40"' in boys and "miles_girls_" not in boys and "girls_" not in boys
    page = c.get("/groups?team=Girls").text
    assert "Girls training groups" in page and f'name="miles_girls_{silver.id}" value="32"' in page and f'name="girls_{silver.id}" value="56"' in page
    assert f'name="miles_{silver.id}"' not in page and f'name="boys_{silver.id}"' not in page
    assert "Girls weekly mileage" in page and "8.0 / 32" in page

    # Saving the girls' page changes only the girls' numbers; a group added there starts the same for the boys.
    db = SessionLocal()
    groups = db.query(TrainingGroup).all()
    form = {"csrf": csrf, "team": "Girls", "lookback": "365", "show_athletes": "on", "new_name": "Diamond", "new_girls": "64", "new_miles_girls": "50"}
    for g in groups:
        form.update({f"name_{g.id}": g.name, f"position_{g.id}": str(g.position), f"color_{g.id}": g.color,
                     f"girls_{g.id}": str(int(g.min_vdot_girls)), f"miles_girls_{g.id}": "%g" % g.weekly_miles_girls if g.weekly_miles_girls else ""})
    db.close()
    form[f"girls_{silver.id}"], form[f"miles_girls_{silver.id}"] = "55", "30"
    assert c.post("/groups", data=form).headers["location"] == "/groups?team=Girls"
    db = SessionLocal()
    s2 = db.get(TrainingGroup, silver.id)
    assert (s2.min_vdot_boys, s2.weekly_miles, s2.min_vdot_girls, s2.weekly_miles_girls) == (62, 40, 55, 30)
    d = db.query(TrainingGroup).filter_by(name="Diamond").one()
    assert (d.min_vdot_boys, d.min_vdot_girls, d.weekly_miles, d.weekly_miles_girls) == (64, 64, 50, 50)
    db.close()
    page = c.get("/groups?team=Girls").text
    assert "8.0 / 30" in page
    me = TestClient(web).get("/a/t-ava").text
    assert "the Silver goal" in me and "22.0" in me                                     # 30 - 8 miles left
    db = SessionLocal()
    assert A.mileage_goal(db, ava_id, "Silver", A.team_today(), "Girls")["goal"] == 30
    assert A.mileage_goal(db, ava_id, "Silver", A.team_today(), "Boys")["goal"] == 40
    assert A.mileage_goal(db, ava_id, "Gold", A.team_today(), "Girls") is None         # no girls' goal for Gold
    db.close()

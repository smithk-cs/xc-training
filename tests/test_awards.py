"""Season awards: MVP (lowest average place) and most improved, by class, for athletes who ran 80% of their class's meets."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import awards as AW
from xcapp import seasons as SE
from xcapp.models import Athlete, RaceResult, StaffUser
from xcapp.security import hash_password

TODAY = dt.date(2026, 11, 11)
MEETS = [dt.date(2026, 9, 5), dt.date(2026, 9, 12), dt.date(2026, 9, 26), dt.date(2026, 10, 10), dt.date(2026, 10, 24)]


def runner(db, first, grade, gender="Boys"):
    a = Athlete(first_name=first, last_name="Test", gender=gender, grade=grade, grad_year=2027 + 12 - grade, group="",
                link_token=f"tok-{first}")
    db.add(a)
    db.flush()
    return a


def race(db, a, i, time_s, place, dist=5000, label="5K", division="Varsity"):
    db.add(RaceResult(athlete_id=a.id, meet=f"Meet {i + 1}", date=MEETS[i], race=division, distance_m=dist,
                      distance_label=label, time_s=time_s, place=place))


def build(db):
    # Varsity: five meets
    steady = runner(db, "Steady", 12)          # places 3 every time, no faster
    climber = runner(db, "Climber", 11)        # 19:00 to 17:30
    winner = runner(db, "Winner", 12)          # 1st every time, but missed two meets (60%): not counted
    other = runner(db, "Other", 11)
    for i in range(5):
        race(db, steady, i, 1000, 3)
        race(db, climber, i, 1140 - 22 * i, 8 - i)
        race(db, other, i, 1100, 6)
    for i in (0, 1, 4):
        race(db, winner, i, 930, 1)
    # Freshmen: only three meets (no frosh race at the last two), two-mile races early and a 5K later
    frosh = runner(db, "Frosh", 9)
    frosh2 = runner(db, "Frosh2", 9)
    race(db, frosh, 0, 780, 12, dist=3218.69, label="2 Miles", division="Frosh/Soph")
    race(db, frosh, 1, 770, 10, dist=3218.69, label="2 Miles", division="Frosh/Soph")
    race(db, frosh, 2, 1150, 9, division="Frosh/Soph")
    race(db, frosh2, 0, 800, 15, dist=3218.69, label="2 Miles", division="Frosh/Soph")
    race(db, frosh2, 1, 805, 14, dist=3218.69, label="2 Miles", division="Frosh/Soph")
    race(db, frosh2, 2, 1320, 20, division="Frosh/Soph")          # slower: not on the most improved list
    # A girl, and a race from last season: neither shows on the boys' 2026 board
    girl = runner(db, "Girl", 12, "Girls")
    race(db, girl, 0, 1200, 1)
    db.add(RaceResult(athlete_id=steady.id, meet="Old", date=dt.date(2025, 9, 6), distance_m=5000, time_s=1300, place=40))
    db.flush()


def test_awards_by_class(session):
    build(session)
    b = AW.awards(session, SE.period(2026, "xc"), "Boys", TODAY)
    v = b["varsity"]
    assert v["meets"] == 5 and v["need"] == 4 and v["eligible"] == 3
    assert [e["a"].first_name for e in v["short"]] == ["Winner"]
    assert [m["a"].first_name for m in v["mvp"]] == ["Steady", "Climber", "Other"]          # avg 3, 6, 6 (Climber faster)
    assert v["mvp"][0]["avg_place"] == 3 and v["mvp"][1]["avg_place"] == 6
    assert [m["a"].first_name for m in v["improved"]] == ["Climber"]                          # the others didn't get faster
    c = v["improved"][0]                       # 19:00 → 17:32 over five 5Ks: the trend through all five, 27 s a mile
    assert c["n"] == 5 and round(c["gain"]) == 27 and not c["mixed"] and c["p_from"] > c["p_to"]
    f = b["freshmen"]
    assert f["meets"] == 3 and f["need"] == 3 and f["eligible"] == 2                          # held to their own meets
    assert [m["a"].first_name for m in f["mvp"]] == ["Frosh", "Frosh2"]
    fi = f["improved"]                         # two 2-mile races, then a 5K: the 2-milers as the 5K pace they're worth
    assert [m["a"].first_name for m in fi] == ["Frosh"] and round(fi[0]["gain"]) == 32 and fi[0]["mixed"] and fi[0]["n"] == 3
    assert AW.trend([(0, 400), (10, 390), (20, 380)]) == (-1.0, 400.0) and AW.trend([(0, 400), (0, 390)]) is None
    # The coach picks the meets: Meet 2 to Meet 4 for varsity; the freshmen didn't race Meet 4
    meets = AW.season_meets(session, SE.period(2026, "xc"), "Boys", TODAY)
    assert [m[1] for m in meets] == ["Meet 1", "Meet 2", "Meet 3", "Meet 4", "Meet 5"]
    picked = AW.awards(session, SE.period(2026, "xc"), "Boys", TODAY, (meets[1], meets[3]))
    pv = picked["varsity"]                     # only Meets 2 to 4 count: 3 meets, all 3 needed
    assert pv["span"] == (meets[1], meets[3]) and pv["meets"] == 3 and pv["need"] == 3 and round(pv["improved"][0]["gain"]) == 14
    assert pv["improved"][0]["n"] == 3 and [e["a"].first_name for e in pv["short"]] == ["Winner"]
    assert picked["freshmen"]["span"] == (meets[1], None) and picked["freshmen"]["improved"] == []
    assert AW.parse_meet(AW.meet_key(meets[2])) == meets[2] and AW.parse_meet("junk") is None
    assert b["sophomores"]["meets"] == 0
    girls = AW.awards(session, SE.period(2026, "xc"), "Girls", TODAY)
    assert girls["varsity"]["eligible"] == 1 and girls["varsity"]["mvp"][0]["a"].first_name == "Girl"
    assert AW.grade_in(session.get(Athlete, 1), SE.period(2025, "xc")["start"], TODAY) == 11   # a year earlier: a grade lower


def test_awards_page(session):
    build(session)
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert 'href="/races/awards"' in c.get("/races").text
    page = c.get("/races/awards?season=2026").text
    assert "Boys season awards" in page and "MVP: top 5 scorers" in page and "Most improved: top 5" in page
    for name in ("Freshmen", "Sophomores", "Varsity"):
        assert f"<h2" in page and f">{name}</h2>" in page
    assert "Steady Test" in page and "1 not counted" in page and "Winner Test (3)" in page
    assert "Girl Test" not in page and "Girl Test" in c.get("/races/awards?team=Girls&season=2026").text
    assert c.get("/races/awards?season=1999").status_code == 200
    picked = c.get("/races/awards?season=2026&start_meet=2026-09-12|Meet 2&end_meet=2026-10-10|Meet 4").text
    assert 'value="2026-09-12|Meet 2" selected' in picked and "Sep 12 Meet 2 to Oct 10 Meet 4: 3 meets" in picked
    assert "14 s/mi" in picked and "Freshmen didn’t race at the last meet picked" in picked
    assert "(5K pace)" in page and "27 s/mi" in page and "5 races:" in page                              # unknown season: the latest

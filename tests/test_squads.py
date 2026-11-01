import datetime as dt
import re
import statistics

from xcapp import points as PT
from xcapp import squads as SQ
from xcapp.models import Athlete, RaceResult, SquadMember, WeekResult

MON = dt.date(2026, 10, 12)


def test_balance_even_sizes_and_strength():
    import random
    rnd = random.Random(4)
    strength = {i: rnd.gauss(0, 1) for i in range(1, 30)}
    squads = SQ.balance(strength, 4)
    assert sorted(len(s) for s in squads) == [7, 7, 7, 8] and sorted(sum(squads, [])) == list(range(1, 30))
    means = [statistics.fmean(strength[i] for i in s) for s in squads]
    assert max(means) - min(means) < 0.05
    top4 = sorted(strength, key=lambda i: -strength[i])[:4]
    assert all(sum(1 for i in s if i in top4) == 1 for s in squads)   # the best runners are spread out


def test_split_balances_each_gender():
    from types import SimpleNamespace
    people = [SimpleNamespace(id=i, gender="Boys" if i <= 13 else "Girls") for i in range(1, 22)]
    strength = {p.id: (14 - p.id) / 4 if p.gender == "Boys" else (22 - p.id) / 3 for p in people}
    squads = SQ.split_by_gender(people, strength, 3)
    boys = [[i for i in s if i <= 13] for s in squads]
    girls = [[i for i in s if i > 13] for s in squads]
    assert sorted(len(b) for b in boys) == [4, 4, 5] and sorted(len(g) for g in girls) == [2, 3, 3]
    assert sorted(len(s) for s in squads) == [7, 7, 7]
    for part in (boys, girls):
        means = [statistics.fmean(strength[i] for i in b) for b in part]
        assert max(means) - min(means) < 0.35


def test_rank_points_and_race_points():
    assert SQ._rank_points({1: 90, 2: 80, 3: 90, 4: None}, 3) == {1: 2.5, 3: 2.5, 2: 1}
    rr = lambda aid, t, race="V": RaceResult(athlete_id=aid, meet="Meet", date=MON, race=race, distance_m=5000, time_s=t)  # noqa: E731
    squad_of = {1: 10, 2: 10, 3: 20, 4: 20, 5: 20}
    total, detail = SQ.race_points([rr(1, 900), rr(3, 910), rr(2, 920), rr(4, 930), rr(5, 950, "JV"), rr(99, 800)], squad_of)
    # Varsity: 1 beats 3 and 4 (2), 3 beats 2 (1), 2 beats 4 (1); JV alone: 0. Athlete 99 is on no squad.
    assert total == {10: 3, 20: 1} and [d["runners"] for d in detail] == [1, 4]


def make_team(db, n=8):
    out = []
    for i in range(n):
        a = Athlete(first_name=f"R{i}", last_name=f"Runner{i}", gender="Boys", grade=10, group="Varsity", link_token=f"sq-{i}",
                    vdot_override=40 + i)
        db.add(a)
        out.append(a)
    db.flush()
    return out


def test_standings_from_saved_weeks_and_races(session, monkeypatch):
    athletes = make_team(session, 4)
    monkeypatch.setattr(PT, "team_week", lambda db, monday, gender=None, athletes=None: [
        {"a": a, "miles": 0, "goal_miles": 30, "points": 0, "target": 100, "score": 0, "score_pct": None} for a in athletes or []])
    comp = SQ.create(session, "Fall", "Boys", 2, MON, None, MON + dt.timedelta(days=9))
    assert sorted(len(s.members) for s in comp.squads) == [2, 2]
    red, gold = comp.squads
    on = {m.athlete_id: s for s in comp.squads for m in s.members}
    # The finished week: Red's runners hit 100% and 90% of miles; Gold's 80% and 120% (counted as 110%).
    miles = {}
    for s, vals in ((red, (30, 27)), (gold, (24, 36))):
        for m, mi in zip(s.members, vals):
            miles[m.athlete_id] = mi
            session.add(WeekResult(athlete_id=m.athlete_id, monday=MON, miles=mi, goal_miles=30, points=50, target=100,
                                   score=50, medal="", computed_on=MON + dt.timedelta(days=9)))
    first_red = red.members[0].athlete_id
    session.add(RaceResult(athlete_id=first_red, meet="Inv", date=MON + dt.timedelta(days=5), race="V", distance_m=5000, time_s=900))
    session.add(RaceResult(athlete_id=gold.members[0].athlete_id, meet="Inv", date=MON + dt.timedelta(days=5), race="V", distance_m=5000, time_s=950))
    session.flush()
    st = SQ.standings(session, comp, MON + dt.timedelta(days=9))
    week = next(w for w in st["weeks"] if w["monday"] == MON)
    assert week["done"] and round(week["rows"][red.id]["miles_pct"]) == 95 and round(week["rows"][gold.id]["miles_pct"]) == 95
    assert week["rows"][red.id]["team_points"] == {"miles": 1.5, "points": 1.5, "races": 2}
    assert week["rows"][gold.id]["total"] == 1.5 + 1.5 + 1
    assert st["totals"][red.id]["team_points"] == 5 and st["table"][0].id == red.id
    current = st["weeks"][0]
    assert not current["done"] and current["monday"] == MON + dt.timedelta(days=7)
    assert on[first_red] is red


def test_squads_pages(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp import config
    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    monkeypatch.setenv("AS_OF_DATE", "2026-10-21")
    config.reload_settings()
    db = SessionLocal()
    db.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    ids = [a.id for a in make_team(db, 9)]
    db.commit()
    db.close()
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    page = c.get("/squads").text
    assert "New squad competition" in page and 'href="/squads"' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/squads/new", data={"csrf": csrf, "name": "Fall squads", "gender": "Boys", "squads": "3", "start": "2026-10-14"})
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    page = c.get(f"/squads/{cid}").text
    assert "Fall squads" in page and "Standings" in page and "Week of Oct 19" in page and "Week of Oct 12" in page
    assert page.count('class="squad-dot"') >= 6
    db = SessionLocal()
    m = db.query(SquadMember).filter_by(athlete_id=ids[0]).one()
    other = next(s for s in {x.squad_id for x in db.query(SquadMember)} if s != m.squad_id)
    db.close()
    c.post(f"/squads/{cid}/move", data={"csrf": csrf, "athlete_id": str(ids[0]), "squad_id": str(other)})
    c.post(f"/squads/{cid}/move", data={"csrf": csrf, "athlete_id": str(ids[1]), "squad_id": ""})
    db = SessionLocal()
    assert db.query(SquadMember).filter_by(athlete_id=ids[0]).one().squad_id == other
    assert db.query(SquadMember).filter_by(athlete_id=ids[1]).count() == 0
    db.close()
    assert "Not on a squad" in c.get(f"/squads/{cid}").text
    c.post(f"/squads/{cid}/rebalance", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(SquadMember).count() == 9
    db.close()
    r = c.post("/squads/new", data={"csrf": csrf, "name": "Too many", "gender": "Girls", "squads": "3", "start": "2026-10-14"})
    assert "enough athletes" in c.get("/squads?list=1").text
    c.post(f"/squads/{cid}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(SquadMember).count() == 0
    db.close()

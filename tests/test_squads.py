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


def test_race_scoring_like_cross_country():
    rr = lambda aid, t: RaceResult(athlete_id=aid, meet="Meet", date=MON, race="V", distance_m=5000, time_s=t)  # noqa: E731
    squad_of = {1: 10, 2: 10, 3: 20, 4: 20, 5: 30}
    order = SQ.score_race([rr(1, 900), rr(3, 910), rr(2, 920), rr(4, 930), rr(5, 940), rr(99, 800)], squad_of, 2)
    # places among squad runners: 1 (10), 2 (20), 3 (10), 4 (20), 5 (30); athlete 99 is on no squad
    assert [(x["squad_id"], x["score"], x["complete"]) for x in order] == [(10, 4, True), (20, 6, True), (30, 5, False)]
    same = lambda a, b: (a["complete"], a["score"]) == (b["complete"], b["score"])  # noqa: E731
    assert SQ._award(order, [40, 25, 10, 5], same) == {10: 40, 20: 25, 30: 10}
    tie = [{"squad_id": 1, "complete": True, "score": 10}, {"squad_id": 2, "complete": True, "score": 10}, {"squad_id": 3, "complete": True, "score": 12}]
    assert SQ._award(tie, [40, 25, 10], same) == {1: 32.5, 2: 32.5, 3: 10}


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
    import json
    athletes = make_team(session, 4)
    monkeypatch.setattr(PT, "team_week", lambda db, monday, gender=None, athletes=None: [
        {"a": a, "miles": 0, "goal_miles": 30, "points": 0, "target": 100, "score": 0, "run_days": 0} for a in athletes or []])
    today = MON + dt.timedelta(days=9)
    comp = SQ.create(session, "Fall", "Boys", 2, MON, None, today)
    red, gold = comp.squads
    # The finished week: Red 30 and 27 miles of 30 (both at 90% or more), 6 and 5 days; Gold 26 and 36, 6 and 6 days
    for s, vals in ((red, ((30, 6), (27, 5))), (gold, ((26, 6), (36, 6)))):
        for m, (mi, days) in zip(s.members, vals):
            session.add(WeekResult(athlete_id=m.athlete_id, monday=MON, miles=mi, goal_miles=30, points=95, target=100,
                                   score=50, run_days=days, medal="", computed_on=today))
    race = lambda m, t: RaceResult(athlete_id=m.athlete_id, meet="Inv", date=MON + dt.timedelta(days=5), race="V", distance_m=5000, time_s=t)  # noqa: E731
    session.add_all([race(red.members[0], 900), race(gold.members[0], 905), race(red.members[1], 910), race(gold.members[1], 990)])
    session.flush()
    comp.scoring = json.dumps({**SQ.DEFAULT_SCORING, "race_scorers": 2})
    st = SQ.standings(session, comp, today)
    week = next(w for w in st["weeks"] if w["monday"] == MON)
    assert week["rows"][red.id]["parts"] == {"race": 40, "miles": 2, "days": 1, "points": None}   # race 1+3=4 beats 2+4=6
    assert week["rows"][gold.id]["parts"] == {"race": 25, "miles": 1, "days": 2, "points": None}
    assert st["totals"][red.id]["total"] == 43 and st["table"][0].id == red.id
    assert [k for k, _ in st["categories"]] == ["race", "miles", "days"]
    # Leave the race out, count practice points at 1 point each
    key = SQ.race_key(MON + dt.timedelta(days=5), "Inv", "V")
    comp.scoring = json.dumps({**SQ.DEFAULT_SCORING, "excluded_races": [key], "points_on": True, "points_pts": 2})
    st = SQ.standings(session, comp, today)
    week = next(w for w in st["weeks"] if w["monday"] == MON)
    assert week["rows"][red.id]["parts"] == {"race": 0, "miles": 2, "days": 1, "points": 4} and not week["races"][0]["counts"]
    assert st["totals"][red.id]["total"] == 7
    assert [r["counts"] for r in SQ.completed_races(session, comp, today)] == [False]


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
    form = {"csrf": csrf, "race_on": "1", "miles_on": "1", "race_place_0": "50", "race_place_1": "30", "race_place_2": "10",
            "race_scorers": "4", "miles_pct": "85", "miles_pts": "2", "days_min": "5", "days_pts": "1", "points_pct": "90", "points_pts": "1"}
    c.post(f"/squads/{cid}/scoring", data=form)
    page = c.get(f"/squads/{cid}").text
    assert "Saved the scoring" in page and "85% of miles goal" in page and "5+ days run" not in page and 'value="50"' in page
    c.post(f"/squads/{cid}/scoring", data=dict(form, days_min="9", days_on="1"))
    assert "Days run: use 1 to 7." in c.get(f"/squads/{cid}").text
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

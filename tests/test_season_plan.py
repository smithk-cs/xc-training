"""Season plan: Daniels' phases by week, peak miles and the week's goals, and quality sessions sized to his limits."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp import limits as LM
from xcapp import season_plan as SP
from xcapp import seasons as SE
from xcapp.models import PlannedWorkout, SeasonWeek, StaffUser, TrainingGroup
from xcapp.security import hash_password


def test_default_plans():
    xc = SP.default_plan({"kind": "xc", "start": dt.date(2026, 8, 3), "end": dt.date(2026, 12, 5)})
    assert len(xc) == 18 and [p for p, _ in xc] == [2] * 4 + [3] * 7 + [4] * 7                 # II, then III, then IV
    assert [pct for _, pct in xc][-3:] == [100, 85, 70]                                         # a two-week taper
    base = SP.default_plan({"kind": "summer", "start": dt.date(2026, 6, 7), "end": dt.date(2026, 8, 2)})
    assert [p for p, _ in base] == [1] * 6 + [2] * 3 and [pct for _, pct in base] == [70, 70, 70, 75, 75, 75, 80, 80, 80]


def test_sessions_fit_daniels_limits():
    for weekly in (15, 25, 40, 55):
        for phase in (2, 3, 4):
            for s in SP.suggestions(phase, weekly):
                miles = LM.zone_miles(s["parts"], 50)
                for zone, mi in miles.items():
                    if zone in LM.LIMITS:
                        share, cap = LM.LIMITS[zone]
                        lim = min(weekly * share, cap) if cap else weekly * share
                        assert mi <= lim + 1e-6, (weekly, phase, s["title"], mi, lim)          # always within the limit
    assert [s["key"] for s in SP.suggestions(1, 30)] == ["strides", "hills"] and SP.suggestions(3, 0) == []
    small = {s["key"]: s["title"] for s in SP.suggestions(3, 15)}                               # a 15-mile week: shorter reps
    assert small == {"i": "I: 3 × 600 m", "t": "T: 2 × 1200 m cruise"}
    assert SP.suggestions(3, 40)[0]["title"] == "I: 5 × 1000 m" and SP.suggestions(2, 40)[0]["title"] == "R: 16 × 200 m"


def login(app, name="coach1"):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": name, "password": "correct-horse-1"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/workouts").text).group(1)   # a new token after signing in


def test_plan_page_fill_apply_and_suggest(session):
    for g in session.query(TrainingGroup).all():
        g.weekly_miles, g.weekly_miles_girls = {"Gold": 40, "Silver": 30}.get(g.name, 0), {"Gold": 36, "Silver": 30}.get(g.name, 0)
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    session.add(StaffUser(username="trainer1", role="trainer", password_hash=hash_password("correct-horse-1")))
    session.commit()
    from xcapp.web import create_app
    app = create_app()
    c, csrf = login(app)
    today = A.team_today()
    season = SE.period_for(today)
    key = f"{season['school_year']}-{season['kind']}"
    page = c.get("/workouts/plan").text
    assert "Season plan" in page and "Fill in Daniels’ plan" in page and "has no plan yet" in page
    assert "No season plan for this week yet" in c.get("/workouts").text
    c.post("/workouts/plan", data={"csrf": csrf, "season": key, "action": "fill"})
    n = session.query(SeasonWeek).count()
    assert n == len(SP.weeks_of(season))
    assert SP.peaks(session)["Gold"] == {"Boys": 40.0, "Girls": 36.0}                         # peaks kept from the goals
    # Edit this week: Phase III at 90%
    monday = A.week_monday(today).isoformat()
    form = {"csrf": csrf, "season": key, "action": "save"}
    for w in SP.weeks(session, season):
        k = w["monday"].isoformat()
        form[f"phase_{k}"], form[f"pct_{k}"] = str(w["row"].phase), str(w["row"].miles_pct)
    form[f"phase_{monday}"], form[f"pct_{monday}"], form[f"note_{monday}"] = "3", "90", "Hard week"
    c.post("/workouts/plan", data=form)
    session.expire_all()
    sw = SP.week_of(session, today)
    assert (sw.phase, sw.miles_pct, sw.note) == (3, 90, "Hard week")
    page = c.get(f"/workouts/plan?season={key}&week={monday}").text
    assert "Phase III · Transition quality" in page and "90% of peak miles" in page and "Hard week" in page
    assert "I: 4 × 1000 m" in page and "T: 3 × 1 mi cruise" in page                           # Gold boys at 36 mi
    assert "don’t match this week’s plan" in page
    banner = c.get("/workouts").text
    assert "This week: Phase III · Transition quality" in banner
    # Apply: the groups' weekly goals become 90% of the peaks; the peaks stay
    c.post("/workouts/plan/apply", data={"csrf": csrf, "week": monday, "season": key})
    session.expire_all()
    gold = session.query(TrainingGroup).filter_by(name="Gold").one()
    assert (gold.weekly_miles, gold.weekly_miles_girls) == (36, 32.5) and SP.peaks(session)["Gold"]["Boys"] == 40
    assert "goals match this week’s plan" in c.get(f"/workouts/plan?season={key}&week={monday}").text
    # A suggested session opens in the plan form, for that group
    form_page = c.get(f"/workouts?suggest=i&group=Gold&team=Boys&week={monday}").text
    assert "Suggested: I: 4 × 1000 m · Gold" in form_page and 'name="group_Gold" checked' in form_page
    assert 'name="team_Boys" checked' in form_page and 'value="jog as long as the rep"' in form_page
    assert "Plan a workout" in c.get("/workouts?suggest=nope&group=Gold&week=" + monday).text   # unknown: an empty form
    # Peaks can be changed; a week can be cleared
    c.post("/workouts/plan", data={"csrf": csrf, "season": key, "action": "peaks", "peak_Gold_Boys": "50", "peak_Gold_Girls": "44"})
    session.expire_all()
    session.info.pop("cache", None)                                    # this test's session keeps settings it already read
    assert SP.peaks(session)["Gold"] == {"Boys": 50.0, "Girls": 44.0}
    form[f"phase_{monday}"] = ""
    c.post("/workouts/plan", data=form)
    session.expire_all()
    assert SP.week_of(session, today) is None and session.query(SeasonWeek).count() == n - 1
    # A trainer sees the plan but can't change it
    t, tcsrf = login(app, "trainer1")
    tp = t.get("/workouts/plan").text
    assert "Season plan" in tp and "Save the plan" not in tp
    assert t.post("/workouts/plan", data={"csrf": tcsrf, "season": key, "action": "fill"}).status_code == 403
    assert session.query(PlannedWorkout).count() == 0                                          # nothing posted on its own

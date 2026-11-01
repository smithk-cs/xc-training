import datetime as dt
import re

from xcapp import medals as MD
from xcapp import points as PT
from xcapp.models import WeekResult

MONDAY = dt.date(2026, 10, 26)        # the 2026 XC season starts Mon Aug 3: 12 finished weeks
WEEKS = {dt.date(2026, 10, 19): (95, 31, 30), dt.date(2026, 10, 12): (85, 25, 30), dt.date(2026, 10, 5): (70, 30, 30),
         dt.date(2026, 9, 28): (92, 30, 30)}


def fake_week(calls):
    def team_week(db, monday, gender=None, athletes=None):
        calls.append(monday)
        score, miles, goal = WEEKS.get(monday, (50, 10, 30))
        return [{"a": a, "score": score, "miles": miles, "goal_miles": goal, "points": score, "target": 100, "run_days": 5} for a in athletes]
    return team_week


def test_medal_rules():
    assert MD.medal_for(90, 0, 30) == "gold" and MD.medal_for(89.9, 40, 30) == "silver"
    assert MD.medal_for(79, 30, 30) == "miles" and MD.medal_for(79, 29.97, 30) == "miles"
    assert MD.medal_for(79, 29, 30) == "" and MD.medal_for(None, 30, None) == ""


def test_summary_saves_weeks_and_monday_note(session, athlete, monkeypatch):
    calls = []
    monkeypatch.setattr(PT, "team_week", fake_week(calls))
    s = MD.summary(session, athlete, MONDAY)
    assert len(calls) == 12
    assert [(m["icon"], m["count"]) for m in s["medals"]] == [("🥇", 2), ("🥈", 1), ("👟", 1)]
    assert s["medals"][0]["text"] == "Gold: you hit your weekly goal (90% or more) in 2 weeks of 2026 Cross Country."
    assert s["note"]["text"] == "Gold week! You scored 95% last week, your 2nd gold week of 2026 Cross Country. Keep it rolling!"
    calls.clear()
    MD.summary(session, athlete, MONDAY)
    assert calls == []                                         # saved: nothing worked out again the same day
    MD.summary(session, athlete, MONDAY + dt.timedelta(days=1))
    assert calls == [dt.date(2026, 10, 12), dt.date(2026, 10, 19)]   # next day: only the last two weeks again
    assert MD.summary(session, athlete, MONDAY + dt.timedelta(days=1))["note"] is None   # Tuesday: no message
    MD.close_note(session, athlete, dt.date(2026, 10, 19))
    assert MD.summary(session, athlete, MONDAY)["note"] is None


def test_weeks_saved_before_the_rule_change_are_worked_out_again(session, athlete, monkeypatch):
    calls = []
    monkeypatch.setattr(PT, "team_week", fake_week(calls))
    MD.ensure_weeks(session, [athlete], [dt.date(2026, 9, 28)], dt.date(2026, 10, 20))
    MD.ensure_weeks(session, [athlete], [dt.date(2026, 9, 28)], MD.RULES_CHANGED)
    MD.ensure_weeks(session, [athlete], [dt.date(2026, 9, 28)], MD.RULES_CHANGED + dt.timedelta(days=3))
    assert calls == [dt.date(2026, 9, 28)] * 2


def test_silver_and_miles_notes(session, athlete, monkeypatch):
    monkeypatch.setattr(PT, "team_week", fake_week([]))
    assert MD.summary(session, athlete, dt.date(2026, 10, 19))["note"]["text"].startswith("Silver week! You scored 85% last week, your 1st silver week of 2026 Cross Country")
    session.query(WeekResult).delete()
    note = MD.summary(session, athlete, dt.date(2026, 10, 12))["note"]
    assert note["icon"] == "👟" and note["text"].startswith("You ran all 30.0 of your 30 miles last week, your 1st full-mileage week")


def test_athlete_page_medals(monkeypatch):
    from fastapi.testclient import TestClient

    from xcapp import config
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete
    from xcapp.web import create_app
    monkeypatch.setenv("AS_OF_DATE", MONDAY.isoformat())
    config.reload_settings()
    real = PT.team_week
    monkeypatch.setattr(PT, "team_week", lambda db, monday, gender=None, athletes=None:
                        fake_week([])(db, monday, athletes=athletes) if monday < MONDAY else real(db, monday, gender, athletes))
    db = SessionLocal()
    db.add(Athlete(first_name="Ava", last_name="Doyle", gender="Girls", grade=11, group="Varsity", link_token="t-med"))
    db.commit()
    db.close()
    c = TestClient(create_app())
    page = c.get("/a/t-med").text
    assert page.count('class="medal-btn"') == 3 and "🥇" in page and "Gold week! You scored 95%" in page
    assert page.index("medal-btn") < page.index("</header>")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/a/t-med/medal-note", data={"csrf": csrf, "monday": "2026-10-19"})
    page = c.get("/a/t-med").text
    assert "Gold week!" not in page and page.count('class="medal-btn"') == 3

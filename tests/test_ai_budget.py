"""The monthly AI budget, thinking level and answer caps that keep the AI bill predictable."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from test_ai import FakeClient, good_reply, team  # noqa: F401  (team is a fixture)
from xcapp import ai as AI
from xcapp import config
from xcapp.models import AiRun, StaffUser
from xcapp.security import hash_password

AS_OF = dt.date(2026, 10, 25)


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def spend(session, dollars, model="claude-sonnet-5-5", when=None):
    """Record a past run that cost about `dollars` (all output tokens, $10 per million on Sonnet 5.5)."""
    session.add(AiRun(kind="briefing", status="ok", model=model, created_at=when or now_utc(),
                      input_tokens=0, output_tokens=round(dollars / 10 * 1_000_000)))
    session.flush()


def test_prices():
    assert AI.cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.00)
    assert AI.cost_usd("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert AI.price_of("some-future-model") == AI.UNKNOWN_PRICE


def test_month_spend_counts_this_month_only(session):
    spend(session, 4.00)
    failed = AiRun(kind="question", status="failed", model="claude-sonnet-5-5", created_at=now_utc(), input_tokens=500_000, output_tokens=0)
    session.add(failed)                                                   # billed tokens count even when the request failed
    spend(session, 7.00, when=now_utc().replace(day=1) - dt.timedelta(days=2))   # last month
    session.flush()
    assert AI.month_spend(session) == pytest.approx(5.00)


def test_requests_use_medium_thinking_and_room_to_answer(session, team):
    fake = FakeClient([good_reply(["R01", "R02", "R03"]), "R01 is fine."])
    assert AI.generate_briefing(session, client=fake, as_of=AS_OF).status == "ok"
    AI.ask(session, "How is Lucia?", client=fake, as_of=AS_OF)
    (b, q) = fake.calls
    assert b["output_config"] == {"effort": "medium"} and b["max_tokens"] == 16000
    assert q["output_config"] == {"effort": "medium"} and q["max_tokens"] == 8000
    assert "temperature" not in b and "thinking" not in b


def test_budget_stops_requests_before_they_are_sent(session, team):
    spend(session, 24.95)
    fake = FakeClient([good_reply(["R01", "R02", "R03"])])
    run = AI.generate_briefing(session, client=fake, as_of=AS_OF)
    assert run.status == "failed" and fake.calls == []
    assert "This month's AI budget ($25.00) is nearly used up: $24.95 spent so far." in run.error
    q = AI.ask(session, "Anything new?", client=fake, as_of=AS_OF)
    assert q.status == "failed" and "budget" in q.error and fake.calls == []


def test_budget_setting_and_thinking_level(session, team, monkeypatch):
    spend(session, 24.95)
    monkeypatch.setenv("AI_MONTHLY_BUDGET_USD", "40")
    monkeypatch.setenv("AI_EFFORT", "low")
    config.reload_settings()
    fake = FakeClient([good_reply(["R01", "R02", "R03"])])
    assert AI.generate_briefing(session, client=fake, as_of=AS_OF).status == "ok"
    assert fake.calls[0]["output_config"] == {"effort": "low"}
    monkeypatch.setenv("AI_EFFORT", "extreme")
    assert "AI_EFFORT must be low, medium or high." in config.reload_settings().problems()


def test_tokens_are_counted_even_when_the_answer_is_unusable(session, team):
    for stop in ("max_tokens", "refusal"):
        run = AI.generate_briefing(session, client=FakeClient(["{"], stop=stop), as_of=AS_OF)
        assert run.status == "failed" and (run.input_tokens, run.output_tokens) == (1000, 200)
    retried = AI.generate_briefing(session, client=FakeClient(["nope", "still nope"]), as_of=AS_OF)
    assert retried.status == "failed" and (retried.input_tokens, retried.output_tokens) == (2000, 400)
    assert AI.month_spend(session) == pytest.approx(AI.cost_usd("claude-sonnet-5-5", 4000, 800))


def test_ai_page_shows_spending(session):
    session.add(StaffUser(username="coach1", role="coach", password_hash=hash_password("correct-horse-1")))
    spend(session, 3.42)
    session.commit()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "coach1", "password": "correct-horse-1"})
    assert "AI spending this month: $3.42 of $25.00." in c.get("/ai").text

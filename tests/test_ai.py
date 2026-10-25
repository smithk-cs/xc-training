"""AI briefing: de-identification, strict reply checking, retries, failures, and coach questions."""
import datetime as dt
import json
import types

import pytest

from xcapp import ai as AI
from xcapp import datasets as DS
from xcapp.models import Athlete, Injury, PainReport, Result


class FakeClient:
    def __init__(self, replies, stop="end_turn"):
        self.replies, self.stop, self.calls = list(replies), stop, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=self.stop,
                                     usage=types.SimpleNamespace(input_tokens=1000, output_tokens=200))


@pytest.fixture
def team(session):
    people = [("Lucia", "Sorensen", "Girls", "Varsity"), ("Adrian", "Iverson", "Boys", "Varsity"), ("Ruby", "Haddad", "Girls", "JV")]
    out = []
    for i, (f, l, g, grp) in enumerate(people):
        a = Athlete(first_name=f, last_name=l, gender=g, grade=11, group=grp, link_token=f"t{i}")
        session.add(a)
        session.flush()
        session.add(Result(athlete_id=a.id, event="5K", time_s=1100 + 30 * i, date=dt.date(2026, 10, 17), season="2026 XC"))
        out.append(a)
    session.add(Injury(athlete_id=out[0].id, onset_date=dt.date(2026, 10, 6), injury="Hamstring strain", body_area="Thigh",
                       return_date=dt.date(2026, 10, 15), notes="SECRET-NOTE family situation"))
    session.add(PainReport(athlete_id=out[2].id, date=dt.date(2026, 10, 22), body_area="Foot", severity=5, note="SECRET-PAIN-NOTE"))
    session.flush()
    return out


def good_reply(codes):
    return json.dumps({"insights": [
        {"priority": "High", "scope": [codes[0]], "category": "Athlete check-in", "headline": "Check in", "insight": f"{codes[0]} came back from injury.", "action": f"Talk to {codes[0]}."},
        {"priority": "Medium", "scope": ["Team"], "category": "Injury risk", "headline": "Steady", "insight": "Load is steady.", "action": "Keep it."},
        {"priority": "Low", "scope": [codes[1], codes[2]], "category": "Performance", "headline": "Faster", "insight": f"{codes[1]} and {codes[2]} improved.", "action": "Nice."}]})


def test_payload_is_deidentified(session, team):
    payload, code_map = AI.build_payload(DS.snapshot(session, dt.date(2026, 10, 25)))
    text = json.dumps(payload)
    for a in team:
        assert a.first_name not in text and a.last_name not in text
    assert "SECRET" not in text and "Girls" not in text and "hr_max" not in text
    assert sorted(code_map) == ["R01", "R02", "R03"] and code_map["R01"]["name"] == "Lucia Sorensen"
    assert payload["athletes"][2]["pain_reports"][0]["severity"] == 5


def test_reply_checking():
    cm = {"R01": {"athlete_id": "A01", "name": "Lucia Sorensen"}, "R02": {"athlete_id": "A02", "name": "Adrian Iverson"},
          "R03": {"athlete_id": "A03", "name": "Ruby Haddad"}}
    ins = AI.parse_insights("```json\n" + good_reply(["R01", "R02", "R03"]) + "\n```", cm)
    assert ins[0]["scope"] == "Lucia Sorensen" and ins[0]["insight"] == "Lucia Sorensen came back from injury."
    assert ins[0]["athlete_code"] == "A01" and ins[1]["athlete_code"] is None and ins[2]["scope"] == "Adrian Iverson, Ruby Haddad"
    bad = json.loads(good_reply(["R01", "R02", "R03"]))
    for mutate, msg in [
        (lambda b: b["insights"][0].update(priority="Urgent"), "priority"),
        (lambda b: b["insights"][0].update(category="Diet"), "category"),
        (lambda b: b["insights"][0].update(scope=["R99"]), "unknown athlete code"),
        (lambda b: b["insights"][1].update(insight="R42 is fine"), "unknown athlete code"),
        (lambda b: b.update(insights=b["insights"][:2]), "3 to 8"),
    ]:
        b = json.loads(json.dumps(bad))
        mutate(b)
        with pytest.raises(AI.AiError, match=msg):
            AI.parse_insights(json.dumps(b), cm)
    with pytest.raises(AI.AiError, match="valid JSON"):
        AI.parse_insights("Here is your briefing!", cm)


def test_briefing_retry_success_and_failure(session, team):
    fake = FakeClient(["not json", good_reply(["R01", "R02", "R03"])])
    run = AI.generate_briefing(session, client=fake, as_of=dt.date(2026, 10, 25))
    assert run.status == "ok" and len(run.insights) == 3 and len(fake.calls) == 2
    assert "rejected" in fake.calls[1]["messages"][0]["content"]
    assert run.insights[0].athlete_id == team[0].id and run.input_tokens == 2000
    rows = DS.insight_rows(session)
    assert rows[0]["athlete_id"] == team[0].code and rows[0]["scope"] == "Lucia Sorensen"
    for c in fake.calls:
        assert "Lucia" not in c["messages"][0]["content"] and "Sorensen" not in c["messages"][0]["content"]

    bad = AI.generate_briefing(session, client=FakeClient(["nope", "still nope"]), as_of=dt.date(2026, 10, 25))
    assert bad.status == "failed" and "could not be used" in bad.error and not bad.insights
    assert DS.insight_rows(session)[0]["scope"] == "Lucia Sorensen"   # the last good briefing stays up
    refused = AI.generate_briefing(session, client=FakeClient(["x"], stop="refusal"), as_of=dt.date(2026, 10, 25))
    assert refused.status == "failed" and "declined" in refused.error


def test_question_swaps_names_both_ways(session, team):
    fake = FakeClient(["R01 is trending well; R03 reported foot pain."])
    run = AI.ask(session, "How are lucia sorensen and Ruby doing?", client=fake, as_of=dt.date(2026, 10, 25))
    sent = fake.calls[0]["messages"][0]["content"]
    assert "QUESTION: How are R01 and R03 doing?" in sent and "Lucia" not in sent and "Ruby" not in sent
    assert run.status == "ok" and run.answer == "Lucia Sorensen is trending well; Ruby Haddad reported foot pain."
    assert AI.ask(session, "   ", client=fake).status == "failed"


def test_ai_off_without_key(session, team):
    run = AI.generate_briefing(session)
    assert run.status == "failed" and "ANTHROPIC_API_KEY" in run.error

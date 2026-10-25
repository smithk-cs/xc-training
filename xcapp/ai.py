"""AI briefing and coach questions, powered by Claude through Anthropic's API.

Privacy: athletes are sent as codes (R01, R02, ...), never by name. Injury notes, gender, heart-rate max
and anything typed as free text are never sent. Names are put back only after the response is checked.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics

from sqlalchemy.orm import Session

from . import analysis as A
from .config import settings
from .datasets import snapshot
from .ingest import log
from .models import AiRun, Insight

PRIORITIES = ("High", "Medium", "Low")
CATEGORIES = ("Injury risk", "Training adjustment", "Athlete check-in", "Heart rate", "Performance")
CODE_RE = re.compile(r"\bR\d{2,3}\b")

SYSTEM_BRIEFING = """You write the weekly training briefing for the coaching staff of a high school cross country team.

You receive one JSON object with the team's synced training data. Athletes appear only as codes such as R07.
Definitions:
- load_ratio: an athlete's miles in a 7-day week divided by their average over the previous four weeks.
  Above 1.3 means mileage jumped more than 30%; it is a screening signal, not a diagnosis.
- recovery_bpm: how far heart rate fell in the first 60 seconds of rest between reps. Under 25 is flagged.
- rpe: the athlete's own effort rating for a run, 0-10. effort_load: rpe x minutes, summed per week.
  legs: how the athlete said their legs felt (Fresh, Normal, Heavy, Sore). Missing values mean not reported.
- Times are in seconds; paces are seconds per mile. Lower is faster.

Rules:
- Use only numbers that appear in the data. Quote them accurately, with units.
- Refer to athletes only by their codes. Never guess names, gender, weight, body shape or eating habits.
- Do not diagnose. For anything medical, the next step is the athletic trainer or a physician.
- Prefer findings a coach can act on this week: load spikes, athletes returning from injury, heart rate that
  is high for the pace or recovering slowly, pain reports, and performance that is stalling or improving.
- Write in plain, direct coaching language.

Reply with JSON only, no other text, in this exact shape:
{"insights": [{"priority": "High|Medium|Low", "scope": ["Team"] or ["R07", ...],
 "category": "Injury risk|Training adjustment|Athlete check-in|Heart rate|Performance",
 "headline": "at most 8 words", "insight": "at most 70 words", "action": "at most 30 words"}]}
Give 4 to 8 insights, most important first."""

SYSTEM_QUESTION = """You answer a high school cross country coach's question about their team's training data.
The data is one JSON object; athletes appear only as codes such as R07, and the definitions are:
load_ratio = this week's miles / average of the previous four weeks (above 1.3 is flagged);
recovery_bpm = heart-rate drop in the first 60 seconds of rest between reps (under 25 is flagged);
times in seconds, paces in seconds per mile.
Use only the data. Refer to athletes by code. Do not diagnose; send medical questions to the athletic trainer or a
physician. If the data cannot answer the question, say so. Answer in at most 200 words of plain prose."""


class AiError(Exception):
    pass


def make_client():
    s = settings()
    if not s.ai_configured:
        raise AiError("AI is not set up yet. Add ANTHROPIC_API_KEY.")
    import anthropic
    kwargs = {"api_key": s.anthropic_api_key, "max_retries": 2, "timeout": 120.0}
    if s.anthropic_base_url:
        kwargs["base_url"] = s.anthropic_base_url
    return anthropic.Anthropic(**kwargs)


# ---------- building the de-identified payload ----------

def _days_ago(as_of: dt.date, iso: str) -> int:
    return (as_of - dt.date.fromisoformat(iso)).days


def build_payload(snap: dict) -> tuple[dict, dict[str, dict]]:
    as_of: dt.date = snap["as_of"]
    roster = snap["roster"]
    codes = {r["athlete_id"]: f"R{i:02d}" for i, r in enumerate(roster, start=1)}
    code_map = {codes[r["athlete_id"]]: {"athlete_id": r["athlete_id"], "name": r["athlete"]} for r in roster}

    weekly = snap["weekly_load"]
    weeks = sorted({w["week_start"] for w in weekly})
    team_weeks = []
    for w in weeks[-10:]:
        rows = [r for r in weekly if r["week_start"] == w]
        team_weeks.append({
            "week_start": w, "avg_miles": round(statistics.fmean(float(r["miles"]) for r in rows), 1) if rows else 0,
            "athletes_over_1_3": sum(1 for r in rows if (A.num(r["acwr"]) or 0) > A.LOAD_RATIO_FLAG),
            "athletes_injured": sum(1 for r in rows if int(r["injured"]) == 1),
        })
    peaks = snap["injury_peaks"]
    injuries = [{
        "athlete": codes[i["athlete_id"]], "injury": i["injury"], "body_area": i["body_area"],
        "onset_days_ago": _days_ago(as_of, i["onset_date"]), "status": i["status"], "days_missed": i["days_missed"],
        "peak_load_ratio_in_2_weeks_before": None if peaks.get(i["injury_id"]) is None else round(peaks[i["injury_id"]], 2),
    } for i in snap["injuries"] if _days_ago(as_of, i["onset_date"]) <= 150 and i["athlete_id"] in codes]

    summaries = snap["summaries"]
    latest_date = max((s["date"] for s in summaries), default=None)
    latest_recs = [s["recovery"] for s in summaries if s["date"] == latest_date and s["recovery"] is not None]
    seg_by_workout = {}
    for s in snap["segments"]:
        seg_by_workout.setdefault(s["workout_id"], s)

    prog = {}
    for p in snap["progress"]:
        prog.setdefault(p["athlete_id"], {})[p["event"]] = {
            k: (None if p[k] is None else round(p[k], 1)) for k in ("last_xc", "track", "current", "goal")}
    watch = {w["athlete_id"]: w["reasons"] for w in snap["watchlist"]}
    effort = {}
    for r in snap.get("recent_runs", []):
        effort.setdefault(r["athlete_id"], []).append(
            {"days_ago": _days_ago(as_of, r["date"]), "miles": r["miles"], "rpe": r["rpe"], "legs": r["feel"] or None, "workout": r["workout"]})
    pain = snap["pain_reports"]

    athletes = []
    for r in roster:
        aid = r["athlete_id"]
        rows = [w for w in weekly if w["athlete_id"] == aid]
        rows.sort(key=lambda w: w["week_start"])
        mine = sorted((s for s in summaries if s["athlete_id"] == aid), key=lambda s: s["date"])
        workouts = []
        for s in mine[-3:]:
            seg = seg_by_workout.get(s["workout_id"], {})
            workouts.append({
                "workout": s["workout"], "date": s["date"], "rep_m": seg.get("rep_m"), "reps": s["reps"],
                "avg_pace": s["avg_pace"], "target_pace": s["target"], "avg_hr": s["avg_hr"],
                "hr_rise_first_to_last_rep": s["hr_rise"], "recovery_bpm": s["recovery"],
            })
        active = next((i for i in snap["injuries"] if i["athlete_id"] == aid and i["status"] == "Active"), None)
        athletes.append({
            "code": codes[aid], "group": r["group"], "grade": r["grade"],
            "weekly_miles_last_6": [w["miles"] for w in rows[-6:]],
            "load_ratio_last_6": [A.num(w["acwr"]) for w in rows[-6:]],
            "injured_weeks_last_6": [int(w["injured"]) for w in rows[-6:]],
            "effort_load_last_6": [w.get("srpe", 0) for w in rows[-6:]],
            "runs_last_7_days": effort.get(aid, []),
            "active_injury": None if not active else {"injury": active["injury"], "onset_days_ago": _days_ago(as_of, active["onset_date"])},
            "pain_reports": [{"days_ago": _days_ago(as_of, p["date"]), "body_area": p["body_area"], "severity": p["severity"]}
                             for p in pain if p["athlete_id"] == aid],
            "recent_workouts": workouts,
            "benchmarks_sec": prog.get(aid, {}),
            "flags": watch.get(aid, ""),
        })

    payload = {
        "as_of": as_of.isoformat(),
        "seasons": snap["seasons"],
        "team": {
            "athletes": len(roster), "kpis": snap["kpis"],
            "injury_rate_by_load_ratio": snap["risk_bands"],
            "weeks": team_weeks,
            "injuries_last_150_days": injuries,
            "latest_workout": None if latest_date is None else {
                "date": latest_date, "team_median_recovery_bpm": round(statistics.median(latest_recs), 1) if latest_recs else None},
        },
        "athletes": athletes,
    }
    # Codes only: the payload must never carry a roster name.
    text = json.dumps(payload)
    for info in code_map.values():
        if info["name"] and info["name"] in text:
            raise AiError("Refusing to send: an athlete name appeared in the AI payload.")
    return payload, code_map


# ---------- calling Claude ----------

def _call(client, system: str, user: str, max_tokens: int):
    resp = client.messages.create(
        model=settings().anthropic_model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    stop = getattr(resp, "stop_reason", None)
    usage = getattr(resp, "usage", None)
    tokens = (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
    if stop == "refusal":
        raise AiError("Claude declined to answer this request.")
    if stop == "max_tokens":
        raise AiError("The answer was cut off before it finished.")
    return text, tokens


def _names(text: str, code_map: dict) -> str:
    def sub(m):
        code = m.group(0)
        if code not in code_map:
            raise AiError(f"The response mentioned an unknown athlete code {code}.")
        return code_map[code]["name"]
    return CODE_RE.sub(sub, text)


def parse_insights(text: str, code_map: dict) -> list[dict]:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AiError(f"The response was not valid JSON ({exc.msg}).") from exc
    items = body.get("insights") if isinstance(body, dict) else None
    if not isinstance(items, list) or not 3 <= len(items) <= 8:
        raise AiError("The response did not contain 3 to 8 insights.")
    out = []
    for n, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            raise AiError(f"Insight {n} is not an object.")
        pri, cat = it.get("priority"), it.get("category")
        if pri not in PRIORITIES:
            raise AiError(f"Insight {n} has an unknown priority.")
        if cat not in CATEGORIES:
            raise AiError(f"Insight {n} has an unknown category.")
        scope = it.get("scope")
        scope = [scope] if isinstance(scope, str) else scope
        if not isinstance(scope, list) or not scope:
            raise AiError(f"Insight {n} has no scope.")
        fields = {k: str(it.get(k) or "").strip() for k in ("headline", "insight", "action")}
        if not all(fields.values()):
            raise AiError(f"Insight {n} is missing text.")
        if len(fields["headline"]) > 120 or len(fields["insight"]) > 900 or len(fields["action"]) > 400:
            raise AiError(f"Insight {n} is too long.")
        if scope == ["Team"]:
            scope_text, athlete_code = "Team", None
        else:
            bad = [c for c in scope if c not in code_map]
            if bad:
                raise AiError(f"Insight {n} names unknown athlete code {bad[0]}.")
            scope_text, athlete_code = ", ".join(code_map[c]["name"] for c in scope), scope[0]
        out.append({
            "priority": pri, "category": cat, "scope": scope_text,
            "athlete_code": athlete_code and code_map[athlete_code]["athlete_id"],
            **{k: _names(v, code_map) for k, v in fields.items()},
        })
    return out


def generate_briefing(db: Session, client=None, as_of: dt.date | None = None) -> AiRun:
    run = AiRun(kind="briefing", status="started", model=settings().anthropic_model)
    db.add(run)
    db.flush()
    try:
        client = client or make_client()
        payload, code_map = build_payload(snapshot(db, as_of))
        user = "DATA:\n" + json.dumps(payload, separators=(",", ":")) + "\n\nWrite this week's briefing."
        last_err = None
        for attempt in range(2):
            prompt = user if last_err is None else user + f"\n\nYour previous reply was rejected: {last_err} Reply with valid JSON only."
            text, (tin, tout) = _call(client, SYSTEM_BRIEFING, prompt, 4000)
            run.input_tokens += tin
            run.output_tokens += tout
            try:
                insights = parse_insights(text, code_map)
                break
            except AiError as exc:
                last_err = str(exc)
        else:
            raise AiError(f"Claude's reply could not be used: {last_err}")
        for pos, it in enumerate(insights, start=1):
            aid = it["athlete_code"]
            run.insights.append(Insight(
                position=pos, priority=it["priority"], scope=it["scope"], category=it["category"],
                athlete_id=int(aid[1:]) if aid else None, headline=it["headline"], insight=it["insight"], action=it["action"]))
        run.status = "ok"
        log(db, "ai", f"Briefing generated with {len(insights)} insights.")
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)[:1000]
        log(db, "ai", f"Briefing failed: {exc}", "error")
    db.flush()
    return run


def ask(db: Session, question: str, client=None, as_of: dt.date | None = None) -> AiRun:
    question = question.strip()[:1000]
    run = AiRun(kind="question", status="started", model=settings().anthropic_model, question=question)
    db.add(run)
    db.flush()
    try:
        if not question:
            raise AiError("Type a question first.")
        client = client or make_client()
        payload, code_map = build_payload(snapshot(db, as_of))
        # Names typed by the coach are swapped for codes before sending.
        q = question
        for code, info in sorted(code_map.items(), key=lambda kv: -len(kv[1]["name"])):
            q = re.sub(r"\b" + re.escape(info["name"]) + r"\b", code, q, flags=re.IGNORECASE)
        firsts: dict[str, list[str]] = {}
        for code, info in code_map.items():
            firsts.setdefault(info["name"].split()[0].lower(), []).append(code)
        for first, cs in firsts.items():
            if len(cs) == 1:  # a first name that only one athlete has
                q = re.sub(r"\b" + re.escape(first) + r"\b", cs[0], q, flags=re.IGNORECASE)
        text, (tin, tout) = _call(client, SYSTEM_QUESTION,
                                  "DATA:\n" + json.dumps(payload, separators=(",", ":")) + "\n\nQUESTION: " + q, 1200)
        run.input_tokens, run.output_tokens = tin, tout
        run.answer = _names(text.strip(), code_map)
        run.status = "ok"
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)[:1000]
    db.flush()
    return run

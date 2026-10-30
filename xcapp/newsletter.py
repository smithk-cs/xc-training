"""The weekly newsletter: drafted from the team's data, edited and approved by a coach, then sent by hand.

Rules (docs/plans/newsletter.md and privacy.md): never sent without a coach's approval; each issue keeps a snapshot of
its numbers so later corrections don't change it; athletes appear by name or in photos only with consent, checked when
the numbers are drawn and again every time the issue is shown; no rankings by miles or points, no lists of misses, and
nothing about body weight or food restriction."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .models import (Activity, Athlete, Course, MeetConditions, MeetEntry, MissedDay, NewsletterDeadline, NewsletterIssue,
                     NewsletterSection, PlannedWorkout, RaceResult, RaceTeamResult, RecoveryTip, Spotlight, VdotChange, utcnow)

TEMPLATE_VERSION = 1
STATUSES = ("draft", "ready_for_review", "approved", "sent")
STATUS_LABELS = {"draft": "Draft", "ready_for_review": "Ready for review", "approved": "Approved", "sent": "Sent"}
COACH_NOTE_WORDS, SPOTLIGHT_WORDS = 100, 60   # suggested lengths; longer text gets a gentle warning
STREAK_PCT = 80          # a streak week: at least this % of the weekly points target
STREAK_MIN_WEEKS = 3     # streaks are mentioned from this many weeks
STREAK_LOOKBACK = 8      # weeks looked back for streaks
RECAP_TOP = 10           # finishers listed per race
EMAIL_WIDTH = 600

# Body weight, food restriction and the like are never part of the newsletter (privacy.md, hard rule 2).
# "Weight room" (strength training) is fine.
BLOCKED = re.compile(r"\b(body\s*weight|bodyweight|weigh[- ]?ins?|weighed|lose weight|losing weight|lost weight|weight loss|"
                     r"gain(ed|ing)? weight|pounds|lbs|diet|diets|dieting|calorie|calories|bmi|body fat|skinny|fat|"
                     r"restrict(ing)? (food|eating))\b", re.I)


def blocked_words(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in BLOCKED.finditer(text or "")})


# ---------- the issue's week ----------

@dataclass
class WeekCtx:
    db: Session
    monday: dt.date            # the week the issue reports on
    issue_id: int | None = None

    @property
    def sunday(self) -> dt.date:
        return self.monday + dt.timedelta(days=6)

    @property
    def next_monday(self) -> dt.date:
        return self.monday + dt.timedelta(days=7)

    @property
    def next_sunday(self) -> dt.date:
        return self.monday + dt.timedelta(days=13)


def default_week(today: dt.date) -> dt.date:
    """The week a new issue reports on: the one that just ended (or this one, from Friday on)."""
    monday = A.week_monday(today)
    return monday if today.weekday() >= 4 else monday - dt.timedelta(days=7)


def _name(a: Athlete) -> dict:
    return {"athlete_id": a.id, "name": a.name}


def _team(db: Session) -> list[Athlete]:
    return db.scalars(select(Athlete).where(A.on_team()).order_by(Athlete.last_name, Athlete.first_name)).all()


# ---------- section builders: (ctx) -> dict | None (None: nothing this week) ----------

def build_week_ahead(c: WeekCtx) -> dict | None:
    plans = c.db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= c.next_monday, PlannedWorkout.date <= c.next_sunday)
                         .order_by(PlannedWorkout.date, PlannedWorkout.id)).all()
    meets = c.db.scalars(select(MeetEntry).where(MeetEntry.date >= c.next_monday, MeetEntry.date <= c.next_sunday)
                         .order_by(MeetEntry.date)).all()
    days = []
    for i in range(7):
        d = c.next_monday + dt.timedelta(days=i)
        items = [{"kind": "meet", "text": m.name + (f" · {m.location}" if m.location else "")} for m in meets if m.date == d]
        items += [{"kind": "workout", "text": w.title + ("" if (w.groups or "All") == "All" else f" ({w.groups.replace(',', ', ')})")}
                  for w in plans if w.date == d]
        if items:
            days.append({"date": d.isoformat(), "items": items})
    return {"days": days} if days else None


def build_race_logistics(c: WeekCtx) -> dict | None:
    meets = c.db.scalars(select(MeetEntry).where(MeetEntry.date >= c.next_monday, MeetEntry.date <= c.next_sunday)
                         .order_by(MeetEntry.date)).all()
    if not meets:
        return None
    return {"meets": [{"name": m.name, "date": m.date.isoformat(), "location": m.location,
                       "course": m.course.name if m.course else "",
                       "divisions": [d.name for d in m.divisions],
                       # No weather service yet: the coach adds the forecast in the section's text.
                       "forecast": "pending"} for m in meets]}


def build_deadlines(c: WeekCtx) -> dict | None:
    sent_on = c.sunday + dt.timedelta(days=1)
    rows = c.db.scalars(select(NewsletterDeadline).where(NewsletterDeadline.due_date >= sent_on).order_by(NewsletterDeadline.due_date)).all()
    shown = [{"title": d.title, "due": d.due_date.isoformat(), "notes": d.notes} for d in rows
             if d.due_date - dt.timedelta(days=d.show_from_days) <= sent_on]
    return {"deadlines": shown} if shown else None


def _week_races(c: WeekCtx) -> list[RaceResult]:
    return c.db.scalars(select(RaceResult).join(Athlete).where(RaceResult.date >= c.monday, RaceResult.date <= c.sunday,
                                                               Athlete.is_test.is_(False))
                        .order_by(RaceResult.date, RaceResult.meet, RaceResult.race)).all()


def build_race_recap(c: WeekCtx) -> dict | None:
    races = _week_races(c)
    if not races:
        return None
    groups: dict[tuple, list[RaceResult]] = {}
    for r in races:
        groups.setdefault((r.date, r.meet, r.race), []).append(r)
    out = []
    for (day, meet, race), rows in groups.items():
        team = c.db.scalar(select(RaceTeamResult).where(RaceTeamResult.meet == meet, RaceTeamResult.date == day, RaceTeamResult.race == race))
        cond = c.db.scalar(select(MeetConditions).where(MeetConditions.meet == meet, MeetConditions.date == day))
        rows.sort(key=lambda r: (r.place is None, r.place or 0, r.time_s))
        out.append({
            "meet": meet, "date": day.isoformat(), "race": race, "course": rows[0].course or (cond.course.name if cond and cond.course else ""),
            "distance": rows[0].distance_label,
            "team_place": team.place if team else None, "teams": team.teams if team else None,
            "team_points": team.points if team else None,
            "conditions": None if cond is None else {"temp_f": cond.temp_f, "footing": cond.footing, "wind_mph": cond.wind_mph},
            "runners": len(rows),
            "finishers": [{**_name(r.athlete), "time": round(r.time_s, 1), "place": r.place} for r in rows[:RECAP_TOP]],
        })
    return {"races": out}


def build_prs(c: WeekCtx) -> dict | None:
    out = []
    for r in _week_races(c):
        same = [t for t, m in c.db.execute(select(RaceResult.time_s, RaceResult.distance_m).where(
            RaceResult.athlete_id == r.athlete_id, RaceResult.date < r.date)).all() if abs(m - r.distance_m) <= r.distance_m * 0.01]
        if same and r.time_s < min(same):
            out.append({**_name(r.athlete), "distance": r.distance_label, "time": round(r.time_s, 1), "previous": round(min(same), 1),
                        "meet": r.meet})
    out.sort(key=lambda x: x["name"].split()[-1] + " " + x["name"])
    return {"prs": out} if out else None


def build_course_snapshot(c: WeekCtx) -> dict | None:
    meets = c.db.scalars(select(MeetEntry).where(MeetEntry.date >= c.next_monday, MeetEntry.date <= c.next_sunday)).all()
    out = []
    for m in meets:
        course: Course | None = m.course
        last = c.db.scalars(select(RaceTeamResult).where(RaceTeamResult.date < c.next_monday, RaceTeamResult.date >= m.date - dt.timedelta(days=500),
                                                         RaceTeamResult.date <= m.date - dt.timedelta(days=200))).all()
        same = [t for t in last if _same_meet(t.meet, m.name)]
        been = bool(same) or bool(c.db.scalar(select(RaceResult.id).where(RaceResult.date < c.next_monday).where(
            (RaceResult.course == course.name) if course else (RaceResult.meet == m.name)).limit(1)))
        out.append({"meet": m.name, "course": course.name if course else "",
                    "distance_m": course.distance_m if course else None, "surface": course.surface if course else "",
                    "hill_tier": course.hill_tier if course else None, "notes": (course.notes if course else "")[:300],
                    "video_url": course.video_url if course else "",
                    "last_year": [{"race": t.race, "place": t.place, "teams": t.teams, "points": t.points} for t in same],
                    "first_time": not been})
    return {"courses": out} if out else None


def _same_meet(a: str, b: str) -> bool:
    key = lambda s: re.sub(r"\b(19|20)\d\d\b|\W+", " ", s.lower()).split()  # noqa: E731  (ignore years and punctuation)
    return key(a) == key(b)


def build_team_numbers(c: WeekCtx) -> dict | None:
    from . import points as PT
    rows = PT.team_week(c.db, c.monday)
    if not rows or not any(r["miles"] for r in rows):
        return None
    goal = float(A.get_setting(c.db, "newsletter_team_goal_miles", "0") or 0)
    s0, _s1 = A.season_range(c.sunday)
    season_miles = None
    if goal:
        ids = [a.id for a in _team(c.db)]
        dist = c.db.scalars(select(Activity.distance_m).where(Activity.athlete_id.in_(ids), Activity.local_date >= s0,
                                                              Activity.local_date <= c.sunday)).all()
        season_miles = round(sum(d or 0 for d in dist) / A.METERS_PER_MILE)
    # Totals only: no names here, ever (no rankings by miles or points).
    return {"athletes": len(rows), "miles": round(sum(r["miles"] for r in rows)), "points": round(sum(r["points"] for r in rows)),
            "days_run": sum(r["run_days"] for r in rows), "goal_miles": goal or None, "season_miles": season_miles,
            "season": A.season_for(c.sunday)}


def build_streaks_vdot(c: WeekCtx) -> dict | None:
    from . import points as PT
    ups = []
    for ch in c.db.scalars(select(VdotChange).where(VdotChange.date >= c.monday, VdotChange.date <= c.sunday)).all():
        a = c.db.get(Athlete, ch.athlete_id)
        if ch.vdot is not None and ch.previous is not None and ch.vdot > ch.previous and a is not None and a.active and not a.is_test:
            if all(u["athlete_id"] != a.id for u in ups):
                ups.append({**_name(a), "vdot": ch.vdot})
    # Streaks: consecutive weeks at STREAK_PCT% or more of the points target; a week with a sick or injured day pauses
    # the streak rather than breaking it.
    weeks = [c.monday - dt.timedelta(days=7 * k) for k in range(STREAK_LOOKBACK)]
    by_week = {w: {r["a"].id: r for r in PT.team_week(c.db, w)} for w in weeks}
    excused = {(m.athlete_id, A.week_monday(m.date)) for m in c.db.scalars(select(MissedDay).where(
        MissedDay.date >= weeks[-1], MissedDay.date <= c.sunday, MissedDay.reason.in_(("Injury", "Sick")))).all()}
    streaks = []
    for a in _team(c.db):
        n = 0
        for w in weeks:
            r = by_week[w].get(a.id)
            if (a.id, w) in excused:
                continue
            if r and r["target"] and 100 * r["points"] / r["target"] >= STREAK_PCT:
                n += 1
            else:
                break
        if n >= STREAK_MIN_WEEKS:
            streaks.append({**_name(a), "weeks": n})
    ups.sort(key=lambda x: x["name"].split()[-1])
    if not ups and not streaks:
        return None
    return {"vdot_ups": ups, "streaks": streaks}   # streaks are already alphabetical: never ranked


def suggest_spotlight(db: Session, issue_id: int | None = None) -> Athlete | None:
    """Whose turn it is: athletes with spotlight consent, those featured least (and longest ago) first."""
    team = [a for a in _team(db) if a.show_spotlight and a.show_name]
    if not team:
        return None
    rows = db.scalars(select(Spotlight).where(Spotlight.issue_id != (issue_id or -1))).all()
    count = {a.id: 0 for a in team}
    last = {a.id: 0 for a in team}
    for s in rows:
        if s.athlete_id in count:
            count[s.athlete_id] += 1
            last[s.athlete_id] = max(last[s.athlete_id], s.issue_id)
    return min(team, key=lambda a: (count[a.id], last[a.id], a.last_name, a.first_name))


def build_spotlight(c: WeekCtx) -> dict | None:
    a = suggest_spotlight(c.db, c.issue_id)
    return {"suggested": _name(a)} if a else None


def pick_tip(db: Session) -> RecoveryTip | None:
    tips = db.scalars(select(RecoveryTip).where(RecoveryTip.approved_by != "")).all()
    return min(tips, key=lambda t: (t.last_used or dt.date.min, t.id)) if tips else None


def build_recovery_tip(c: WeekCtx) -> dict | None:
    t = pick_tip(c.db)
    return {"tip_id": t.id, "text": t.text, "category": t.category} if t else None


@dataclass(frozen=True)
class SectionSpec:
    key: str
    title: str
    build: Callable[[WeekCtx], dict | None] | None   # None: written by a coach only
    needs_coach_text: bool = False
    help: str = ""


SECTIONS = [
    SectionSpec("coach_note", "From the coach", None, True, f"A short note, about {COACH_NOTE_WORDS} words."),
    SectionSpec("week_ahead", "The week ahead", build_week_ahead, help="From next week’s workouts and meets. Add anything else below."),
    SectionSpec("race_logistics", "Race-day details", build_race_logistics,
                help="Add departure and return times, what to bring, and the forecast once it’s out."),
    SectionSpec("deadlines", "Coming up", build_deadlines, help="From the deadlines list (forms, fees, orders)."),
    SectionSpec("race_recap", "Race recap", build_race_recap, help="Team place and score, and the top finishers with times."),
    SectionSpec("prs", "Personal records", build_prs, help="New bests this week, with the previous best."),
    SectionSpec("course_snapshot", "Course preview", build_course_snapshot, help="For next week’s meets: the course and last year’s team result."),
    SectionSpec("team_numbers", "Team numbers", build_team_numbers, help="Team totals only, never names."),
    SectionSpec("streaks_vdot", "Streaks and fitness gains", build_streaks_vdot,
                help=f"Alphabetical: {STREAK_MIN_WEEKS}+ weeks in a row at {STREAK_PCT}% of the points target, and anyone who moved up a fitness level."),
    SectionSpec("spotlight", "Athlete spotlight", build_spotlight, True, f"A few lines, about {SPOTLIGHT_WORDS} words. The app suggests whose turn it is."),
    SectionSpec("captains_corner", "Captains’ corner", None, True, "Text from the captains, after a coach has read it."),
    SectionSpec("recovery_tip", "Recovery tip", build_recovery_tip, help="Rotates through the approved tips."),
    SectionSpec("photos", "Photos", None, help="Only photos where every athlete shown has photo consent."),
    SectionSpec("thank_yous", "Thank you", None, True, "Volunteers, parents, sponsors."),
]
SPEC = {s.key: s for s in SECTIONS}


# ---------- issues ----------

def create_issue(db: Session, monday: dt.date, by: str) -> NewsletterIssue:
    issue = NewsletterIssue(week_start=monday, status="draft", template_version=TEMPLATE_VERSION, created_by=by)
    db.add(issue)
    db.flush()
    for i, spec in enumerate(SECTIONS):
        issue.sections.append(NewsletterSection(key=spec.key, position=i, enabled=True))
    refresh(db, issue)
    return issue


def refresh(db: Session, issue: NewsletterIssue) -> None:
    """Re-draw every section's numbers into a new snapshot. The coach's text is kept."""
    if issue.status == "sent":
        raise ValueError("A sent issue can’t change. Put any correction in next week’s issue.")
    ctx = WeekCtx(db, issue.week_start, issue.id)
    for sec in issue.sections:
        spec = SPEC.get(sec.key)
        data = spec.build(ctx) if spec and spec.build else None
        sec.data_snapshot = json.dumps(data, separators=(",", ":")) if data else ""
        if sec.key == "spotlight" and sec.athlete_id is None and data:
            sec.athlete_id = data["suggested"]["athlete_id"]
    issue.generated_at = utcnow()
    back_to_draft(issue)


def back_to_draft(issue: NewsletterIssue) -> None:
    """Any change to an approved issue needs approving again."""
    if issue.status in ("approved", "ready_for_review"):
        issue.status = "draft"
    issue.approved_by, issue.approved_at = "", None


def set_status(issue: NewsletterIssue, status: str, user) -> None:
    if issue.status == "sent":
        raise ValueError("This issue was sent and can’t change.")
    if status == "approved":
        if user.role not in ("coach", "admin"):
            raise ValueError("Only a coach can approve an issue.")
        issue.status, issue.approved_by, issue.approved_at = "approved", user.username, utcnow()
    elif status in ("draft", "ready_for_review"):
        issue.status = status
        issue.approved_by, issue.approved_at = "", None
    else:
        raise ValueError("Unknown status.")


def problems(issue: NewsletterIssue) -> list[str]:
    """What has to change before approval: blocked topics in any coach text or photo text."""
    out = []
    for sec in issue.sections:
        bad = blocked_words(sec.coach_text)
        if bad and sec.enabled:
            out.append(f"{SPEC[sec.key].title if sec.key in SPEC else sec.key}: please leave out “{', '.join(bad)}”. "
                       "The newsletter never mentions body weight or dieting.")
    for ph in issue.photos:
        bad = blocked_words(ph.alt + " " + ph.caption)
        if bad:
            out.append(f"A photo’s caption or description mentions “{', '.join(bad)}”.")
    return out


# ---------- consent: applied when shown, on top of the snapshot ----------

def consent_map(db: Session) -> dict[int, Athlete]:
    return {a.id: a for a in db.scalars(select(Athlete)).all()}


def _named(rows: list[dict], people: dict[int, Athlete]) -> list[dict]:
    """Only athletes whose name may be shown, with their current name."""
    out = []
    for r in rows:
        a = people.get(r.get("athlete_id"))
        if a is not None and a.show_name:
            out.append({**r, "name": a.name})
    return out


def photo_ok(photo, people: dict[int, Athlete]) -> bool:
    ids = photo.tagged
    return all(people.get(i) is not None and people[i].show_photo for i in ids)


def rendered(db: Session, issue: NewsletterIssue, photo_url: Callable | None = None) -> list[dict]:
    """The enabled sections with something to show, in order, with consent applied. Used by every output."""
    people = consent_map(db)
    out = []
    for sec in sorted(issue.sections, key=lambda s: s.position):
        if not sec.enabled or sec.key not in SPEC:
            continue
        spec = SPEC[sec.key]
        data = json.loads(sec.data_snapshot) if sec.data_snapshot else None
        text = (sec.coach_text or "").strip()
        if data:
            data = _filter(sec.key, data, people)
        if sec.key == "spotlight":
            a = people.get(sec.athlete_id) if sec.athlete_id else None
            if not text or a is None or not (a.show_spotlight and a.show_name):
                continue
            data = {"athlete": a.name}
        if sec.key == "photos":
            shown = [p for p in issue.photos if photo_ok(p, people)]
            if not shown:
                continue
            data = {"photos": [{"id": p.id, "alt": p.alt, "caption": p.caption, "url": photo_url(p) if photo_url else ""} for p in shown]}
        if not data and not text:
            continue   # empty this week: the section is skipped
        out.append({"key": sec.key, "title": spec.title, "text": text, "data": data or {}})
    return out


def _filter(key: str, data: dict, people: dict[int, Athlete]) -> dict | None:
    if key == "race_recap":
        races = []
        for r in data["races"]:
            races.append({**r, "finishers": _named(r["finishers"], people)})
        return {"races": races}
    if key == "prs":
        prs = _named(data["prs"], people)
        return {"prs": prs} if prs else None
    if key == "streaks_vdot":
        ups = [{**u, "vdot": u["vdot"] if people[u["athlete_id"]].show_vdot else None} for u in _named(data["vdot_ups"], people)]
        streaks = _named(data["streaks"], people)
        return {"vdot_ups": ups, "streaks": streaks} if ups or streaks else None
    if key == "spotlight":
        s = data.get("suggested")
        return data if s and people.get(s["athlete_id"]) is not None and people[s["athlete_id"]].show_spotlight else None
    return data


# ---------- outputs ----------

def subject(issue: NewsletterIssue, team: str) -> str:
    return f"{team} weekly update · week of {issue.week_start:%b %-d}"


def plain_text(issue: NewsletterIssue, sections: list[dict], team: str) -> str:
    lines = [subject(issue, team).upper(), ""]
    for s in sections:
        lines += [s["title"].upper(), "-" * len(s["title"])]
        lines += _text_lines(s)
        if s["text"]:
            lines.append(s["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _text_lines(s: dict) -> list[str]:
    d, k, out = s["data"], s["key"], []
    t = A.fmt_time
    if k == "week_ahead":
        for day in d.get("days", []):
            out.append(dt.date.fromisoformat(day["date"]).strftime("%a %b %-d") + ": " + "; ".join(i["text"] for i in day["items"]))
    elif k == "race_logistics":
        for m in d.get("meets", []):
            out.append(f"{m['name']}, {dt.date.fromisoformat(m['date']):%a %b %-d}" + (f", {m['location']}" if m["location"] else "")
                       + ". Forecast: pending.")
    elif k == "deadlines":
        out += [f"{x['title']}: due {dt.date.fromisoformat(x['due']):%a %b %-d}" + (f". {x['notes']}" if x["notes"] else "") for x in d.get("deadlines", [])]
    elif k == "race_recap":
        for r in d.get("races", []):
            head = f"{r['meet']} · {r['race']}"
            if r.get("team_place"):
                head += f": team {_ordinal(r['team_place'])}" + (f" of {r['teams']}" if r.get("teams") else "") + \
                        (f", {r['team_points']:g} points" if r.get("team_points") is not None else "")
            out.append(head)
            out += [f"  {f['place'] or ''} {f['name']} {t(f['time'])}".replace("   ", " ") for f in r["finishers"]]
    elif k == "prs":
        out += [f"{p['name']}: {p['distance']} in {t(p['time'])} at {p['meet']} (previous best {t(p['previous'])})" for p in d.get("prs", [])]
    elif k == "course_snapshot":
        for c in d.get("courses", []):
            out.append(f"{c['meet']}: " + ("first time here." if c["first_time"] else (c["course"] or "course on file") + "."))
            out += [f"  Last year {x['race']}: team {_ordinal(x['place'])}" for x in c["last_year"] if x.get("place")]
    elif k == "team_numbers":
        out.append(f"{d['miles']:,} miles, {d['points']:,} points and {d['days_run']} days run across {d['athletes']} athletes.")
        if d.get("goal_miles"):
            out.append(f"Season so far: {d['season_miles']:,} of {d['goal_miles']:,.0f} miles.")
    elif k == "streaks_vdot":
        if d.get("streaks"):
            out.append("On a streak: " + ", ".join(f"{x['name']} ({x['weeks']}{'+' if x['weeks'] >= STREAK_LOOKBACK else ''} weeks)" for x in d["streaks"]))
        if d.get("vdot_ups"):
            out.append("Moved up a fitness level: " + ", ".join(x["name"] + (f" (VDOT {x['vdot']})" if x.get("vdot") else "") for x in d["vdot_ups"]))
    elif k == "spotlight":
        out.append(d.get("athlete", ""))
    elif k == "recovery_tip":
        out.append(d.get("text", ""))
    elif k == "photos":
        out += [p["caption"] or p["alt"] for p in d.get("photos", [])]
    return out


def _ordinal(n) -> str:
    if n is None:
        return ""
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def photo_token(secret: str, photo_id: int) -> str:
    """An unguessable link for a photo, so email clients can show it without signing in."""
    return hmac.new(secret.encode(), f"newsletter-photo:{photo_id}".encode(), hashlib.sha256).hexdigest()[:32]


def as_pdf(issue: NewsletterIssue, sections: list[dict], team: str, logo, photo_bytes: Callable[[int], bytes | None]) -> bytes:
    from . import registration as REG
    pdf, w = REG.branded_page(team, f"Weekly update · week of {issue.week_start:%B %-d, %Y}", logo)
    pdf.set_y(118)
    for s in sections:
        if pdf.get_y() > pdf.h - 140:
            pdf.add_page()
        pdf.set_font("Barlow", size=16)
        pdf.set_text_color(*REG.RED)
        pdf.cell(w, 22, s["title"], new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Inter", size=10.5)
        for line in _text_lines(s):
            pdf.multi_cell(w, 15, line, new_x="LMARGIN", new_y="NEXT")
        if s["key"] == "photos":
            for p in s["data"]["photos"]:
                raw = photo_bytes(p["id"])
                if raw:
                    if pdf.get_y() > pdf.h - 280:
                        pdf.add_page()
                    pdf.image(io.BytesIO(raw), x=pdf.l_margin, w=min(w, 320))
                    pdf.ln(4)
        if s["text"]:
            pdf.multi_cell(w, 15, s["text"], new_x="LMARGIN", new_y="NEXT")
        pdf.ln(10)
    return bytes(pdf.output())


def left_out(key: str, data: dict | None, people: dict[int, Athlete]) -> list[str]:
    """For the coach: names in a section's numbers that won't be shown for lack of name consent."""
    if not data:
        return []
    rows = []
    if key == "race_recap":
        rows = [f for r in data["races"] for f in r["finishers"]]
    elif key == "prs":
        rows = data["prs"]
    elif key == "streaks_vdot":
        rows = data["vdot_ups"] + data["streaks"]
    names = {people[r["athlete_id"]].name for r in rows if r.get("athlete_id") in people and not people[r["athlete_id"]].show_name}
    return sorted(names)

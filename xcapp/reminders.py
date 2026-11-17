"""Daily reminder emails: each athlete who opted in, and has an email address, hears about what they still need to do
(runs to rate, practice days to explain, meets to answer). Sent through Resend; nothing is sent until it is set up."""
from __future__ import annotations

import datetime as dt
from html import escape

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analysis as A
from . import registration as REG
from .config import settings
from .ingest import log
from .models import Activity, Athlete, MeetEntry, RestingHeartRate

RESEND_URL = "https://api.resend.com/emails"
ITEMS = {
    "effort": "Runs from the past week without an effort rating",
    "days": "Practice days in the past week with no run and no reason given",
    "meets": "Meets they haven’t answered, when the answer closes within 3 days",
    "rhr": "Today’s resting heart rate, if they haven’t entered it",
}
DEFAULT_ITEMS = ["effort", "days", "meets"]


class EmailError(Exception):
    pass


def enabled(db: Session) -> bool:
    return A.get_setting(db, "reminders_enabled", "0") == "1"


def chosen_items(db: Session) -> list[str]:
    raw = A.get_setting(db, "reminders_items", ",".join(DEFAULT_ITEMS))
    return [i for i in raw.split(",") if i in ITEMS]


def todo(db: Session, a: Athlete, today: dt.date, items: list[str]) -> list[str]:
    """What this athlete still needs to do, one line each."""
    out = []
    if "effort" in items:
        n = db.scalar(select(func.count(Activity.id)).where(
            Activity.athlete_id == a.id, Activity.rpe.is_(None), Activity.local_date > today - dt.timedelta(days=7),
            Activity.local_date <= today))
        if n:
            out.append(f"Rate how hard {n} run{'' if n == 1 else 's'} felt (effort 0–10).")
    if "days" in items:
        missing = A.unlogged_days(db, a, today, days=7)
        if missing:
            out.append(f"Tell your coaches why you didn’t run on {', '.join(d.strftime('%a %b %-d') for d in sorted(missing))}.")
    if "meets" in items:
        for m in db.scalars(select(MeetEntry).where(MeetEntry.date > today).order_by(MeetEntry.date)).all():
            days = (m.date - today).days
            closing = REG.ATTENDANCE_CUTOFF_DAYS < days <= REG.ATTENDANCE_CUTOFF_DAYS + 3
            fits = not m.divisions or a.gender in {d.gender for d in m.divisions}
            if closing and fits and not any(r.athlete_id == a.id for r in m.responses):
                last = m.date - dt.timedelta(days=REG.ATTENDANCE_CUTOFF_DAYS + 1)
                out.append(f"Tell your coaches whether you’ll be at {m.name} on {m.date:%a %b %-d} (answer by {last:%a %b %-d}).")
    if "rhr" in items and not db.scalar(select(RestingHeartRate.id).where(RestingHeartRate.athlete_id == a.id,
                                                                           RestingHeartRate.date == today)):
        out.append("Enter this morning’s resting heart rate.")
    return out


def compose(a: Athlete, lines: list[str]) -> tuple[str, str, str]:
    s = settings()
    link = f"{s.base_url.rstrip('/')}/a/{a.link_token}"
    subject = f"{s.team_name}: {len(lines)} thing{'' if len(lines) == 1 else 's'} to finish"
    text = "\n".join([f"Hi {a.first_name},", "", "A quick reminder from your coaches. Please:", ""]
                     + [f"  • {x}" for x in lines]
                     + ["", f"Your page: {link}", "", "To stop these emails, untick “Email me reminders” on your page.", "", s.team_name])
    items = "".join(f'<li style="margin:6px 0">{escape(x)}</li>' for x in lines)
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;color:#1B1B1F">
<div style="background:#A6192E;color:#fff;padding:14px 18px;border-bottom:4px solid #C99A2E;font-weight:bold;font-size:18px">{escape(s.team_name)}</div>
<div style="padding:16px 18px">
<p>Hi {escape(a.first_name)},</p><p>A quick reminder from your coaches. Please:</p>
<ul style="padding-left:20px">{items}</ul>
<p><a href="{escape(link)}" style="display:inline-block;background:#A6192E;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:bold">Open my page</a></p>
<p style="font-size:12px;color:#6B645C">To stop these emails, untick “Email me reminders” on your page.</p>
</div></div>"""
    return subject, text, html


def compose_link(a: Athlete) -> tuple[str, str, str]:
    """The welcome email with an athlete's own page link and how to use it."""
    s = settings()
    link = f"{s.base_url.rstrip('/')}/a/{a.link_token}"
    subject = f"{s.team_name}: your training page"
    steps = [
        ("Save it", "Open the link on your phone and add it to your home screen (Share, then Add to Home Screen), so it's one tap away."),
        ("Connect Garmin", "If you run with a Garmin watch, tap Connect Garmin on your page once. Your runs then arrive on their own."),
        ("After each run", "Rate how hard it felt (0 to 10) and how your legs felt. It takes a few seconds and shapes your training."),
        ("If something hurts", "Report pain on your page the same day. Your coaches and the athletic trainer see it."),
        ("Meets", "Answer whether you'll be at each meet, so your coaches can enter the races."),
        ("Reminders", "Tick Email me reminders on your page if you'd like a nudge when something's left to do."),
    ]
    text = "\n".join([f"Hi {a.first_name},", "", "Here is your own training page from your coaches:", "", link, ""]
                     + [f"  • {t}: {d}" for t, d in steps]
                     + ["", "This link is just for you: please don't share it, since it opens your page without a password.",
                        "Questions? Reply to this email.", "", s.team_name])
    items = "".join(f'<li style="margin:8px 0"><b>{escape(t)}.</b> {escape(d)}</li>' for t, d in steps)
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;color:#1B1B1F">
<div style="background:#A6192E;color:#fff;padding:14px 18px;border-bottom:4px solid #C99A2E;font-weight:bold;font-size:18px">{escape(s.team_name)}</div>
<div style="padding:16px 18px">
<p>Hi {escape(a.first_name)},</p><p>Here is your own training page from your coaches:</p>
<p><a href="{escape(link)}" style="display:inline-block;background:#A6192E;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:bold">Open my page</a></p>
<p style="font-size:12px;color:#6B645C;word-break:break-all">{escape(link)}</p>
<ul style="padding-left:20px">{items}</ul>
<p style="font-size:12px;color:#6B645C">This link is just for you: please don’t share it, since it opens your page without a password. Questions? Reply to this email.</p>
</div></div>"""
    return subject, text, html


def send_link(db: Session, a: Athlete, http: httpx.Client | None = None) -> None:
    """Email an athlete their page link; raises EmailError (no email address, or the service refused it)."""
    if not a.email:
        raise EmailError(f"{a.name} has no email address yet.")
    send_email(a.email, *compose_link(a), http=http, reply=reply_to(db))
    from .models import utcnow
    a.link_sent_at = utcnow()


REPLY_TO_KEY = "email_reply_to"


def reply_to(db: Session) -> str:
    """Where replies to the app's emails go (set on the Reminder emails page): the sender's domain has no inbox."""
    return A.get_setting(db, REPLY_TO_KEY, "")


def send_email(to: str, subject: str, text: str, html: str, http: httpx.Client | None = None, reply: str = "") -> str:
    s = settings()
    if not s.email_configured:
        raise EmailError("Email isn’t set up yet: add RESEND_API_KEY and EMAIL_FROM.")
    client = http or httpx.Client(timeout=20)
    try:
        r = client.post(RESEND_URL, headers={"Authorization": f"Bearer {s.resend_api_key}"},
                        json={"from": s.email_from, "to": [to], "subject": subject, "text": text, "html": html,
                              **({"reply_to": [reply]} if reply else {})})
    finally:
        if http is None:
            client.close()
    if r.status_code >= 300:
        try:
            said = r.json().get("message", "")
        except ValueError:
            said = r.text[:200]
        raise EmailError(f"The email service refused it ({r.status_code}): {said}")
    return (r.json() or {}).get("id", "")


def recipients(db: Session, today: dt.date) -> list[tuple[Athlete, list[str]]]:
    items = chosen_items(db)
    people = db.scalars(select(Athlete).where(Athlete.active.is_(True), Athlete.email_reminders.is_(True), Athlete.email != "")
                        .order_by(Athlete.last_name, Athlete.first_name)).all()
    return [(a, lines) for a in people if (lines := todo(db, a, today, items))]


def run(db: Session, today: dt.date, http: httpx.Client | None = None) -> dict:
    """Send today's reminders, once per day."""
    if A.get_setting(db, "reminders_last_sent", "") == today.isoformat():
        return {"skipped": "already sent today"}
    out = {"sent": 0, "errors": 0}
    for a, lines in recipients(db, today):
        try:
            send_email(a.email, *compose(a, lines), http=http, reply=reply_to(db))
            out["sent"] += 1
        except (EmailError, httpx.HTTPError) as exc:
            out["errors"] += 1
            log(db, "email", f"Reminder to {a.name} not sent: {exc}", "error")
    A.set_setting(db, "reminders_last_sent", today.isoformat())
    log(db, "email", f"Reminder emails: {out['sent']} sent" + (f", {out['errors']} failed" if out["errors"] else "") + ".")
    return out

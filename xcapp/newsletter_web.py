"""Coach pages for the weekly newsletter. Registered from web.create_app with that app's helpers."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import newsletter as NL
from . import photos as P
from .ingest import log
from .models import (Athlete, NewsletterDeadline, NewsletterIssue, NewsletterPhoto, NewsletterSend, RecoveryTip, Spotlight,
                     StaffUser, utcnow)
from .reminders import EmailError, send_email

MAX_PHOTOS = 8
PHOTO_SIZE = 1200   # pixels on the longest side

STARTER_TIPS = [
    ("Sleep", "Aim for 8 to 10 hours of sleep. Your body does most of its rebuilding while you sleep."),
    ("Sleep", "Keep a steady bedtime, even on weekends. A regular rhythm makes sleep come easier."),
    ("Sleep", "Put the phone away 30 minutes before bed. Screens make it harder to fall asleep."),
    ("Sleep", "A short nap (20 to 30 minutes) after school can help on hard training days."),
    ("Hydration", "Carry a water bottle at school and sip through the day, not just at practice."),
    ("Hydration", "Pale yellow urine is a good sign you’re drinking enough."),
    ("Hydration", "On hot days, drink before practice as well as after."),
    ("Hydration", "After a long or sweaty run, a drink with some salt (like a sports drink) helps you recover."),
    ("Fueling", "Eat a snack within 30 to 60 minutes after hard workouts to help your muscles recover."),
    ("Fueling", "Breakfast matters: it’s the fuel for your whole school day and afternoon practice."),
    ("Fueling", "Pack a snack for after school so you have energy for practice."),
    ("Fueling", "Fruits and vegetables of different colors give your body a range of vitamins."),
    ("Fueling", "Eat enough to match your training. Running more means your body needs more fuel."),
    ("Fueling", "The night before a race, eat a normal, familiar meal. Race day is not the time to try new foods."),
    ("Recovery", "Easy days should feel easy. Running them slowly is what makes the hard days work."),
    ("Recovery", "A few minutes of easy stretching or mobility after runs helps you feel loose the next day."),
    ("Recovery", "Feeling a sharp pain, or pain that changes how you run? Tell a coach or the trainer the same day."),
    ("Recovery", "Rest days are part of training. Your body gets fitter while it recovers."),
    ("Recovery", "Replace running shoes every 300 to 500 miles. Worn-out shoes can lead to sore legs."),
    ("Recovery", "A cool-down jog after workouts helps your legs recover faster than stopping right away."),
    ("General", "Log your effort after each run. It helps your coaches plan the right training for you."),
    ("General", "Lay out your gear the night before races so race morning is calm."),
    ("General", "Talk to your coaches about how training feels. They want to hear from you."),
    ("General", "Strides (short, relaxed fast runs) after easy runs keep your legs quick."),
]


def register(app, h: SimpleNamespace) -> None:
    """h: render, flash, back, form_checked, any_staff, coach_or_admin, get_db, logo_bytes, settings, templates."""
    render, flash, back, form_checked, get_db = h.render, h.flash, h.back, h.form_checked, h.get_db
    any_staff, coach_or_admin, s = h.any_staff, h.coach_or_admin, h.settings
    h.templates.env.globals["streak_max"] = NL.STREAK_LOOKBACK   # a streak this long reads "8+": the app looks back no further

    def get_issue(db: Session, iid: int) -> NewsletterIssue:
        issue = db.get(NewsletterIssue, iid)
        if issue is None:
            raise HTTPException(404, "Issue not found.")
        return issue

    def photo_url(p: NewsletterPhoto) -> str:
        return f"{s.base_url.rstrip('/')}/newsletter/photo/{p.id}/{NL.photo_token(s.secret_key, p.id)}.jpg"

    def email_html(db: Session, issue: NewsletterIssue) -> str:
        secs = NL.rendered(db, issue, photo_url)
        return h.templates.get_template("newsletter_email.html").render(
            issue=issue, sections=secs, team_name=s.team_name, fmt_time=A.fmt_time, date=dt.date, ordinal=NL._ordinal,
            width=NL.EMAIL_WIDTH, base_url=s.base_url.rstrip("/"))

    # ---------- issues ----------

    @app.get("/newsletter", response_class=HTMLResponse)
    def newsletter_list(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        issues = db.scalars(select(NewsletterIssue).order_by(NewsletterIssue.week_start.desc())).all()
        team = db.scalars(select(Athlete).where(A.on_team())).all()
        return render(request, "newsletter_list.html", user, issues=issues, labels=NL.STATUS_LABELS,
                      default_week=NL.default_week(A.team_today()), team_goal=A.get_setting(db, "newsletter_team_goal_miles", ""),
                      consent_named=sum(1 for a in team if a.show_name), team_size=len(team),
                      tips_approved=db.query(RecoveryTip).filter(RecoveryTip.approved_by != "").count())

    @app.post("/newsletter/new")
    async def newsletter_new(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            monday = A.week_monday(dt.date.fromisoformat(str(f.get("week", ""))))
        except ValueError:
            flash(request, "Pick a date in the week the issue is about.", "error")
            return back("/newsletter")
        issue = db.scalar(select(NewsletterIssue).where(NewsletterIssue.week_start == monday))
        if issue is None:
            issue = NL.create_issue(db, monday, user.username)
            log(db, "newsletter", f"{user.username} started the issue for the week of {monday:%b %-d}.")
            flash(request, "Draft started from this week’s data. Write the coach sections, check the rest, then approve.")
        return back(f"/newsletter/{issue.id}")

    @app.post("/newsletter/settings")
    async def newsletter_settings(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        raw = str(f.get("team_goal", "")).strip()
        try:
            goal = float(raw) if raw else 0
            if not 0 <= goal <= 100000:
                raise ValueError
        except ValueError:
            flash(request, "The team goal is a number of miles, such as 6000.", "error")
            return back("/newsletter")
        A.set_setting(db, "newsletter_team_goal_miles", f"{goal:g}" if goal else "")
        flash(request, "Team goal saved. It shows in Team numbers from the next refresh.")
        return back("/newsletter")

    @app.get("/newsletter/{iid}", response_class=HTMLResponse)
    def newsletter_edit(request: Request, iid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        issue = get_issue(db, iid)
        people = NL.consent_map(db)
        team = db.scalars(select(Athlete).where(A.on_team()).order_by(Athlete.last_name, Athlete.first_name)).all()
        import json
        secs = [{"sec": sec, "spec": NL.SPEC.get(sec.key), "data": json.loads(sec.data_snapshot) if sec.data_snapshot else None}
                for sec in sorted(issue.sections, key=lambda x: x.position) if sec.key in NL.SPEC]
        sends = db.scalars(select(NewsletterSend).where(NewsletterSend.issue_id == issue.id).order_by(NewsletterSend.sent_at)).all()
        return render(request, "newsletter_edit.html", user, issue=issue, secs=secs, labels=NL.STATUS_LABELS, people=people, team=team,
                      problems=NL.problems(issue), preview=NL.rendered(db, issue, lambda p: f"/newsletter/{issue.id}/photos/{p.id}.jpg"),
                      sends=sends, photo_ok=lambda p: NL.photo_ok(p, people), ordinal=NL._ordinal,
                      recipients=[u for u in db.scalars(select(StaffUser).order_by(StaffUser.username)).all() if u.email],
                      editable=user.role in ("coach", "admin") and issue.status != "sent", date=dt.date,
                      words=lambda t: len((t or "").split()), max_photos=MAX_PHOTOS, left_out=NL.left_out)

    @app.post("/newsletter/{iid}/save")
    async def newsletter_save(request: Request, iid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        issue = get_issue(db, iid)
        if issue.status == "sent":
            flash(request, "This issue was sent and can’t change. Put any correction in next week’s issue.", "error")
            return back(f"/newsletter/{iid}")
        changed = False
        for sec in issue.sections:
            enabled = bool(f.get(f"enabled_{sec.key}"))
            text = str(f.get(f"text_{sec.key}", sec.coach_text or "")).strip()[:4000]
            try:
                pos = int(f.get(f"position_{sec.key}", sec.position))
            except ValueError:
                pos = sec.position
            new = (enabled, text, pos)
            if sec.key == "spotlight":
                raw = str(f.get("spotlight_athlete", "")).strip()
                aid = int(raw) if raw.isdigit() else None
                if aid != sec.athlete_id:
                    sec.athlete_id, changed = aid, True
            if new != (sec.enabled, sec.coach_text or "", sec.position):
                sec.enabled, sec.coach_text, sec.position = new
                changed = True
        if changed:
            was = issue.status
            NL.back_to_draft(issue)
            flash(request, "Saved." + (" It needs approving again because it changed." if was in ("approved", "ready_for_review") else ""))
        else:
            flash(request, "No changes.")
        return back(f"/newsletter/{iid}")

    @app.post("/newsletter/{iid}/refresh")
    async def newsletter_refresh(request: Request, iid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        issue = get_issue(db, iid)
        try:
            NL.refresh(db, issue)
            flash(request, "Numbers refreshed from the latest data. Your writing was kept.")
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back(f"/newsletter/{iid}")

    @app.post("/newsletter/{iid}/status")
    async def newsletter_status(request: Request, iid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        issue = get_issue(db, iid)
        action = str(f.get("action", ""))
        try:
            if action == "approved":
                bad = NL.problems(issue)
                if bad:
                    raise ValueError(" ".join(bad))
            NL.set_status(issue, action, user)
            if action == "approved":
                log(db, "newsletter", f"{user.username} approved the issue for the week of {issue.week_start:%b %-d}.")
            flash(request, {"approved": "Approved. Send it when you’re ready.", "ready_for_review": "Marked ready for review.",
                            "draft": "Back to draft."}.get(action, "Saved."))
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back(f"/newsletter/{iid}")

    @app.post("/newsletter/{iid}/send")
    async def newsletter_send(request: Request, iid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        issue = get_issue(db, iid)
        if issue.status != "approved":
            flash(request, "Approve the issue before sending it.", "error")
            return back(f"/newsletter/{iid}")
        to = [u for u in db.scalars(select(StaffUser)).all() if u.email]
        if not to:
            flash(request, "No coach has an email address yet. Add them on the Staff page.", "error")
            return back(f"/newsletter/{iid}")
        html = email_html(db, issue)
        text = NL.plain_text(issue, NL.rendered(db, issue, photo_url), s.team_name)
        subj = NL.subject(issue, s.team_name)
        sent, failed, last_err = 0, 0, ""
        for u in to:
            try:
                send_email(u.email, subj, text, html)
                sent += 1
            except EmailError as exc:
                failed, last_err = failed + 1, str(exc)
        if not sent:
            flash(request, f"Nothing was sent: {last_err}", "error")
            log(db, "newsletter", f"Sending the issue for the week of {issue.week_start:%b %-d} failed: {last_err}", "error")
            return back(f"/newsletter/{iid}")
        db.add(NewsletterSend(issue_id=issue.id, recipient_group="coaches", recipients=sent, failed=failed, sent_by=user.username))
        issue.status, issue.sent_by, issue.sent_at = "sent", user.username, utcnow()
        spot = next((x for x in issue.sections if x.key == "spotlight" and x.enabled and x.athlete_id and (x.coach_text or "").strip()), None)
        if spot:
            db.add(Spotlight(athlete_id=spot.athlete_id, issue_id=issue.id))
        tip_sec = next((x for x in issue.sections if x.key == "recovery_tip" and x.enabled and x.data_snapshot), None)
        if tip_sec:
            import json
            tip = db.get(RecoveryTip, json.loads(tip_sec.data_snapshot).get("tip_id"))
            if tip:
                tip.last_used = A.team_today()
        log(db, "newsletter", f"{user.username} sent the issue for the week of {issue.week_start:%b %-d} to {sent} coach"
                              f"{'' if sent == 1 else 'es'}" + (f"; {failed} failed: {last_err}" if failed else "."), "error" if failed else "info")
        flash(request, f"Sent to {sent} coach{'' if sent == 1 else 'es'}." + (f" {failed} failed: {last_err}" if failed else ""),
              "error" if failed else "ok")
        return back(f"/newsletter/{iid}")

    @app.post("/newsletter/{iid}/delete")
    async def newsletter_delete(request: Request, iid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        issue = get_issue(db, iid)
        if issue.status == "sent":
            flash(request, "Sent issues are kept as a record.", "error")
            return back(f"/newsletter/{iid}")
        db.delete(issue)
        log(db, "newsletter", f"{user.username} deleted the draft for the week of {issue.week_start:%b %-d}.")
        flash(request, "Draft deleted.")
        return back("/newsletter")

    @app.get("/newsletter/{iid}/view", response_class=HTMLResponse)
    def newsletter_view(request: Request, iid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        issue = get_issue(db, iid)
        return render(request, "newsletter_view.html", user, issue=issue, ordinal=NL._ordinal, date=dt.date, labels=NL.STATUS_LABELS,
                      sections=NL.rendered(db, issue, lambda p: f"/newsletter/{issue.id}/photos/{p.id}.jpg"))

    @app.get("/newsletter/{iid}/email", response_class=HTMLResponse)
    def newsletter_email_preview(iid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        return HTMLResponse(email_html(db, get_issue(db, iid)))

    @app.get("/newsletter/{iid}/issue.pdf")
    def newsletter_pdf(iid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        issue = get_issue(db, iid)
        secs = NL.rendered(db, issue)
        data = NL.as_pdf(issue, secs, s.team_name, h.logo_bytes(db), lambda pid: (db.get(NewsletterPhoto, pid) or SimpleNamespace(data=None)).data)
        name = f"weekly-update-{issue.week_start.isoformat()}.pdf"
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})

    # ---------- photos ----------

    @app.post("/newsletter/{iid}/photos")
    async def newsletter_photo_add(request: Request, iid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        issue = get_issue(db, iid)
        try:
            if issue.status == "sent":
                raise ValueError("This issue was sent and can’t change.")
            if len(issue.photos) >= MAX_PHOTOS:
                raise ValueError(f"Up to {MAX_PHOTOS} photos an issue.")
            up = f.get("photo")
            if not up or not getattr(up, "filename", ""):
                raise ValueError("Choose a photo.")
            alt = str(f.get("alt", "")).strip()[:200]
            if not alt:
                raise ValueError("Describe the photo in a few words (for readers who can’t see it).")
            caption = str(f.get("caption", "")).strip()[:200]
            bad = NL.blocked_words(alt + " " + caption)
            if bad:
                raise ValueError(f"Please leave out “{', '.join(bad)}”.")
            ids = sorted({int(x) for x in f.getlist("athletes") if str(x).isdigit()})
            data = P.prepare(await up.read(), PHOTO_SIZE)   # also strips EXIF, including GPS location
            issue.photos.append(NewsletterPhoto(data=data, alt=alt, caption=caption, athlete_ids=",".join(map(str, ids)),
                                                uploaded_by=user.username))
            NL.back_to_draft(issue)
            people = NL.consent_map(db)
            missing = [people[i].name for i in ids if i in people and not people[i].show_photo]
            flash(request, "Photo added." + (f" It stays out of the issue until these athletes have photo consent: {', '.join(missing)}." if missing else ""),
                  "error" if missing else "ok")
        except (ValueError, P.PhotoError) as exc:
            flash(request, str(exc), "error")
        return back(f"/newsletter/{iid}#photos")

    @app.post("/newsletter/{iid}/photos/{pid}/delete")
    async def newsletter_photo_delete(request: Request, iid: int, pid: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        issue = get_issue(db, iid)
        ph = db.get(NewsletterPhoto, pid)
        if ph and ph.issue_id == issue.id and issue.status != "sent":
            db.delete(ph)
            NL.back_to_draft(issue)
            flash(request, "Photo removed.")
        return back(f"/newsletter/{iid}#photos")

    @app.get("/newsletter/{iid}/photos/{pid}.jpg")
    def newsletter_photo_staff(iid: int, pid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        ph = db.get(NewsletterPhoto, pid)
        if ph is None or ph.issue_id != iid:
            raise HTTPException(404, "Photo not found.")
        return Response(ph.data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/newsletter/photo/{pid}/{token}.jpg")
    def newsletter_photo_email(pid: int, token: str, db: Session = Depends(get_db, scope="function")):
        """For email clients: the link itself is the key, and only photos whose athletes all have consent are served."""
        import hmac
        ph = db.get(NewsletterPhoto, pid)
        if ph is None or not hmac.compare_digest(token, NL.photo_token(s.secret_key, pid)) or not NL.photo_ok(ph, NL.consent_map(db)):
            raise HTTPException(404, "Photo not found.")
        return Response(ph.data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ---------- deadlines ----------

    @app.get("/newsletter-deadlines", response_class=HTMLResponse)
    def deadlines_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.scalars(select(NewsletterDeadline).order_by(NewsletterDeadline.due_date)).all()
        return render(request, "newsletter_deadlines.html", user, rows=rows, today=A.team_today())

    @app.post("/newsletter-deadlines")
    async def deadlines_add(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            title = str(f.get("title", "")).strip()[:160]
            if not title:
                raise ValueError("Give the deadline a name.")
            due = dt.date.fromisoformat(str(f.get("due", "")))
            days = int(f.get("show_from", 14) or 14)
            if not 1 <= days <= 120:
                raise ValueError("Show it from 1 to 120 days before.")
            db.add(NewsletterDeadline(title=title, due_date=due, show_from_days=days, notes=str(f.get("notes", "")).strip()[:300]))
            flash(request, "Deadline added. It appears in issues from that many days before it’s due.")
        except ValueError as exc:
            flash(request, str(exc) if "deadline" in str(exc).lower() or "days" in str(exc) else "Check the due date.", "error")
        return back("/newsletter-deadlines")

    @app.post("/newsletter-deadlines/{did}/delete")
    async def deadlines_delete(request: Request, did: int, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        d = db.get(NewsletterDeadline, did)
        if d:
            db.delete(d)
            flash(request, "Deadline removed.")
        return back("/newsletter-deadlines")

    # ---------- recovery tips ----------

    @app.get("/newsletter-tips", response_class=HTMLResponse)
    def tips_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        rows = db.scalars(select(RecoveryTip).order_by(RecoveryTip.category, RecoveryTip.id)).all()
        return render(request, "newsletter_tips.html", user, rows=rows, categories=("Sleep", "Hydration", "Fueling", "Recovery", "General"))

    @app.post("/newsletter-tips")
    async def tips_add(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        if f.get("starter"):
            have = {t.text for t in db.scalars(select(RecoveryTip)).all()}
            new = [RecoveryTip(category=c, text=t) for c, t in STARTER_TIPS if t not in have]
            db.add_all(new)
            flash(request, f"Added {len(new)} starter tips. Each one needs approving before it’s used.")
            return back("/newsletter-tips")
        text = str(f.get("text", "")).strip()[:300]
        bad = NL.blocked_words(text)
        if not text:
            flash(request, "Write the tip.", "error")
        elif bad:
            flash(request, f"Tips never mention body weight or dieting (found “{', '.join(bad)}”).", "error")
        else:
            db.add(RecoveryTip(text=text, category=str(f.get("category", "General"))[:30]))
            flash(request, "Tip added. Approve it to use it.")
        return back("/newsletter-tips")

    @app.post("/newsletter-tips/{tid}")
    async def tips_change(request: Request, tid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        t = db.get(RecoveryTip, tid)
        if t is None:
            raise HTTPException(404, "Tip not found.")
        action = f.get("action")
        if action == "approve" and not NL.blocked_words(t.text):
            t.approved_by = user.username   # coaches, admins or the athletic trainer
            flash(request, "Tip approved.")
        elif action == "unapprove":
            t.approved_by = ""
            flash(request, "Tip set aside.")
        elif action == "delete" and user.role in ("coach", "admin"):
            db.delete(t)
            flash(request, "Tip deleted.")
        return back("/newsletter-tips")

    # ---------- consent ----------

    @app.get("/newsletter-consent", response_class=HTMLResponse)
    def consent_page(request: Request, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        team = db.scalars(select(Athlete).where(A.on_team()).order_by(Athlete.last_name, Athlete.first_name)).all()
        return render(request, "newsletter_consent.html", user, team=team)

    @app.post("/newsletter-consent")
    async def consent_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        team = db.scalars(select(Athlete).where(A.on_team())).all()
        basis = str(f.get("basis", "")).strip()[:100]
        everyone = f.get("everyone") == "release"
        changes = []
        for a in team:
            if everyone:
                new = (True, True, True, a.show_vdot)
            else:
                new = tuple(bool(f.get(f"{k}_{a.id}")) for k in ("name", "photo", "spotlight", "vdot"))
            old = (a.show_name, a.show_photo, a.show_spotlight, a.show_vdot)
            if new != old:
                a.show_name, a.show_photo, a.show_spotlight, a.show_vdot = new
                a.consent_by = f"{user.username}" + (f": {basis}" if basis else (": school media release" if everyone else ""))
                a.consent_at = utcnow()
                changes.append(f"{a.name} {''.join('y' if x else 'n' for x in old)}→{''.join('y' if x else 'n' for x in new)}")
        if changes:   # audit trail: name, photo, spotlight, VDOT (y/n), before and after
            log(db, "consent", f"{user.username} changed newsletter consent" + (" (school release)" if everyone else "") + ": " + "; ".join(changes))
        flash(request, f"Saved consent for {len(changes)} athlete{'' if len(changes) == 1 else 's'}." if changes else "No changes.")
        return back("/newsletter-consent")

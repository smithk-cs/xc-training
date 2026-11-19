"""The athlete's own pages, opened with their private link (/a/{token}): their page, runs and efforts, typed-in runs and
uploads, pain and missed days, meets, reminders, Garmin, AI consent, their data and deleting it. Registered by
web.create_app, which passes in the helpers these share with the rest of the app."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import analysis as A
from . import courses as CS
from . import form_plan as FP
from . import garmin as G
from . import medals as MD
from . import points as PT
from . import push as PU
from . import registration as REG
from . import team_calendar as TC
from . import weather as WX
from . import wellness as WL
from . import workout_view as WV
from .ingest import log, rematch_day
from .models import (Activity, Announcement, CaptainPost, DeletionRequest, MissedDay, PainReport, PlannedWorkout, RaceResult,
                     RaceTeamResult, RestingHeartRate, utcnow)
from .web import BODY_AREAS, FEELS, get_db


def reflection_bits(db: Session, a, races, today: dt.date) -> dict:
    """The athlete's race reflections, and the recent race still to reflect on (reflections.py)."""
    from . import reflections as RF
    refs = RF.by_race(db, a.id)
    return {"reflections": refs, "reflect_next": RF.to_ask(races, refs, today)}


def return_view(db: Session, a, today: dt.date):
    """The athlete's return-to-run plan for their page: an active or paused one, or one finished in the last week."""
    from . import return_run as RR
    from .models import ReturnPlan
    p = RR.active(db, a.id) or db.scalar(select(ReturnPlan).where(
        ReturnPlan.athlete_id == a.id, ReturnPlan.status == "done", ReturnPlan.finished_on >= today - dt.timedelta(days=7)))
    return RR.view(db, p, today) if p is not None else None


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, form_checked, s = h.render, h.flash, h.back, h.form_checked, h.settings
    athlete_by_token, photo_response, photo_versions, route_pdf = h.athlete_by_token, h.photo_response, h.photo_versions, h.route_pdf
    upload, add_typed_run, own_run, unrated_runs, missed_day = h.upload, h.add_typed_run, h.own_run, h.unrated_runs, h.missed_day
    apply_type_and_workout, workout_choices, series_with_goal = h.apply_type_and_workout, h.workout_choices, h.series_with_goal
    get_meet, my_meets, set_response = h.get_meet, h.my_meets, h.set_response

    @app.get("/a/{token}/routes/{rid}.pdf")
    def athlete_route_view(token: str, rid: int, db: Session = Depends(get_db, scope="function")):
        from .models import CourseRoute
        athlete_by_token(db, token)   # athletes and families with the athlete's own link
        r = db.get(CourseRoute, rid)
        if r is None:
            raise HTTPException(404, "Route map not found.")
        return route_pdf(r)

    @app.get("/a/{token}", response_class=HTMLResponse)
    def athlete_page(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).order_by(Activity.start_utc.desc()).limit(10)).all()
        today = A.team_today()
        open_delete = db.scalar(select(DeletionRequest).where(DeletionRequest.athlete_id == a.id, DeletionRequest.status == "open"))
        unrated = unrated_runs(db, a, today)
        need, first_unrated = len(unrated), (unrated[0] if unrated else None)
        roster, bench = A.roster_rows(db, athlete=a), A.benchmark_rows(db, athlete=a)
        prog, seasons = A.progress(bench, A.goal_rows(db, athlete=a), roster)
        tiles = [p for p in prog if p["current"] is not None]
        show_group = A.get_setting(db, "show_group_to_athletes", "1") == "1"
        mine = A.abilities(db, today).get(a.id, {})
        ability = mine if show_group else {}
        coming = db.scalars(select(PlannedWorkout).where(PlannedWorkout.date >= today, PlannedWorkout.date <= today + dt.timedelta(days=7))
                            .order_by(PlannedWorkout.date, PlannedWorkout.id)).all()
        done = set(db.scalars(select(Activity.planned_workout_id).where(  # workouts already run come off the list
            Activity.athlete_id == a.id, Activity.planned_workout_id.in_([w.id for w in coming]))).all())
        paces = A.workout_paces(db, a, [w for w in coming if w.applies_to(a.group, mine.get("group") or "", gender=a.gender) and w.id not in done])
        races = db.scalars(select(RaceResult).where(RaceResult.athlete_id == a.id)
                           .order_by(RaceResult.date.desc(), RaceResult.id.desc())).all()
        history = A.race_history(races)
        videos = {(t.date, t.meet, t.race): t.video_url for t in db.scalars(select(RaceTeamResult).where(
            RaceTeamResult.video_url != "", RaceTeamResult.date.in_({r.date for r in races} or {today}))).all()}
        from .form_web import for_athlete as form_reports
        return render(request, "athlete.html", None, history=history, videos=videos, forms=form_reports(db, a.id),
                      form_focus=FP.focus(FP.latest(db, [a.id]).get(a.id)), form_hist=FP.history(db, a.id), adjusted=CS.adjusted_times(db, races), paces=paces, a=a, acts=acts, tiles=tiles, seasons=seasons, need=need, first_unrated=first_unrated, ability=ability,
                      show_group=show_group, open_delete=open_delete, races=races, vo2=A.latest_vo2(db, a.id),
                      rhr=A.resting_hr(db, a.id, today), rhr_range=(A.RESTING_HR_MIN, A.RESTING_HR_MAX),
                      rhr_cal=A.resting_hr_calendar(db, a.id, today), photo_v=photo_versions(db).get(a.id), meets=my_meets(db, a, today),
                      wins=A.achievements(db, a, today),
                      calendar_link=f"/team-calendar/{TC.token(db)}?team={a.gender if a.gender in ('Boys', 'Girls') else 'All'}",
                      workout_weather={x["workout"].date: WX.practice_forecast(db, x["workout"].date, a.gender if a.gender in ("Boys", "Girls") else "All")
                                       for x in paces},
                      captain_posts=db.scalars(select(CaptainPost).where(CaptainPost.athlete_id == a.id, CaptainPost.created_at >=
                                                                         dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=14))
                                               .order_by(CaptainPost.created_at.desc())).all() if a.is_captain else [],
                      miles_goal=A.mileage_goal(db, a.id, mine.get("group") or "", today, a.gender, a.experience) if show_group else None,
                      my_points=PT.team_week(db, A.week_monday(today), athletes=[a])[0] if show_group else None, rates=PT.RATES,
                      medals=MD.summary(db, a, today) if show_group else None, vdot_move=A.vdot_moves(db, today).get(a.id),
                      notes=[n for n in db.scalars(select(Announcement).where(Announcement.start_date <= today, Announcement.end_date >= today)
                                                   .order_by(Announcement.start_date.desc(), Announcement.id.desc())).all()
                             if n.for_athlete(a.gender, mine.get("group") or "")], paces_link=f"/a/{token}/paces" if show_group and mine.get("vdot") else None,
                      areas=BODY_AREAS, token=token, today=today.isoformat(), push_key=PU.public_key(db),
                      push_phones=len(PU.subscriptions(db, [a.id])), rtr=return_view(db, a, today),
                      checkin=next(iter(WL.for_athlete(db, a.id, today, days=1)), None), ws=WL, wl=WL.label,
                      **reflection_bits(db, a, races, today))

    @app.post("/a/{token}/connect")
    async def athlete_connect(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        if not f.get("consent"):
            flash(request, "A parent or guardian needs to approve first. Tick the box when they have.", "error")
            return back(f"/a/{token}")
        try:
            url = G.start_authorization(db, a, "Athlete confirmed parent/guardian approval on the team link page.")
        except G.GarminError as exc:
            flash(request, str(exc), "error")
            return back(f"/a/{token}")
        request.session["athlete_token"] = token
        return render(request, "garmin_go.html", None, url=url)

    @app.post("/a/{token}/disconnect")
    async def athlete_disconnect(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a = athlete_by_token(db, token)
        if a.garmin and a.garmin.status != "revoked":
            with G.make_client() as http:
                G.disconnect(db, a.garmin, http)
            log(db, "garmin", f"{a.name} disconnected Garmin.")
        flash(request, "Garmin is disconnected. Runs you already shared stay with your team.")
        return back(f"/a/{token}")

    @app.post("/a/{token}/upload")
    async def athleteupload(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        flash(request, *await upload(db, a, f.getlist("file")))
        return back(f"/a/{token}")

    @app.post("/a/{token}/runs/new")
    async def athlete_add_run(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        act, msg, level = add_typed_run(db, a, f)
        flash(request, msg + (" Now add how hard it felt." if level == "ok" and act.rpe is None else ""), level)
        return back(f"/a/{token}/runs/{act.id}#effort" if level == "ok" else f"/a/{token}#add-runs")

    @app.post("/a/{token}/runs/{activity_id}/delete")
    async def athlete_delete_run(request: Request, token: str, activity_id: int, db: Session = Depends(get_db, scope="function")):
        """An athlete can delete a run they typed in (to fix a mistake), not one from their watch."""
        await form_checked(request)
        a = athlete_by_token(db, token)
        act = own_run(db, a, activity_id)
        if not act.is_manual:
            raise HTTPException(404, "Run not found.")
        day = act.local_date
        db.delete(act)
        db.flush()
        rematch_day(db, day, athlete_id=a.id)   # another run that day may now be the team workout
        flash(request, f"Deleted the run you typed in for {day:%b %-d}.")
        return back(f"/a/{token}/runs")

    @app.post("/a/{token}/pain")
    async def athlete_pain(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        try:
            sev = int(f.get("severity", -1))
            area = f.get("body_area")
            if not 0 <= sev <= 10 or area not in BODY_AREAS:
                raise ValueError("Pick where it hurts and how much (0 to 10).")
            db.add(PainReport(athlete_id=a.id, date=A.team_today(), body_area=area, severity=sev,
                              note=str(f.get("note", ""))[:300], saw_trainer=bool(f.get("saw_trainer"))))
            db.flush()
            A.dedupe_pain_reports(db, a.id)   # a second report for the same day and place replaces the first
            flash(request, "Thanks. Your coach will see this today." + (" Please also tell your coach or athletic trainer in person." if sev >= A.PAIN_FLAG else ""))
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back(f"/a/{token}")

    @app.post("/a/{token}/medal-note")
    async def athlete_medal_note(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        f = await form_checked(request)
        try:
            MD.close_note(db, a, dt.date.fromisoformat(f.get("monday", "")))
        except ValueError:
            pass
        return back(f"/a/{token}")

    @app.post("/a/{token}/resting-hr")
    async def athlete_resting_hr(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        try:
            bpm = int(str(f.get("bpm", "")).strip())
        except ValueError:
            bpm = 0
        today = A.team_today()
        try:
            day = dt.date.fromisoformat(str(f.get("date") or today.isoformat()))
        except ValueError:
            day = None
        if day is None or not today - dt.timedelta(days=A.RESTING_HR_BACKDATE) <= day <= today:
            flash(request, f"Pick a day from the past {A.RESTING_HR_BACKDATE} days.", "error")
        elif not A.RESTING_HR_MIN <= bpm <= A.RESTING_HR_MAX:
            flash(request, f"Enter your resting heart rate as beats per minute, between {A.RESTING_HR_MIN} and {A.RESTING_HR_MAX}.", "error")
        else:
            A.save_resting_hr(db, a.id, day, bpm, "athlete")
            flash(request, f"Saved: {bpm} beats per minute " + ("this morning." if day == today else f"for {day:%a, %b %-d}."))
        return back(f"/a/{token}#rhr")

    @app.get("/a/{token}/series.json")
    def athlete_series_self(token: str, db: Session = Depends(get_db, scope="function")):
        # The goal comes from the athlete's group, so it follows the "show athletes their own group" setting.
        return JSONResponse(series_with_goal(db, athlete_by_token(db, token), A.get_setting(db, "show_group_to_athletes", "1") == "1"))

    @app.get("/a/{token}/photo")
    def athlete_own_photo(token: str, db: Session = Depends(get_db, scope="function")):
        return photo_response(athlete_by_token(db, token))

    @app.post("/a/{token}/meets/{mid}")
    async def athlete_meet_answer(request: Request, token: str, mid: int, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        m = get_meet(db, mid)
        days = (m.date - A.team_today()).days
        if days <= REG.ATTENDANCE_CUTOFF_DAYS:
            flash(request, f"It’s too close to {m.name} to answer here. Talk to the coaching staff about attendance.", "error")
        elif any(r.athlete_id == a.id for r in m.responses):
            flash(request, f"You’ve already answered for {m.name}. Talk to the coaching staff to change it.", "error")
        elif f.get("status") == "attending":
            set_response(m, a, "attending", "", "athlete")
            flash(request, f"Thanks. You’re confirmed for {m.name}.")
        elif f.get("status") == "absent":
            reason = str(f.get("reason", "")).strip()[:300]
            if not reason:
                flash(request, "Tell your coaches why you can’t be there.", "error")
            else:
                set_response(m, a, "absent", reason, "athlete")
                flash(request, f"Thanks. Your coaches know you won’t be at {m.name}.")
        return back(f"/a/{token}#meets")

    @app.post("/a/{token}/reminders")
    async def athlete_reminders(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        a.email_reminders = bool(f.get("on")) and bool(a.email)
        flash(request, f"You’ll get a reminder email at {a.email} when something needs finishing." if a.email_reminders
              else "Reminder emails are off.")
        return back(f"/a/{token}#reminders")

    @app.get("/a/{token}/paces", response_class=HTMLResponse)
    def athlete_paces(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        """The athlete's times for 100 m to 1600 m at each of the five training zones, from their VDOT."""
        a = athlete_by_token(db, token)
        mine = A.abilities(db, A.team_today()).get(a.id, {})
        shown = A.get_setting(db, "show_group_to_athletes", "1") == "1"
        return render(request, "athlete_paces.html", None, a=a, token=token, ability=mine if shown else {},
                      table=A.pace_table(mine.get("vdot")) if shown else [], zones=A.PACE_ZONES)

    @app.get("/a/{token}/runs", response_class=HTMLResponse)
    def athlete_runs(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).options(selectinload(Activity.planned))
                          .order_by(Activity.start_utc.desc()).limit(60)).all()
        pains = {p.activity_id: p for p in a.pain_reports if p.activity_id}
        today = A.team_today()
        oldest = acts[-1].local_date if acts else today - dt.timedelta(days=14)
        reports = db.scalars(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date >= min(oldest, today - dt.timedelta(days=14)))).all()
        items = [("run", act.local_date, act) for act in acts] + [("missed", m.date, m) for m in reports] \
            + [("gap", d, None) for d in A.unlogged_days(db, a, today)]
        items.sort(key=lambda x: (x[1], x[0] == "run"), reverse=True)
        return render(request, "athlete_runs.html", None, a=a, items=items, pains=pains, token=token)

    @app.get("/a/{token}/runs/{activity_id}", response_class=HTMLResponse)
    def athlete_run(request: Request, token: str, activity_id: int, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        act = own_run(db, a, activity_id)
        pain = next((p for p in a.pain_reports if p.activity_id == act.id), None)
        wv = WV.build(db, act) if (act.planned_workout_id or act.stream or act.laps) else None
        if wv is not None:
            wv["summary"] = WV.summary(wv)
        return render(request, "athlete_run.html", None, a=a, act=act, pain=pain, token=token, areas=BODY_AREAS,
                      choices=workout_choices(db, act), wv=wv)

    @app.post("/a/{token}/runs/{activity_id}")
    async def athlete_run_save(request: Request, token: str, activity_id: int, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        act = own_run(db, a, activity_id)
        try:
            rpe = int(f.get("rpe", ""))
            if not 0 <= rpe <= 10:
                raise ValueError
        except ValueError:
            flash(request, "Pick how hard the run felt, from 0 to 10.", "error")
            return back(f"/a/{token}/runs/{act.id}")
        feel = f.get("feel", "")
        if feel and feel not in FEELS:
            feel = ""
        area = f.get("pain_area", "None")
        level = None
        if area != "None":
            try:
                level = int(f.get("pain_level", ""))
                if area not in BODY_AREAS or not 1 <= level <= 10:
                    raise ValueError
            except ValueError:
                flash(request, "You picked where it hurts. Also pick how much, from 1 to 10.", "error")
                return back(f"/a/{token}/runs/{act.id}")
        note = str(f.get("note", "")).strip()[:300]
        err = apply_type_and_workout(db, act, f)
        if err:
            flash(request, err, "error")
            return back(f"/a/{token}/runs/{act.id}")
        act.rpe, act.feel, act.athlete_note, act.feedback_at = rpe, feel, note, G._now()
        existing = db.scalar(select(PainReport).where(PainReport.activity_id == act.id))
        if level is None:
            if existing is not None:
                db.delete(existing)
        else:
            p = existing or PainReport(athlete_id=a.id, activity_id=act.id)
            p.date, p.body_area, p.severity, p.note = act.local_date, area, level, note
            p.saw_trainer = bool(f.get("saw_trainer"))
            db.add(p)
            db.flush()
            A.dedupe_pain_reports(db, a.id)
        msg = "Saved. Thanks for adding your effort."
        if level is not None and level >= A.PAIN_FLAG:
            msg += " That’s a lot of pain: please also tell your coach or athletic trainer in person."
        db.flush()
        nxt = unrated_runs(db, a, A.team_today())
        if nxt:   # on to the next run still waiting for its effort (its form opens near the top, below the note), else home
            n = len(nxt)
            flash(request, msg + f" {n} more run{'' if n == 1 else 's'} to rate.")
            return back(f"/a/{token}/runs/{nxt[0].id}")
        flash(request, msg)
        return back(f"/a/{token}")

    @app.get("/a/{token}/missed")
    def athlete_missed_start(token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        gaps = A.unlogged_days(db, a, A.team_today())
        return back(f"/a/{token}/missed/{(gaps[0] if gaps else A.team_today()).isoformat()}")

    @app.get("/a/{token}/missed/{day}", response_class=HTMLResponse)
    def athlete_missed(request: Request, token: str, day: str, edit: int = 0, db: Session = Depends(get_db, scope="function")):
        a, d = missed_day(token, day, db)
        report = db.scalar(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date == d))
        ran = db.scalar(select(func.count(Activity.id)).where(Activity.athlete_id == a.id, Activity.local_date == d))
        return render(request, "athlete_missed.html", None, a=a, day=d, report=report, editing=bool(edit or report is None),
                      ran=ran, token=token, areas=BODY_AREAS, today=A.team_today().isoformat())

    @app.post("/a/{token}/missed/{day}")
    async def athlete_missed_save(request: Request, token: str, day: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a, d = missed_day(token, day, db)
        url = f"/a/{token}/missed/{d.isoformat()}"
        reason = f.get("reason", "")
        if reason not in A.MISSED_REASONS:
            flash(request, "Pick why you didn’t run.", "error")
            return back(url + "?edit=1")
        m = db.scalar(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date == d)) or MissedDay(athlete_id=a.id, date=d)
        m.reason = reason
        if reason == "Injury":
            area, side, started, seen, more = (f.get(k, "") for k in ("body_area", "side", "started", "seen_by", "more_days"))
            try:
                pain = int(f.get("pain", ""))
            except ValueError:
                pain = 0
            missing = [label for ok, label in ((area in BODY_AREAS, "where it hurts"), (side in A.SIDES, "which side"),
                                               (1 <= pain <= 10, "how much it hurts")) if not ok]
            if missing:
                flash(request, "Please add " + " and ".join(missing) + ".", "error")
                return back(url + "?edit=1")
            m.body_area, m.side, m.pain = area, side, pain
            m.started = started if started in A.STARTED else ""
            m.seen_by = seen if seen in A.SEEN_BY else ""
            m.more_days = more if more in A.MORE_DAYS else ""
            m.description = str(f.get("description", "")).strip()[:1000]
        else:
            m.body_area = m.side = m.started = m.seen_by = m.more_days = ""
            m.pain = None
            m.description = str(f.get("note", "")).strip()[:1000]
        m.updated_at = utcnow()
        db.add(m)
        msg = "Sent to your coaches."
        if reason == "Injury" and (m.pain or 0) >= A.PAIN_FLAG:
            msg += " That’s a lot of pain: please also tell your coach or athletic trainer in person today."
        flash(request, msg)
        return back(url)

    @app.post("/a/{token}/missed/{day}/delete")
    async def athlete_missed_delete(request: Request, token: str, day: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a, d = missed_day(token, day, db)
        m = db.scalar(select(MissedDay).where(MissedDay.athlete_id == a.id, MissedDay.date == d))
        if m is not None:
            db.delete(m)
            flash(request, "Report removed.")
        return back(f"/a/{token}/runs")

    @app.post("/a/{token}/ai-consent")
    async def athlete_ai_consent(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        if f.get("action") == "withdraw":
            a.ai_consent_at, a.ai_consent_note = None, ""
            log(db, "privacy", f"{a.name} withdrew consent to AI analysis.")
            flash(request, "Done. Your data will not be sent for AI analysis from now on.")
        else:
            if not (f.get("agree") and f.get("parent")):
                flash(request, "To turn on AI analysis, tick both boxes: that you agree, and that a parent or guardian agrees.", "error")
                return back(f"/a/{token}#privacy")
            a.ai_consent_at = utcnow()
            a.ai_consent_note = "Athlete agreed and confirmed parent/guardian agreement on the team link page."
            log(db, "privacy", f"{a.name} agreed to AI analysis.")
            flash(request, "Thanks. Your training data will be included in the coaches’ AI briefing. You can turn this off any time.")
        return back(f"/a/{token}#privacy")

    @app.get("/a/{token}/my-data.json")
    def athlete_export(token: str, db: Session = Depends(get_db, scope="function")):
        a = athlete_by_token(db, token)
        acts = db.scalars(select(Activity).where(Activity.athlete_id == a.id).options(selectinload(Activity.laps), selectinload(Activity.planned))
                          .order_by(Activity.start_utc)).all()
        ab = A.abilities(db, A.team_today()).get(a.id, {})
        data = {
            "exported_at": utcnow().isoformat() + "Z", "team": s.team_name,
            "athlete": {"name": a.name, "email": a.email or None, "gender": a.gender, "grade": a.grade, "group": a.group, "hr_max": a.hr_max, "hr_rest": a.hr_rest,
                        "training_group": ab.get("group"), "vdot": ab.get("vdot"),
                        "ai_consent": a.ai_consent_at.isoformat() + "Z" if a.ai_consent_at else None},
            "garmin": None if a.garmin is None else {"status": a.garmin.status, "garmin_user_id": a.garmin.garmin_user_id,
                                                     "permissions": a.garmin.permissions, "connected_at": a.garmin.connected_at.isoformat() + "Z",
                                                     "consent_note": a.garmin.consent_note},
            "runs": [{"date": x.local_date.isoformat(), "start_utc": x.start_utc.isoformat() + "Z", "source": x.source, "sport": x.sport,
                      "distance_m": x.distance_m, "duration_s": x.duration_s, "avg_hr": x.avg_hr, "max_hr": x.max_hr, "device": x.device,
                      "watch_vo2_max": x.vo2_max,
                      "run_type": A.run_type_of(x), "team_workout": x.planned.title if x.planned else None,
                      "effort": x.rpe, "legs": x.feel or None, "note": x.athlete_note or None,
                      "laps": [{"lap": l.idx + 1, "distance_m": l.distance_m, "time_s": l.timer_s, "avg_hr": l.avg_hr, "max_hr": l.max_hr,
                                "rep": l.rep_num, "hr_drop_60s": l.hr_drop_60s} for l in x.laps]} for x in acts],
            "results": [{"date": r.date.isoformat(), "event": r.event, "time_s": r.time_s, "season": r.season, "course": r.course, "kind": r.kind}
                        for r in sorted(a.results, key=lambda r: r.date)],
            "goals": [{"season": g.season, "event": g.event, "goal_s": g.goal_s} for g in a.goals],
            "injuries": [{"onset": i.onset_date.isoformat(), "injury": i.injury, "body_area": i.body_area,
                          "return": i.return_date.isoformat() if i.return_date else None} for i in a.injuries],
            "resting_heart_rate": [{"date": r.date.isoformat(), "bpm": r.bpm, "source": r.source}
                                   for r in sorted(a.resting_hrs, key=lambda r: r.date)],
            "pain_reports": [{"date": p.date.isoformat(), "body_area": p.body_area, "severity": p.severity, "note": p.note or None,
                              "saw_athletic_trainer": p.saw_trainer} for p in a.pain_reports],
            "days_missed": [{"date": m.date.isoformat(), "reason": m.reason, "body_area": m.body_area or None, "side": m.side or None,
                             "pain": m.pain, "started": m.started or None, "seen_by": m.seen_by or None, "more_days": m.more_days or None,
                             "description": m.description or None}
                            for m in db.scalars(select(MissedDay).where(MissedDay.athlete_id == a.id).order_by(MissedDay.date)).all()],
            "newsletter": {"name_shown": a.show_name, "photo_shown": a.show_photo, "spotlight": a.show_spotlight, "vdot_shown": a.show_vdot,
                           "consent_recorded_by": a.consent_by or None, "captain": a.is_captain,
                           "captain_notes": [{"week_of": c.week_start.isoformat(), "text": c.text, "status": c.status}
                                             for c in db.scalars(select(CaptainPost).where(CaptainPost.athlete_id == a.id)).all()]},
            "not_included": "Athletic trainer notes on injuries are kept separately; ask the athletic trainer or coaches for a copy.",
        }
        fname = f"my-training-data-{A.team_today().isoformat()}.json"
        return Response(json.dumps(data, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/a/{token}/delete-garmin-data")
    async def athlete_delete_garmin(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        a = athlete_by_token(db, token)
        if str(f.get("confirm", "")).strip().upper() != "DELETE":
            flash(request, "Type DELETE to confirm.", "error")
            return back(f"/a/{token}#privacy")
        if a.garmin and a.garmin.status != "revoked":
            with G.make_client() as http:
                G.disconnect(db, a.garmin, http)
        if a.garmin:
            db.delete(a.garmin)
        n = 0
        for act in db.scalars(select(Activity).where(Activity.athlete_id == a.id, Activity.source.like("garmin%"))).all():
            db.delete(act)
            n += 1
        for r in db.scalars(select(RestingHeartRate).where(RestingHeartRate.athlete_id == a.id, RestingHeartRate.source == "garmin")).all():
            db.delete(r)
        log(db, "privacy", f"{a.name} deleted their Garmin data ({n} runs) and disconnected Garmin.")
        flash(request, f"Deleted {n} run{'' if n == 1 else 's'} that came from Garmin and disconnected your Garmin account.")
        return back(f"/a/{token}#privacy")

    @app.post("/a/{token}/delete-request")
    async def athlete_delete_request(request: Request, token: str, db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        a = athlete_by_token(db, token)
        if not db.scalar(select(DeletionRequest).where(DeletionRequest.athlete_id == a.id, DeletionRequest.status == "open")):
            db.add(DeletionRequest(athlete_id=a.id, athlete_name=a.name))
            log(db, "privacy", f"{a.name} asked for all of their data to be deleted.")
        flash(request, "Your request is in. Your coaches will delete all of your data within 30 days and let you know.")
        return back(f"/a/{token}#privacy")

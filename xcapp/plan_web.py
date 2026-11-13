"""The season plan page (/workouts/plan): each week's Daniels phase and share of peak miles, each group's peak weekly
miles, and the week's suggested quality sessions for each group, which open in the Workouts page's plan form. See
season_plan.py."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from . import points as PT
from . import season_plan as SP
from . import seasons as SE
from .ingest import log
from .models import SeasonWeek, TrainingGroup


def season_choices(today: dt.date) -> list[dict]:
    """Last school year's, this one's and next one's seasons, in order."""
    y = SE.school_year_of(today)
    return [p for yr in (y - 1, y, y + 1) for p in SE.year_periods(yr)]


def season_key(p: dict) -> str:
    return f"{p['school_year']}-{p['kind']}"


def pick_season(key: str, today: dt.date) -> dict:
    for p in season_choices(today):
        if season_key(p) == key:
            return p
    return SE.period_for(today)


def suggested_workout(db: Session, key: str, group: str, team: str, week: str, today: dt.date):
    """A suggested session as a workout to open in the plan form (not saved): (workout-like, parts) or (None, [])."""
    try:
        monday = A.week_monday(dt.date.fromisoformat(week))
    except ValueError:
        return None, []
    sw = SP.week_of(db, monday)
    row = next((r for r in SP.board(db, sw, PT.settings_for(db)[0]) if r["group"] == group and r["team"] in ("", team)), None)
    s = next((x for x in (row or {}).get("sessions", []) if x["key"] == key), None)
    if s is None:
        return None, []
    day = max(today, monday + dt.timedelta(days=1)) if monday + dt.timedelta(days=6) >= today else monday + dt.timedelta(days=1)
    return SimpleNamespace(id=None, title=f"{s['title']} · {group}", date=day, groups=group, gender=team if row["team"] else ""), s["parts"]


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, coach_or_admin = h.any_staff, h.coach_or_admin

    @app.get("/workouts/plan", response_class=HTMLResponse)
    def plan_page(request: Request, season: str = "", week: str = "", user=Depends(any_staff, scope="function"),
                  db: Session = Depends(get_db, scope="function")):
        today = A.team_today()
        p = pick_season(season, today)
        rows = SP.weeks(db, p)
        try:
            focus = A.week_monday(dt.date.fromisoformat(week)) if week else A.week_monday(today)
        except ValueError:
            focus = A.week_monday(today)
        if rows and not rows[0]["monday"] <= focus <= rows[-1]["monday"]:
            focus = rows[0]["monday"]
        sw = SP.week_of(db, focus)
        run_days = PT.settings_for(db)[0]
        groups = db.scalars(select(TrainingGroup).order_by(TrainingGroup.position)).all()
        pk = SP.peaks(db)
        # Does each group's weekly goal match the plan for this week?
        mismatch = []
        if sw is not None:
            for g in groups:
                want = {t: SP.planned_miles(pk[g.name][t], sw.miles_pct) for t in ("Boys", "Girls")}
                if any(want[t] and want[t] != g.miles_goal(t) for t in want):
                    mismatch.append(g.name)
        return render(request, "plan.html", user, season=p, seasons=season_choices(today), season_key=season_key, rows=rows,
                      focus=focus, sw=sw, board=SP.board(db, sw, run_days), phases=SP.PHASES, peaks=pk, groups=groups,
                      mismatch=mismatch, today=today, this_monday=A.week_monday(today), planned_miles=SP.planned_miles,
                      top=next(iter(pk.items()), None))

    @app.post("/workouts/plan")
    async def plan_save(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        p = pick_season(str(f.get("season", "")), A.team_today())
        action = f.get("action", "save")
        if action in ("fill", "replace"):
            n = SP.fill(db, p, replace=action == "replace")
            flash(request, f"Daniels’ plan filled in for {n} week{'s' if n != 1 else ''} of {p['label']}." if n
                  else "Every week already has a plan.")
        elif action == "peaks":
            values = {}
            for g in db.scalars(select(TrainingGroup)).all():
                vals = {}
                for t in ("Boys", "Girls"):
                    try:
                        vals[t] = max(0.0, min(150.0, float(f.get(f"peak_{g.name}_{t}", "") or 0)))
                    except ValueError:
                        vals[t] = 0.0
                values[g.name] = vals
            SP.save_peaks(db, values)
            flash(request, "Peak weekly miles saved.")
        else:
            changed = 0
            for w in SP.weeks(db, p):
                key = w["monday"].isoformat()
                phase, pct = f.get(f"phase_{key}", ""), f.get(f"pct_{key}", "")
                if not phase:
                    if w["row"] is not None:
                        db.delete(w["row"])
                        changed += 1
                    continue
                try:
                    ph, pc = int(phase), int(float(pct or 100))
                except ValueError:
                    continue
                if ph not in SP.PHASES or not 30 <= pc <= 130:
                    continue
                row = w["row"] or SeasonWeek(monday=w["monday"])
                if w["row"] is None:
                    db.add(row)
                row.phase, row.miles_pct, row.note = ph, pc, str(f.get(f"note_{key}", "")).strip()[:200]
                changed += 1
            flash(request, f"Saved the plan for {p['label']}.")
        db.flush()
        if action != "peaks":   # a week's phase sets its score mix: weeks saved for medals are worked out again
            from .medals import recalculate
            recalculate(db, SP.weeks_of(p)[0] if SP.weeks_of(p) else None)
        log(db, "app", f"{user.username} updated the season plan ({action}).")
        return back(f"/workouts/plan?season={season_key(p)}&week={f.get('week', '')}")

    @app.post("/workouts/plan/apply")
    async def plan_apply(request: Request, user=Depends(coach_or_admin, scope="function"), db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        try:
            sw = SP.week_of(db, dt.date.fromisoformat(str(f.get("week", ""))))
        except ValueError:
            sw = None
        if sw is None:
            flash(request, "That week has no plan yet.", "error")
        else:
            lines = SP.apply_miles(db, sw)
            log(db, "app", f"{user.username} set weekly miles goals from the plan for the week of {sw.monday:%b %-d}: {', '.join(lines)}.")
            flash(request, "Weekly miles goals set: " + ", ".join(lines) + "." if lines else "No group has a peak weekly miles set.")
        return back(f"/workouts/plan?season={f.get('season', '')}&week={f.get('week', '')}")

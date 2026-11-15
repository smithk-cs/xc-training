"""Course flyover pages and the Courses page's flyover uploads (see flyover.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import courses as CS
from . import flyover as FL
from .models import Course, CourseFlyover, utcnow


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, coach_or_admin, athlete_by_token = h.any_staff, h.coach_or_admin, h.athlete_by_token

    def course_or_404(db: Session, cid: int) -> Course:
        c = db.get(Course, cid)
        if c is None:
            raise HTTPException(404, "Course not found.")
        return c

    @app.post("/courses/{cid}/flyovers")
    async def flyover_save(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"),
                           db: Session = Depends(get_db, scope="function")):
        f = await form_checked(request)
        c = course_or_404(db, cid)
        to = f"/courses?edit={cid}#flyovers"
        dist = str(f.get("distance", ""))
        allowed = [d for d in (c.distances or "").split(",") if d] or list(CS.DISTANCES)
        fly = next((x for x in c.flyovers if x.distance == dist), None)
        preset = str(f.get("preset", ""))
        up = f.get("file")
        has_file = bool(up and getattr(up, "filename", ""))
        if dist not in allowed and fly is None:
            flash(request, "Pick the distance this flyover is for.", "error")
            return back(to)
        if preset or has_file:
            if preset:
                if preset not in FL.presets():
                    flash(request, "Pick one of the built-in courses.", "error")
                    return back(to)
                data, notes, sections = FL.preset_record(preset)
                name = FL.presets()[preset]["title"]
            else:
                raw = await up.read(FL.MAX_TRACK_BYTES + 1)
                try:
                    data = FL.build(FL.read_track(str(up.filename), raw))
                except FL.FlyoverError as exc:
                    flash(request, str(exc), "error")
                    return back(to)
                name = str(up.filename)[:200]
                notes = fly.notes if fly and fly.notes.strip() else FL.default_notes(data)
                sections = fly.sections if fly else ""
            if fly is None:
                fly = CourseFlyover(distance=dist)
                c.flyovers.append(fly)
            fly.data, fly.notes, fly.sections = json.dumps(data, separators=(",", ":")), notes, sections
            fly.filename, fly.preset = name, preset
            fly.uploaded_by, fly.uploaded_at = user.username, utcnow()
            msg = f"Saved the {dist} flyover for {c.name}."
            if not preset and data["heights"] == "map":
                msg += " The file had no heights, so they come from an elevation map."
            elif not preset and data["heights"] == "none":
                msg += " The file had no heights and none could be looked up, so the profile is flat."
            flash(request, msg)
            return back(to)
        if fly is None:
            flash(request, "Choose a GPX or FIT file, or a built-in course.", "error")
            return back(to)
        total = json.loads(fly.data or "{}").get("total") or 0
        fly.notes = "\n".join(f"{m:g} {t}" for m, t in FL.parse_notes(str(f.get("notes", "")), total))
        fly.sections = FL.sections_text(FL.parse_sections(str(f.get("sections", "")), total))
        flash(request, f"Saved the notes for the {fly.distance} flyover.")
        return back(to)

    @app.post("/courses/{cid}/flyovers/{fid}/delete")
    async def flyover_delete(request: Request, cid: int, fid: int, user=Depends(coach_or_admin, scope="function"),
                             db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        fly = db.get(CourseFlyover, fid)
        if fly is not None and fly.course_id == cid:
            flash(request, f"Removed the {fly.distance} flyover.")
            db.delete(fly)
        return back(f"/courses?edit={cid}#flyovers")

    def page(request, fly: CourseFlyover, course: Course, user, back_url: str, back_label: str):
        return render(request, "flyover.html", user, fly=fly, view=FL.view_data(fly, course), back_url=back_url,
                      back_label=back_label)

    @app.get("/courses/{cid}/flyovers/{fid}", response_class=HTMLResponse)
    def flyover_staff(request: Request, cid: int, fid: int, user=Depends(any_staff, scope="function"),
                      db: Session = Depends(get_db, scope="function")):
        fly = db.get(CourseFlyover, fid)
        if fly is None or fly.course_id != cid:
            raise HTTPException(404, "Flyover not found.")
        return page(request, fly, db.get(Course, cid), user, "/courses", "Courses")

    @app.get("/a/{token}/flyovers/{fid}", response_class=HTMLResponse)
    def flyover_athlete(request: Request, token: str, fid: int, db: Session = Depends(get_db, scope="function")):
        athlete_by_token(db, token)
        fly = db.get(CourseFlyover, fid)
        if fly is None:
            raise HTTPException(404, "Flyover not found.")
        return page(request, fly, db.get(Course, fly.course_id), None, f"/a/{token}#meets", "My page")

"""Course flyover pages and the Courses page's flyover uploads (see flyover.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from . import courses as CS
from . import flyover as FL
from .models import Course, CourseFlyover, FlyoverImage, utcnow


def register(app, h: SimpleNamespace) -> None:
    render, flash, back, get_db, form_checked = h.render, h.flash, h.back, h.get_db, h.form_checked
    any_staff, coach_or_admin, athlete_by_token = h.any_staff, h.coach_or_admin, h.athlete_by_token

    def get_satellite(fly: CourseFlyover, data: dict) -> bool:
        """Fetch and keep the satellite photo for a flyover made from a GPS track; False when none could be had."""
        got = FL.satellite_image(data.get("geo"))
        if got is None:
            fly.satellite = None
            return False
        img, credit = got
        if fly.satellite is None:            # one photo per flyover: a new one replaces the old in place
            fly.satellite = FlyoverImage()
        sat = fly.satellite
        sat.data, sat.credit, sat.fetched_at = img, credit, utcnow()
        sat.x = sat.y = -FL.IMG_PAD
        sat.size = FL.SIDE + 2 * FL.IMG_PAD
        return True

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
            if preset:
                fly.satellite = None
            elif not get_satellite(fly, data):
                msg += " The satellite photo couldn’t be fetched just now; try Get satellite photo later."
            if not preset and data["heights"] == "usgs":
                msg += " Heights come from USGS elevation data."
            elif not preset and data["heights"] == "track":
                msg += " USGS elevation data couldn’t be reached, so heights come from the file’s GPS; press Get USGS heights later."
            elif not preset and data["heights"] == "map":
                msg += " USGS couldn’t be reached and the file had no heights, so they come from a coarse elevation map; press Get USGS heights later."
            elif not preset and data["heights"] == "none":
                msg += " No heights could be found, so the profile is flat; press Get USGS heights later."
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

    @app.post("/courses/{cid}/flyovers/{fid}/satellite")
    async def flyover_satellite(request: Request, cid: int, fid: int, user=Depends(coach_or_admin, scope="function"),
                                db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        fly = db.get(CourseFlyover, fid)
        if fly is None or fly.course_id != cid:
            raise HTTPException(404, "Flyover not found.")
        data = json.loads(fly.data or "{}")
        if not data.get("geo"):
            flash(request, "This flyover has no map position. Upload its GPX or FIT file again to get a satellite photo.", "error")
        elif get_satellite(fly, data):
            flash(request, f"Saved the satellite photo for the {fly.distance} flyover.")
        else:
            flash(request, "The satellite photo couldn’t be fetched just now. Try again later.", "error")
        return back(f"/courses?edit={cid}#flyovers")

    @app.post("/courses/{cid}/flyovers/{fid}/heights")
    async def flyover_heights(request: Request, cid: int, fid: int, user=Depends(coach_or_admin, scope="function"),
                              db: Session = Depends(get_db, scope="function")):
        """USGS heights for a flyover already made from a GPS track (its route, notes and sections stay)."""
        await form_checked(request)
        fly = db.get(CourseFlyover, fid)
        if fly is None or fly.course_id != cid:
            raise HTTPException(404, "Flyover not found.")
        data = json.loads(fly.data or "{}")
        if not data.get("geo"):
            flash(request, "This flyover has no map position (a built-in course). Make it from a GPX or FIT file to get USGS heights.", "error")
        elif FL.reheight(data):
            fly.data = json.dumps(data, separators=(",", ":"))
            flash(request, f"The {fly.distance} flyover now has USGS heights: about {data['climb_ft']:,} ft of climbing, "
                           f"{data['low'][1]:,} to {data['high'][1]:,} ft.")
        else:
            flash(request, "USGS elevation data couldn’t be reached just now. Try again later.", "error")
        return back(f"/courses?edit={cid}#flyovers")

    def satellite_jpeg(fly: CourseFlyover | None) -> Response:
        if fly is None or fly.satellite is None:
            raise HTTPException(404, "No satellite photo.")
        return Response(fly.satellite.data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.get("/courses/{cid}/flyovers/{fid}/satellite.jpg")
    def flyover_satellite_staff(cid: int, fid: int, user=Depends(any_staff, scope="function"),
                                db: Session = Depends(get_db, scope="function")):
        fly = db.get(CourseFlyover, fid)
        return satellite_jpeg(fly if fly is not None and fly.course_id == cid else None)

    @app.get("/a/{token}/flyovers/{fid}/satellite.jpg")
    def flyover_satellite_athlete(token: str, fid: int, db: Session = Depends(get_db, scope="function")):
        athlete_by_token(db, token)
        return satellite_jpeg(db.get(CourseFlyover, fid))

    @app.post("/courses/{cid}/flyovers/{fid}/delete")
    async def flyover_delete(request: Request, cid: int, fid: int, user=Depends(coach_or_admin, scope="function"),
                             db: Session = Depends(get_db, scope="function")):
        await form_checked(request)
        fly = db.get(CourseFlyover, fid)
        if fly is not None and fly.course_id == cid:
            flash(request, f"Removed the {fly.distance} flyover.")
            db.delete(fly)
        return back(f"/courses?edit={cid}#flyovers")

    def page(request, fly: CourseFlyover, course: Course, user, back_url: str, back_label: str, image_url: str):
        stamp = int(fly.satellite.fetched_at.timestamp()) if fly.satellite and fly.satellite.fetched_at else 0
        return render(request, "flyover.html", user, fly=fly, view=FL.view_data(fly, course, f"{image_url}?v={stamp}"),
                      back_url=back_url, back_label=back_label)

    @app.get("/courses/{cid}/flyovers/{fid}", response_class=HTMLResponse)
    def flyover_staff(request: Request, cid: int, fid: int, user=Depends(any_staff, scope="function"),
                      db: Session = Depends(get_db, scope="function")):
        fly = db.get(CourseFlyover, fid)
        if fly is None or fly.course_id != cid:
            raise HTTPException(404, "Flyover not found.")
        return page(request, fly, db.get(Course, cid), user, "/courses", "Courses", f"/courses/{cid}/flyovers/{fid}/satellite.jpg")

    @app.get("/a/{token}/flyovers/{fid}", response_class=HTMLResponse)
    def flyover_athlete(request: Request, token: str, fid: int, db: Session = Depends(get_db, scope="function")):
        athlete_by_token(db, token)
        fly = db.get(CourseFlyover, fid)
        if fly is None:
            raise HTTPException(404, "Flyover not found.")
        return page(request, fly, db.get(Course, fly.course_id), None, f"/a/{token}#meets", "My page",
                    f"/a/{token}/flyovers/{fid}/satellite.jpg")

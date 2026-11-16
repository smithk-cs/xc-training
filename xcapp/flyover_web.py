"""Course flyover pages and the Courses page's flyover uploads (see flyover.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import courses as CS
from . import flyover as FL
from .models import Course, CourseArea, CourseFlyover, FlyoverImage, utcnow
from .security import csrf_ok


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

    def heights_note(data: dict) -> str:
        return {"usgs": " Heights come from USGS elevation data.",
                "track": " USGS elevation data couldn’t be reached, so heights come from the file’s GPS; press Get USGS heights later.",
                "map": " USGS couldn’t be reached and the file had no heights, so they come from a coarse elevation map; press Get USGS heights later.",
                "none": " No heights could be found, so the profile is flat; press Get USGS heights later."}.get(data.get("heights"), "")

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
            if not preset:
                msg += heights_note(data)
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

    # ---------- the course's area photo, and tracing the route on it ----------

    @app.post("/courses/{cid}/area")
    async def course_area(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"),
                          db: Session = Depends(get_db, scope="function")):
        """Fetch (or fetch again) the USGS photo of the area around the course, or around a point the coach gives."""
        f = await form_checked(request)
        c = course_or_404(db, cid)
        to = f"/courses?edit={cid}#trace"
        try:
            size = int(f.get("size", 1500))
            lat = float(f.get("lat") or c.latitude)
            lon = float(f.get("lon") or c.longitude)
        except (TypeError, ValueError):
            flash(request, "Add the course’s latitude and longitude first (above), or type a centre point.", "error")
            return back(to)
        got = FL.area_image(lat, lon, size)
        if got is None:
            flash(request, "The satellite photo couldn’t be fetched (USGS has no imagery there, or can’t be reached). Try again later.", "error")
            return back(to)
        img, credit, (n, w, s_, e) = got
        if c.area is None:
            c.area = CourseArea()
        a = c.area
        a.data, a.credit, a.north, a.west, a.south, a.east, a.size_m, a.fetched_at = img, credit, n, w, s_, e, size, utcnow()
        flash(request, f"Saved the satellite photo of the area around {c.name} ({FL.AREA_SIZES[size]} across). Tap along the route to trace it.")
        return back(to)

    @app.get("/courses/{cid}/area.jpg")
    def course_area_jpeg(cid: int, user=Depends(any_staff, scope="function"), db: Session = Depends(get_db, scope="function")):
        a = db.scalar(select(CourseArea).where(CourseArea.course_id == cid))
        if a is None:
            raise HTTPException(404, "No area photo.")
        return Response(a.data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/courses/{cid}/trace")
    async def course_trace(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"),
                           db: Session = Depends(get_db, scope="function")):
        """A route traced on the area photo becomes the course's flyover for a distance (as a GPS file would)."""
        from fastapi.responses import JSONResponse
        raw = await request.body()
        if len(raw) > 300_000:
            return JSONResponse({"error": "That route is too long."}, status_code=413)
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "Bad request."}, status_code=400)
        if not csrf_ok(request.session, body.get("csrf")):
            return JSONResponse({"error": "Your session expired. Reload the page and try again."}, status_code=400)
        c = course_or_404(db, cid)
        a = c.area
        dist = str(body.get("distance", ""))
        allowed = [d for d in (c.distances or "").split(",") if d] or list(CS.DISTANCES)
        fly = next((x for x in c.flyovers if x.distance == dist), None)
        if dist not in allowed and fly is None:
            return JSONResponse({"error": "Pick the distance this route is for."}, status_code=400)
        pts = []
        try:
            for p in body.get("points") or []:
                lat, lon = float(p[0]), float(p[1])
                if a is None or not (a.south - 0.01 <= lat <= a.north + 0.01 and a.west - 0.01 <= lon <= a.east + 0.01):
                    raise ValueError
                pts.append((lat, lon))
        except (TypeError, ValueError, IndexError):
            return JSONResponse({"error": "Those points aren’t on the photo. Reload the page and trace again."}, status_code=400)
        if not 2 <= len(pts) <= 3000:
            return JSONResponse({"error": "Tap at least two points along the route."}, status_code=400)
        return store_route(request, c, dist, fly, pts, user, "Traced on the satellite photo")

    def store_route(request, c, dist, fly, pts, user, filename: str, notes="", sections="", marks=None):
        """Make (or remake) a distance's flyover from a route given as latitudes and longitudes, as a GPS file would:
        USGS heights, the satellite photo under it. The flyover's notes and sections stay; a new one starts from
        `notes` and `sections` (a built-in course's), or notes made from the route. `marks`: checkpoints and labels
        {"CP": [[label, lat, lon]], "LAB": [...]} to place in the drawing."""
        from fastapi.responses import JSONResponse
        try:
            data = FL.build(FL.densify(pts))
        except FL.FlyoverError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        for k in ("CP", "LAB"):
            data[k] = [[str(lbl)[:40], *(round(v, 1) for v in FL.to_draw(data["geo"], lat, lon))]
                       for lbl, lat, lon in (marks or {}).get(k, [])]
        if fly is None:
            fly = CourseFlyover(distance=dist)
            c.flyovers.append(fly)
        keep_notes = fly.notes if fly.notes and fly.notes.strip() else (notes or FL.default_notes(data))
        keep_sections = fly.sections if fly.sections and fly.sections.strip() else sections
        fly.data, fly.notes, fly.sections = json.dumps(data, separators=(",", ":")), keep_notes, keep_sections
        fly.filename, fly.preset = filename[:200], ""
        fly.uploaded_by, fly.uploaded_at = user.username, utcnow()
        msg = f"Saved the {dist} flyover for {c.name} ({data['measured_mi']:.2f} miles), {filename[0].lower() + filename[1:]}."
        if not get_satellite(fly, data):
            msg += " The satellite photo couldn’t be fetched just now; try Get satellite photo later."
        msg += heights_note(data)
        flash(request, msg)
        return JSONResponse({"ok": True, "next": f"/courses?edit={c.id}#flyovers"})

    @app.post("/courses/{cid}/place")
    async def course_place(request: Request, cid: int, user=Depends(coach_or_admin, scope="function"),
                           db: Session = Depends(get_db, scope="function")):
        """A built-in course placed on the area photo (its start tapped, turned and sized to the paths) becomes the
        distance's flyover, with its checkpoints, labels, notes and sections."""
        from fastapi.responses import JSONResponse
        raw = await request.body()
        try:
            body = json.loads(raw or b"{}") if len(raw) <= 300_000 else None
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "Bad request."}, status_code=400)
        if not csrf_ok(request.session, body.get("csrf")):
            return JSONResponse({"error": "Your session expired. Reload the page and try again."}, status_code=400)
        c = course_or_404(db, cid)
        a = c.area
        pid = str(body.get("preset", ""))
        if pid not in FL.presets():
            return JSONResponse({"error": "Pick one of the built-in courses."}, status_code=400)
        dist = str(body.get("distance", ""))
        allowed = [d for d in (c.distances or "").split(",") if d] or list(CS.DISTANCES)
        fly = next((x for x in c.flyovers if x.distance == dist), None)
        if dist not in allowed and fly is None:
            return JSONResponse({"error": "Pick the distance this course is for."}, status_code=400)
        inside = lambda lat, lon: a is not None and a.south - 0.02 <= lat <= a.north + 0.02 and a.west - 0.02 <= lon <= a.east + 0.02
        try:
            pts = [(float(p[0]), float(p[1])) for p in body.get("points") or []]
            marks = {k: [(str(m[0]), float(m[1]), float(m[2])) for m in body.get(k.lower()) or []] for k in ("CP", "LAB")}
            if not pts or not all(inside(*p) for p in pts) or not all(inside(m[1], m[2]) for v in marks.values() for m in v):
                raise ValueError
        except (TypeError, ValueError, IndexError):
            return JSONResponse({"error": "The course isn’t on the photo. Tap its start on the photo and try again."}, status_code=400)
        if not 2 <= len(pts) <= 3000:
            return JSONResponse({"error": "That isn’t a whole course."}, status_code=400)
        _, notes, sections = FL.preset_record(pid)
        title = FL.presets()[pid]["title"]
        return store_route(request, c, dist, fly, pts, user, f"{title}, placed on the satellite photo", notes, sections, marks)

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

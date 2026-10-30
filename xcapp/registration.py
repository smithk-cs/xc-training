"""Meet entries: which athletes run which race, each with their season best at that distance, as an email text and a PDF
for the race director."""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analysis as A
from .imports import parse_distance
from .models import MeetEntry, RaceResult, Result, StaffUser

FONTS = Path(__file__).parent / "pdf_fonts"
GENDERS = ("Boys", "Girls")
LEVELS = ("Varsity", "JV", "Frosh/Soph", "Freshman", "Sophomore")
ATTENDANCE_CUTOFF_DAYS = 4  # athletes can say whether they'll be at a meet until this many days before it
DISTANCES = [("1 Mile", 1609.344), ("1.5 Miles", 2414.016), ("2 Miles", 3218.688), ("2.5 Miles", 4023.36), ("3 Miles", 4828.032),
             ("4K", 4000.0), ("5K", 5000.0), ("6K", 6000.0)]
COVERAGE = 0.75  # a close distance is shown when fewer than this share of the athletes listed have run the race's distance


def _nearest(m: float) -> tuple[str, float] | None:
    """The listed distance closest to m, if within 1% (5,000 Meters and 5K are one distance; 4K is not 2.5 miles)."""
    best = min(DISTANCES, key=lambda x: abs(x[1] - m))
    return best if abs(best[1] - m) <= best[1] * 0.01 else None


def _key(m: float) -> float:
    n = _nearest(m)
    return n[1] if n else round(m / 10) * 10


def distance_label(m: float) -> str:
    n = _nearest(m)
    return n[0] if n else f"{m:,.0f} m"


def season_times(db: Session, on: dt.date) -> dict[int, dict[float, float]]:
    """athlete id -> {distance: best time} over the meet's season up to the meet, from meet and benchmark results."""
    start, end = A.season_range(on)
    out: dict[int, dict[float, float]] = {}

    def add(aid, m, t):
        k = _key(m)
        if t and (k not in out.setdefault(aid, {}) or t < out[aid][k]):
            out[aid][k] = t
    for r in db.scalars(select(RaceResult).where(RaceResult.date >= start, RaceResult.date <= min(end, on))).all():
        add(r.athlete_id, r.distance_m, r.time_s)
    for r in db.scalars(select(Result).where(Result.date >= start, Result.date <= min(end, on))).all():
        if r.event in A.EVENTS:
            add(r.athlete_id, A.EVENTS[r.event], r.time_s)
    return out


def close_distance(times: dict[int, dict[float, float]], athlete_ids: list[int], m: float | None) -> float | None:
    """When fewer than COVERAGE of these athletes have run distance m this season: the distance nearest to m that more than
    COVERAGE of them have run. None when the race's own distance is enough, or nothing qualifies."""
    if m is None or not athlete_ids:
        return None
    k = _key(m)
    share = lambda d: sum(1 for a in athlete_ids if d in times.get(a, {})) / len(athlete_ids)
    if share(k) >= COVERAGE:
        return None
    raced = {d for a in athlete_ids for d in times.get(a, {})} - {k}
    good = [d for d in raced if share(d) > COVERAGE]
    import math
    return min(good, key=lambda d: abs(math.log(d / k))) if good else None


def contacts(db: Session) -> list[StaffUser]:
    """Staff listed on entry sheets: those marked for it who have a name, phone or email."""
    order = {"admin": 0, "coach": 1, "trainer": 2}
    staff = [u for u in db.scalars(select(StaffUser)).all() if u.on_entries and (u.full_name or u.phone or u.email)]
    return sorted(staff, key=lambda u: (order.get(u.role, 3), u.id))


def contact_line(u: StaffUser) -> str:
    return " · ".join(x for x in (u.display_name + (f", {u.title}" if u.title else ""), u.phone, u.email) if x)
RED, GOLD, INK, MUTED, LINE, SOFT = (166, 25, 46), (201, 154, 46), (27, 27, 31), (94, 88, 80), (221, 214, 205), (247, 244, 240)


def meters(distance: str) -> float | None:
    try:
        return parse_distance(distance)
    except ValueError:
        return None


def season_best(db: Session, athlete_id: int, distance: str, on: dt.date) -> float | None:
    """Fastest time this season (the meet's season) at the race's distance, from meet results and benchmark results."""
    m = meters(distance)
    if m is None:
        return None
    start, end = A.season_range(on)
    near = lambda d: abs(d - m) <= m * 0.01
    times = [r.time_s for r in db.scalars(select(RaceResult).where(
        RaceResult.athlete_id == athlete_id, RaceResult.date >= start, RaceResult.date <= min(end, on))).all() if near(r.distance_m)]
    times += [r.time_s for r in db.scalars(select(Result).where(
        Result.athlete_id == athlete_id, Result.date >= start, Result.date <= min(end, on))).all()
        if r.event in A.EVENTS and near(A.EVENTS[r.event])]
    return min(times) if times else None


def report(db: Session, meet: MeetEntry) -> list[dict]:
    """[{division, rows: [{athlete, grade, seed}]}] in the order shown on the page."""
    out = []
    for d in meet.divisions:
        rows = [{"athlete": e.athlete, "grade": e.athlete.grade, "seed": season_best(db, e.athlete_id, d.distance, meet.date)}
                for e in d.entries]
        out.append({"division": d, "rows": rows})
    return out


def title(meet: MeetEntry) -> str:
    return f"{meet.name} entries, {meet.date:%a %b %-d, %Y}"


def as_text(meet: MeetEntry, divisions: list[dict], team: str, coach: str, coach_email: str, staff: list | None = None) -> str:
    lines = [f"{team}: entries for {meet.name}", f"{meet.date:%A, %B %-d, %Y}" + (f" · {meet.location}" if meet.location else ""), ""]
    for d in divisions:
        div = d["division"]
        lines.append(f"{div.name}{' · ' + div.distance if div.distance else ''} ({len(d['rows'])} athletes)")
        for i, r in enumerate(d["rows"], 1):
            lines.append(f"  {i}. {r['athlete'].name}, grade {r['grade']}" + (f", season best {A.fmt_time(r['seed'])}" if r["seed"] else ""))
        lines.append("")
    if meet.notes:
        lines += [meet.notes, ""]
    lines.append("Thank you,")
    lines.append(coach + (f" ({coach_email})" if coach_email else ""))
    lines.append(team)
    if staff:
        lines += ["", "Coaching staff:"] + [f"  {contact_line(u)}" for u in staff]
    return "\n".join(lines)


class _Doc(FPDF):
    def __init__(self, team: str, footer_note: str):
        super().__init__(format="Letter", unit="pt")
        self.team, self.footer_note = team, footer_note
        for name, file in (("Inter", "Inter-Regular.ttf"), ("InterSemi", "Inter-SemiBold.ttf"), ("Barlow", "BarlowSemiCondensed-Bold.ttf")):
            self.add_font(name, "", str(FONTS / file))
        self.set_auto_page_break(True, margin=60)
        self.set_margins(48, 48, 48)

    def footer(self):
        self.set_y(-40)
        self.set_draw_color(*LINE)
        self.line(48, self.get_y() - 6, self.w - 48, self.get_y() - 6)
        self.set_font("Inter", size=8)
        self.set_text_color(*MUTED)
        self.cell(0, 12, self.footer_note, align="L")
        self.set_x(48)
        self.cell(0, 12, f"Page {self.page_no()} of {{nb}}", align="R")


def branded_page(team: str, subtitle: str, logo: tuple[bytes, str] | None, center: bool = False) -> tuple[_Doc, float]:
    """A new letter-size PDF with the team's red and gold header band. Returns the document and the usable width.
    center: the team name and subtitle centered across the page instead of beside the logo."""
    pdf = _Doc(team, f"{team} · prepared {A.team_today():%b %-d, %Y}")   # the team's date (Pacific), not the server's
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.w - 96
    pdf.set_fill_color(*RED)
    pdf.rect(0, 0, pdf.w, 92, "F")
    pdf.set_fill_color(*GOLD)
    pdf.rect(0, 92, pdf.w, 5, "F")
    x = 48
    if logo and logo[1] in ("image/png", "image/jpeg"):
        try:
            pdf.image(io.BytesIO(logo[0]), x=48, y=18, h=56)
            x = 120
        except Exception:  # an unreadable logo just leaves the header without it
            x = 48
    if center:
        x = 48
    align = "C" if center else "L"
    pdf.set_xy(x, 22)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Barlow", size=26)
    pdf.cell(w - (x - 48), 30, team.upper(), align=align)
    pdf.set_xy(x, 52)
    pdf.set_font("InterSemi", size=12)
    pdf.cell(w - (x - 48), 18, subtitle, align=align)
    return pdf, w


def staff_table(pdf: _Doc, w: float, staff: list, center: bool = False) -> None:
    """The coaching staff's contacts. center: everything but the names centered."""
    if not staff:
        return
    if pdf.get_y() > pdf.h - 60 - 22 * (len(staff) + 2):
        pdf.add_page()
    pdf.ln(12)
    pdf.set_font("Barlow", size=13)
    pdf.set_text_color(*RED)
    pdf.cell(w, 18, "Coaching staff", align="C" if center else "L", new_x="LMARGIN", new_y="NEXT")
    cols = [(0.30, "Name"), (0.24, "Title"), (0.18, "Cell"), (0.28, "Email")]
    pdf.set_font("InterSemi", size=9)
    pdf.set_fill_color(*INK)
    pdf.set_text_color(255, 255, 255)
    aligns = ["L"] + ["C" if center else "L"] * 3   # names stay left
    for (frac, label), align in zip(cols, aligns):
        pdf.cell(w * frac, 18, label.upper(), fill=True, align=align)
    pdf.ln(18)
    pdf.set_font("Inter", size=10)
    pdf.set_text_color(*INK)
    pdf.set_draw_color(*LINE)
    for u in staff:
        for (frac, _), v, align in zip(cols, (u.display_name, u.title, u.phone, u.email), aligns):
            pdf.cell(w * frac, 20, v or "", border="B", align=align)
        pdf.ln(20)


def as_pdf(meet: MeetEntry, divisions: list[dict], team: str, coach: str, coach_email: str,
           logo: tuple[bytes, str] | None = None, staff: list | None = None) -> bytes:
    pdf, w = branded_page(team, "Meet entries", logo)
    # Meet details
    pdf.set_xy(48, 118)
    pdf.set_text_color(*INK)
    pdf.set_font("Barlow", size=22)
    pdf.multi_cell(w, 26, meet.name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Inter", size=11)
    pdf.set_text_color(*MUTED)
    pdf.cell(w, 16, f"{meet.date:%A, %B %-d, %Y}" + (f"  ·  {meet.location}" if meet.location else ""), new_x="LMARGIN", new_y="NEXT")
    to = ", ".join(x for x in (meet.director_name, meet.director_email) if x)
    if to:
        pdf.cell(w, 16, f"For: {to}", new_x="LMARGIN", new_y="NEXT")
    total = sum(len(d["rows"]) for d in divisions)
    pdf.cell(w, 16, f"{total} athlete{'' if total == 1 else 's'} in {len(divisions)} race{'' if len(divisions) == 1 else 's'}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    cols = [(0.08, "#", "C"), (0.52, "Athlete", "L"), (0.14, "Grade", "C"), (0.26, "Season best", "C")]
    for d in divisions:
        div = d["division"]
        if pdf.get_y() > pdf.h - 160:
            pdf.add_page()
        pdf.set_font("Barlow", size=15)
        pdf.set_text_color(*RED)
        pdf.cell(w * 0.7, 22, div.name + (f"  ·  {div.distance}" if div.distance else ""))
        pdf.set_font("Inter", size=10)
        pdf.set_text_color(*MUTED)
        pdf.cell(w * 0.3, 22, f"{len(d['rows'])} athlete{'' if len(d['rows']) == 1 else 's'}", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("InterSemi", size=9)
        pdf.set_fill_color(*INK)
        pdf.set_text_color(255, 255, 255)
        for frac, label, align in cols:
            pdf.cell(w * frac, 18, label.upper(), align=align, fill=True)
        pdf.ln(18)
        pdf.set_font("Inter", size=10.5)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*LINE)
        for i, r in enumerate(d["rows"], 1):
            pdf.set_fill_color(*(SOFT if i % 2 == 0 else (255, 255, 255)))
            vals = [str(i), r["athlete"].name, str(r["grade"]), A.fmt_time(r["seed"]) if r["seed"] else "—"]
            for (frac, _, align), v in zip(cols, vals):
                pdf.cell(w * frac, 20, v, align=align, fill=True, border="B")
            pdf.ln(20)
        if not d["rows"]:
            pdf.set_text_color(*MUTED)
            pdf.cell(w, 20, "No athletes entered yet.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(14)
    if meet.notes:
        pdf.set_font("InterSemi", size=10)
        pdf.set_text_color(*INK)
        pdf.cell(w, 16, "Notes", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Inter", size=10)
        pdf.multi_cell(w, 14, meet.notes, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(8)
    pdf.set_font("Inter", size=10)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(w, 14, "Submitted by " + coach + (f"  ·  {coach_email}" if coach_email else "") + f"\n{team}",
                   new_x="LMARGIN", new_y="NEXT")
    staff_table(pdf, w, staff or [])
    return bytes(pdf.output())

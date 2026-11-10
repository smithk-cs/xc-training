"""Running-form analysis: a coach films an athlete from the side, the phone finds the body's joints in each frame
(MediaPipe Pose, in the browser: static/form-capture.js and form-core.js) and sends back only the measurements, the
joint-angle curves and a stick figure at a foot strike. The video never leaves the phone. Here each measure is rated
against a coaching range and turned into feedback, a score and a PDF report.

The ranges are general guides for distance runners at training-to-race paces, not hard rules: the right numbers
depend on speed, build and injury history, so the report says what to look at and why, and the coach decides.
"""
from __future__ import annotations

import io
import json
import math

from . import analysis as A

# key, title, unit, good range (low, high), what it measures, tolerance for the score
CHECKS = [
    ("overstride_deg", "Foot landing ahead of the hips", "°", (-5, 8),
     "The angle from the hips to the landing foot when it touches down. Landing well ahead of the hips brakes every step.", 8),
    ("shin_deg", "Shin at landing", "°", (-5, 10),
     "How far the shin tips forward of vertical as the foot lands (ankle ahead of the knee).", 8),
    ("front_knee_deg", "Front knee at landing", "°", (145, 170),
     "The knee angle of the landing leg (180° is straight). A slightly bent knee absorbs the landing.", 10),
    ("back_knee_deg", "Back knee at landing", "°", (40, 100),
     "The knee angle of the trailing leg as the other foot lands: how well the heel folds up behind.", 15),
    ("trunk_deg", "Forward lean", "°", (3, 12),
     "How far the body leans forward from the hips to the shoulders, averaged over the clip.", 5),
    ("head_deg", "Head position", "°", (-20, -3),
     "The line from the ear to the nose against level. Near level, slightly down, means the eyes look ahead, not up or at the feet.", 8),
    ("elbow_deg", "Elbow bend", "°", (70, 110),
     "The angle at the elbow through the arm swing (90° is a right angle).", 15),
    ("cadence", "Steps per minute", "", (165, 195),
     "Foot strikes per minute, both feet. Shorter, quicker steps usually mean less overstriding.", 15),
    ("oscillation_pct", "Bounce", "% of leg length", (0, 11),
     "How much the hips rise and fall, as a share of leg length. Energy spent going up is not moving the runner forward.", 4),
]
# Shown, not scored: they depend on speed more than technique
TIMING = [("contact_ms", "Ground contact", "ms", "Time each foot spends on the ground."),
          ("flight_ms", "Flight", "ms", "Time between steps with both feet off the ground."),
          ("duty_pct", "Duty factor", "%", "Share of each stride a foot is on the ground. Faster running lowers it.")]

# What to say: (key, "low" | "good" | "high") -> (summary, why it matters, cue, drill)
FEEDBACK = {
    ("overstride_deg", "good"): ("Lands under the body.", "Landing close under the hips keeps braking small and lets the body roll over the foot.",
                                 "Keep the feeling of the foot landing under you.", "Strides on grass to keep it natural at faster paces."),
    ("overstride_deg", "low"): ("Lands behind the hips.", "Landing well behind the hips is unusual and can mean the runner is leaning from the waist or the clip caught a stumble.",
                                "Run tall from the ankles, not bent at the waist.", "Film again at a steady pace to confirm."),
    ("overstride_deg", "high"): ("Reaches out in front (overstriding).", "Landing ahead of the hips acts like a brake on every step and sends more load into the knee and shin.",
                                 "Quick, light steps; land with the foot under the knee, then push back.", "A-skips and quick-feet drills; 4 × 20 s at a slightly higher step rate (5% more)."),
    ("shin_deg", "good"): ("Shin near vertical at landing.", "A vertical shin at contact means the foot is under the knee and the landing force goes up through the leg.",
                           "Keep it.", "Strides with relaxed, quick feet."),
    ("shin_deg", "low"): ("Shin tips back at landing.", "The foot lands behind the knee, often with a forward lean from the waist.", "Run tall; let the foot land under the knee.", "Wall drills: lean from the ankles, knee drive."),
    ("shin_deg", "high"): ("Shin tips forward at landing (foot ahead of the knee).", "A shin reaching out at contact usually goes with overstriding and heel-first braking.",
                           "Pull the foot down and back as it lands, like pawing the ground.", "B-skips and straight-leg bounds; quick-feet drills."),
    ("front_knee_deg", "good"): ("Landing knee slightly bent.", "A soft knee at landing lets the leg act as a spring.", "Keep the soft landing.", "Hill strides keep the knee lift and soft landing."),
    ("front_knee_deg", "low"): ("Landing knee very bent (sitting).", "Landing on a deeply bent knee works the quads hard and lowers the hips, which costs energy.",
                                "Run tall: hips high, as if a string pulls the head up.", "Hip and glute strength (bridges, single-leg squats); marching drills."),
    ("front_knee_deg", "high"): ("Landing on a nearly straight leg.", "A straight knee at landing passes the impact into the knee and hip and usually means reaching out.",
                                 "Land with a soft knee under the body.", "Quick-feet drills; strides focusing on landing under the hips."),
    ("back_knee_deg", "good"): ("The trailing leg folds well.", "A heel that folds up behind shortens the leg and lets it swing through quickly.", "Keep it.", "Butt kicks and high knees in warm-ups."),
    ("back_knee_deg", "low"): ("Very high heel kick.", "A tight fold is normal at speed; at easy paces it can waste a little energy.", "Relax the heel recovery at easy paces.", "None needed unless it shows at easy paces."),
    ("back_knee_deg", "high"): ("Trailing leg stays open (heel low behind).", "A leg that doesn't fold swings through slowly, which limits stride and pushes the runner to reach out in front.",
                                "Heel up under the hip as the leg comes through.", "Butt kicks, high knees and A-skips; hip-flexor and hamstring strength."),
    ("trunk_deg", "good"): ("Good forward lean.", "A slight lean from the ankles keeps the body ahead of the feet.", "Keep running tall with a slight lean.", "Hill running reinforces it."),
    ("trunk_deg", "low"): ("Upright or leaning back.", "Leaning back puts the feet ahead of the body (overstriding) and loads the lower back.",
                           "Lean slightly forward from the ankles, not the waist.", "Falling starts: lean until you have to step, then run 20 m."),
    ("trunk_deg", "high"): ("Bent forward at the waist.", "Leaning from the waist (not the ankles) shortens the stride and tires the back and hips.",
                            "Run tall: chest up, hips forward.", "Core strength (planks, dead bugs); tall-posture strides."),
    ("head_deg", "good"): ("Head level, eyes ahead.", "A neutral head keeps the neck and shoulders relaxed.", "Keep looking about 20 m ahead.", "Remind on long runs when tired."),
    ("head_deg", "low"): ("Looking down.", "Looking at the feet rounds the shoulders and back and narrows breathing.", "Eyes up, about 20 m ahead.", "Pick a point down the road and run to it."),
    ("head_deg", "high"): ("Chin up, looking high.", "Lifting the chin tightens the neck and tilts the body back.", "Chin slightly tucked, eyes ahead, not up.", "Relaxed-shoulder strides; check on hills and late in races."),
    ("elbow_deg", "good"): ("Good arm angle.", "Elbows near 90° keep the arm swing quick and in rhythm with the legs.", "Keep the swing forward and back, relaxed hands.", "Arm-swing drill in place: 3 × 20 s."),
    ("elbow_deg", "low"): ("Arms very tight.", "A tight, short arm swing often goes with tense shoulders.", "Relax the shoulders; let the elbows open a little.", "Shoulder rolls, then relaxed strides."),
    ("elbow_deg", "high"): ("Arms too straight.", "Straight arms swing slowly, which slows the legs' rhythm and wastes energy.", "Bend the elbows toward 90°; swing from the shoulder.", "Arm-swing drill in place: 3 × 20 s at 90°."),
    ("cadence", "good"): ("Good step rate.", "A quick rhythm keeps steps under the body.", "Keep it.", "Strides keep the rhythm."),
    ("cadence", "low"): ("Slow, long steps.", "A low step rate usually means reaching out (overstriding) and more load per step.",
                         "Slightly quicker, lighter steps (about 5% more); don't force it.", "Run to a metronome 5% above this step rate for 1-2 min at a time."),
    ("cadence", "high"): ("Very quick, short steps.", "A very high rate at this pace can mean a short, choppy stride.", "Let the stride open from the hips; push back.", "Hill strides and bounding."),
    ("oscillation_pct", "good"): ("Little up-and-down bounce.", "Energy goes into moving forward, not up.", "Keep it.", "None needed."),
    ("oscillation_pct", "low"): ("Very little bounce.", "Fine for distance running.", "Keep it.", "None needed."),
    ("oscillation_pct", "high"): ("Lots of up-and-down bounce.", "Rising high each step spends energy going up and lands harder.",
                                  "Run forward, not up: push back along the ground.", "Quick-feet drills; strides thinking 'low and smooth'."),
}

METRIC_KEYS = {k for k, *_ in CHECKS} | {k for k, *_ in TIMING}
CURVE_KEYS = ("knee", "hip", "ankle", "elbow")
SHORT = {"overstride_deg": "Foot landing", "shin_deg": "Shin at landing", "front_knee_deg": "Front knee at landing",
         "back_knee_deg": "Back knee at landing", "trunk_deg": "Forward lean", "head_deg": "Head position", "elbow_deg": "Arms",
         "cadence": "Step rate", "oscillation_pct": "Bounce"}


def view(data: dict, pace_s: float | None) -> dict:
    """Everything the results screen (static/form-view.js) shows, as plain data."""
    rep = report(data["metrics"], pace_s)
    for c in rep["checks"]:
        c["short"] = SHORT.get(c["key"], c["title"])
    return {"score": rep["score"], "checks": rep["checks"], "timing": rep["timing"], "stride": rep["stride"],
            "curves": data.get("curves") or {}, "curve_titles": CURVE_TITLES, "clip": data.get("clip"), "figure": data.get("figure"),
            "bones": BONES, "fps": data.get("fps"), "contacts": data.get("contacts")}


CURVE_TITLES = {"knee": "Knee bend", "hip": "Hip (thigh against the trunk)", "ankle": "Ankle", "elbow": "Elbow"}


def rate(key: str, value: float | None) -> tuple[str, float | None]:
    """('good' | 'low' | 'high' | '', score 0-100) for one measure."""
    spec = next((c for c in CHECKS if c[0] == key), None)
    if spec is None or value is None:
        return "", None
    lo, hi = spec[3]
    if lo <= value <= hi:
        return "good", 100.0
    dist = lo - value if value < lo else value - hi
    return ("low" if value < lo else "high"), max(0.0, 100 - 100 * dist / (2 * spec[5]))


def report(metrics: dict, pace_s: float | None = None) -> dict:
    """The checks with their rating and feedback, the timing numbers, step length from the pace, and the score."""
    checks, scores = [], []
    for key, title, unit, (lo, hi), what, _ in CHECKS:
        v = metrics.get(key)
        state, s = rate(key, v)
        if s is not None:
            scores.append(s)
        fb = FEEDBACK.get((key, state)) if state else None
        checks.append({"key": key, "title": title, "unit": unit, "value": v, "lo": lo, "hi": hi, "what": what, "state": state,
                       "score": round(s) if s is not None else None, "priority": None,
                       "summary": fb[0] if fb else "Not measured in this clip.", "why": fb[1] if fb else "", "cue": fb[2] if fb else "",
                       "drill": fb[3] if fb else ""})
    timing = [{"key": k, "title": t, "unit": u, "value": metrics.get(k), "what": w} for k, t, u, w in TIMING]
    stride = None
    if pace_s and metrics.get("cadence"):
        speed = A.METERS_PER_MILE / pace_s                       # meters a second
        step = speed / (metrics["cadence"] / 60)
        stride = {"step_m": round(step, 2), "stride_m": round(2 * step, 2), "pace": A.fmt_secs(pace_s)}
    order = {"high": 0, "low": 0, "good": 2, "": 3}
    focus = sorted([c for c in checks if c["state"] in ("low", "high")], key=lambda c: (order[c["state"]], rate(c["key"], c["value"])[1]))
    for i, c in enumerate(focus, 1):   # what to work on first: the furthest out of range
        c["priority"] = i
    return {"checks": checks, "timing": timing, "stride": stride, "score": round(sum(scores) / len(scores), 1) if scores else None,
            "focus": focus[:3]}


def clean(data: dict) -> dict:
    """What a phone sent, checked: numbers within sane limits, curves of 51 points, a figure of 33 points."""
    def num(x, lo, hi):
        return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and lo <= x <= hi else None
    m = data.get("metrics") or {}
    metrics = {k: num(m.get(k), -400, 5000) for k in METRIC_KEYS}
    curves = {}
    for k in CURVE_KEYS:
        c = (data.get("curves") or {}).get(k) or {}
        curves[k] = {side: [v for v in (num(x, -400, 400) for x in (c.get(side) or [])[:51]) if v is not None]
                     for side in ("left", "right")}
    fig = data.get("figure") or []
    figure = [[num(p[0], -1, 2), num(p[1], -1, 2)] for p in fig[:33] if isinstance(p, list) and len(p) >= 2]
    if len(figure) != 33 or any(v is None for p in figure for v in p):
        figure = []
    clip = data.get("clip") or {}
    frames = []
    for fr in (clip.get("f") or [])[:90]:
        pts = [[num(p[0], -2, 3), num(p[1], -2, 3)] for p in fr[:33] if isinstance(p, list) and len(p) >= 2] if isinstance(fr, list) else []
        if len(pts) == 33 and all(v is not None for p in pts for v in p):
            frames.append(pts)
    times = [num(t, 0, 30) for t in (clip.get("t") or [])[:len(frames)]]
    clip = {"t": times, "f": frames} if frames and len(times) == len(frames) and None not in times else None
    if metrics.get("cadence") is None:
        raise ValueError("The analysis didn't find a step rate. Film again with the whole body in frame.")
    return {"metrics": metrics, "curves": curves, "figure": figure, "clip": clip,
            "fps": num(data.get("fps"), 1, 1000), "seconds": num(data.get("seconds"), 0, 600),
            "contacts": int(num(data.get("contacts"), 0, 1000) or 0), "camera": str(data.get("camera") or "")[:12],
            "near": str(data.get("near") or "")[:6]}


# ---------- the PDF ----------

BONES = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24), (23, 25), (25, 27), (27, 29), (29, 31),
         (27, 31), (24, 26), (26, 28), (28, 30), (30, 32), (28, 32), (7, 0), (8, 0)]
STATE_COLOR = {"good": (29, 106, 71), "low": (179, 38, 30), "high": (179, 38, 30), "": (110, 110, 110)}


def _figure(pdf, fig: list, x: float, y: float, size: float) -> None:
    if not fig:
        return
    from .registration import INK, RED
    pdf.set_line_width(2.2)
    for a, b in BONES:
        near_left = a in (11, 13, 15, 23, 25, 27, 29, 31) and b in (11, 13, 15, 23, 25, 27, 29, 31)
        pdf.set_draw_color(*(RED if near_left else INK))
        pdf.line(x + fig[a][0] * size, y + fig[a][1] * size, x + fig[b][0] * size, y + fig[b][1] * size)
    pdf.set_fill_color(*INK)
    head = fig[0]
    pdf.ellipse(x + head[0] * size - 7, y + head[1] * size - 9, 14, 14, "F")
    pdf.set_line_width(0.5)


def _chart(pdf, title: str, series: dict, x: float, y: float, w: float, h: float) -> None:
    from .registration import INK, LINE, MUTED, RED
    vals = [v for s in series.values() for v in s]
    pdf.set_font("InterSemi", size=9)
    pdf.set_text_color(*INK)
    pdf.set_xy(x, y)
    pdf.cell(w, 12, title)
    top = y + 16
    if not vals:
        pdf.set_font("Inter", size=8)
        pdf.set_xy(x, top + h / 2)
        pdf.cell(w, 10, "Not enough strides seen")
        return
    lo, hi = min(vals), max(vals)
    pad = max(5, (hi - lo) * 0.1)
    lo, hi = math.floor((lo - pad) / 10) * 10, math.ceil((hi + pad) / 10) * 10
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.4)
    pdf.set_font("Inter", size=6.5)
    pdf.set_text_color(*MUTED)
    for i in range(3):
        v = lo + (hi - lo) * i / 2
        yy = top + h - (v - lo) / (hi - lo) * h
        pdf.line(x + 22, yy, x + w, yy)
        pdf.set_xy(x, yy - 4)
        pdf.cell(20, 8, f"{v:.0f}°", align="R")
    for i, lab in enumerate(("0%", "50%", "100% of stride")):
        pdf.set_xy(x + 22 + (w - 22) * i / 2 - (40 if i == 2 else 10), top + h + 2)
        pdf.cell(60, 8, lab)
    colors = {"left": (27, 79, 160), "right": RED}
    pdf.set_line_width(1.2)
    for side, s in series.items():
        if len(s) < 2:
            continue
        pdf.set_draw_color(*colors[side])
        pts = [(x + 22 + (w - 22) * i / (len(s) - 1), top + h - (v - lo) / (hi - lo) * h) for i, v in enumerate(s)]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            pdf.line(x1, y1, x2, y2)
    pdf.set_font("Inter", size=7)
    for i, side in enumerate(("left", "right")):
        pdf.set_text_color(*colors[side])
        pdf.set_xy(x + w - 70 + i * 36, y)
        pdf.cell(34, 12, side.title())
    pdf.set_line_width(0.5)


def pdf(fa, athlete_name: str, team: str, logo, by: str) -> bytes:
    """The report as a PDF: score, stick figure at a foot strike, each check with feedback, timing, joint curves."""
    from .registration import INK, LINE, MUTED, RED, SOFT, branded_page
    data = json.loads(fa.data)
    rep = report(data["metrics"], fa.pace_s)
    doc, w = branded_page(team, "Running form analysis", logo)
    doc.set_xy(48, 112)
    doc.set_text_color(*INK)
    doc.set_font("Barlow", size=22)
    doc.cell(w, 26, athlete_name, new_x="LMARGIN", new_y="NEXT")
    doc.set_font("Inter", size=10)
    doc.set_text_color(*MUTED)
    bits = [f"Filmed {A.local_time(fa.created_at):%b %-d, %Y}" if fa.created_at else "", f"by {by}" if by else "",
            f"{data.get('contacts', 0)} foot strikes", f"{data['fps']:.0f} frames a second" if data.get("fps") else ""]
    doc.cell(w, 14, " · ".join(b for b in bits if b), new_x="LMARGIN", new_y="NEXT")
    top = doc.get_y() + 10
    # score and stick figure
    doc.set_fill_color(*SOFT)
    doc.rect(48, top, w, 150, "F")
    _figure(doc, data.get("figure") or [], 70, top + 12, 125)
    doc.set_xy(230, top + 18)
    doc.set_font("InterSemi", size=11)
    doc.set_text_color(*INK)
    doc.cell(200, 14, "Form score")
    doc.set_xy(230, top + 34)
    doc.set_font("Barlow", size=40)
    doc.set_text_color(*RED)
    doc.cell(200, 44, f"{rep['score']:.0f}%" if rep["score"] is not None else "–")
    doc.set_xy(230, top + 84)
    doc.set_font("Inter", size=9)
    doc.set_text_color(*INK)
    focus = "; ".join(c["summary"].rstrip(".") for c in rep["focus"]) or "All checks in range"
    doc.multi_cell(w - 190, 12, f"Work on: {focus}." if rep["focus"] else "Every check is in the usual range. Keep it up.")
    doc.set_xy(48, top + 158)
    doc.set_font("Inter", size=8)
    doc.set_text_color(*MUTED)
    doc.multi_cell(w, 10, "The stick figure shows the runner as a foot lands (red: left side). The score is the share of checks in the "
                          "usual range for distance runners; these ranges are guides, and speed, build and injury history matter too.")
    doc.ln(6)
    # checks
    for c in rep["checks"]:
        need = 70 if c["state"] else 34
        if doc.get_y() > doc.h - 60 - need:
            doc.add_page()
            doc.set_y(48)
        y0 = doc.get_y()
        col = STATE_COLOR[c["state"]]
        doc.set_fill_color(*col)
        doc.rect(48, y0, 4, need - 6, "F")
        doc.set_xy(58, y0)
        doc.set_font("InterSemi", size=10.5)
        doc.set_text_color(*INK)
        doc.cell(w * 0.55, 14, c["title"])
        doc.set_font("InterSemi", size=10.5)
        doc.set_text_color(*col)
        val = f"{c['value']:g}{c['unit'] if c['unit'] in ('°', '') else ' ' + c['unit']}" if c["value"] is not None else "–"
        doc.cell(w * 0.45 - 10, 14, f"{val}   (good: {c['lo']:g} to {c['hi']:g})", align="R", new_x="LMARGIN", new_y="NEXT")
        doc.set_x(58)
        doc.set_font("InterSemi", size=9)
        doc.set_text_color(*INK)
        doc.multi_cell(w - 10, 11, c["summary"], new_x="LMARGIN", new_y="NEXT")
        doc.set_font("Inter", size=8.5)
        doc.set_text_color(*MUTED)
        for label, text in (("Why", c["why"]), ("Cue", c["cue"]), ("Drill", c["drill"])):
            if text and (c["state"] != "good" or label == "Why"):
                doc.set_x(58)
                doc.multi_cell(w - 10, 10.5, f"{label}: {text}", new_x="LMARGIN", new_y="NEXT")
        doc.ln(6)
    # timing and stride
    if doc.get_y() > doc.h - 200:
        doc.add_page()
        doc.set_y(48)
    doc.set_font("Barlow", size=14)
    doc.set_text_color(*INK)
    doc.cell(w, 20, "Stride timing", new_x="LMARGIN", new_y="NEXT")
    doc.set_font("Inter", size=9)
    for t in rep["timing"]:
        doc.set_text_color(*INK)
        doc.cell(110, 13, t["title"])
        doc.set_font("InterSemi", size=9)
        doc.cell(70, 13, f"{t['value']:g} {t['unit']}" if t["value"] is not None else "film at 60+ fps")
        doc.set_font("Inter", size=8.5)
        doc.set_text_color(*MUTED)
        doc.cell(w - 180, 13, t["what"], new_x="LMARGIN", new_y="NEXT")
        doc.set_font("Inter", size=9)
    if rep["stride"]:
        doc.set_text_color(*INK)
        doc.cell(w, 14, f"At {rep['stride']['pace']} a mile: step length {rep['stride']['step_m']} m, stride length {rep['stride']['stride_m']} m.",
                 new_x="LMARGIN", new_y="NEXT")
    # curves
    if doc.get_y() > doc.h - 300:
        doc.add_page()
        doc.set_y(48)
    doc.ln(8)
    doc.set_font("Barlow", size=14)
    doc.set_text_color(*INK)
    doc.cell(w, 20, "Joint angles through the stride", new_x="LMARGIN", new_y="NEXT")
    doc.set_font("Inter", size=8)
    doc.set_text_color(*MUTED)
    doc.multi_cell(w, 10, "From one foot strike to the next of the same foot. Left and right lines far apart can point to an imbalance worth a look.",
                   new_x="LMARGIN", new_y="NEXT")
    y = doc.get_y() + 6
    cw, ch = (w - 20) / 2, 90
    for i, k in enumerate(CURVE_KEYS):
        cx, cy = 48 + (i % 2) * (cw + 20), y + (i // 2) * (ch + 40)
        _chart(doc, CURVE_TITLES[k], data["curves"].get(k) or {}, cx, cy, cw, ch)
    doc.set_draw_color(*LINE)
    return bytes(doc.output())

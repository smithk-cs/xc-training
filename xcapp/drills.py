"""Form drills: how to do each drill the running-form feedback prescribes (form.FEEDBACK), with an animated stick-figure
demonstration (static/drill-anim.js draws the one with the same key) and, if the coach adds one, a link to their own
video. Shown on the Running form section's Drills page, from the Form focus groups, and to each athlete for their own
focus."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from . import analysis as A

# key: name, what it's for, how to do it (steps), the cue, how much. The key names the animation in drill-anim.js.
DRILLS: dict[str, dict] = {
    "a_skip": {"name": "A-skip", "for": "Landing under the hips, knee lift and a quick, active foot strike.",
               "steps": ["Skip forward, driving one knee up to hip height with the toes pulled up.",
                         "Punch that foot straight down so it lands under the hips, on the ball of the foot.",
                         "Swing the opposite arm forward, elbow at 90°. Stay tall.",
                         "Small, quick skips: rhythm matters more than height."],
               "cue": "Knee up, foot down under you.", "dose": "2-3 × 20 m in the warm-up."},
    "b_skip": {"name": "B-skip", "for": "Pawing the foot down and back instead of reaching out in front.",
               "steps": ["Start like an A-skip: knee up to hip height.",
                         "Let the lower leg swing out in front, almost straight.",
                         "Sweep the foot down and back, like pawing the ground, to land under the hips.",
                         "Keep it light and rhythmic; arms swing opposite."],
               "cue": "Reach, then paw it back.", "dose": "2-3 × 20 m in the warm-up."},
    "high_knees": {"name": "High knees", "for": "Quick knee drive and a leg that folds and comes through fast.",
                   "steps": ["Run with short, quick steps, bringing each knee up to hip height.",
                             "Land on the balls of the feet, under the hips.",
                             "Stay tall; don't lean back. Arms pump at 90°."],
                   "cue": "Quick and tall.", "dose": "2 × 20 m in the warm-up."},
    "butt_kicks": {"name": "Butt kicks", "for": "Folding the trailing leg so the heel comes up under the hip.",
                   "steps": ["Jog with short steps, letting each heel come up toward the seat.",
                             "Keep the knee pointing down; the thigh stays almost vertical.",
                             "Quick, relaxed rhythm; arms swing normally."],
                   "cue": "Heel up under the hip.", "dose": "2 × 20 m in the warm-up."},
    "quick_feet": {"name": "Quick feet", "for": "A quicker, lighter step that lands under the body.",
                   "steps": ["Take tiny, very fast steps, moving forward slowly.",
                             "Feet barely leave the ground; land on the balls of the feet under the hips.",
                             "Arms pump quickly and match the feet. Stay tall and relaxed."],
                   "cue": "Fast and light, like the ground is hot.", "dose": "3-4 × 10 s, then stride out 20 m."},
    "straight_leg_bounds": {"name": "Straight-leg bounds", "for": "Pulling the foot down and back from the hip.",
                            "steps": ["Keep both legs nearly straight, knees soft but not bending much.",
                                      "Swing each leg forward from the hip, then pull it down and back hard.",
                                      "Land on the ball of the foot under the hips and bounce off it.",
                                      "Big opposite-arm swing."],
                            "cue": "Pull the ground back under you.", "dose": "2 × 20 m."},
    "marching": {"name": "A-march", "for": "Posture and knee lift, slowly, before doing A-skips.",
                 "steps": ["Walk forward tall, lifting one knee to hip height with the toes pulled up.",
                           "Step down under the hips on the ball of the foot.",
                           "Opposite arm forward at 90°. Hold the top for a moment each step."],
                 "cue": "Tall, knee up, toe up.", "dose": "2 × 20 m."},
    "wall_drill": {"name": "Wall drill", "for": "Leaning from the ankles and driving the knee, with the foot landing under the knee.",
                   "steps": ["Stand an arm's length from a wall and lean into it with straight arms.",
                             "Make one straight line from head to heels: lean from the ankles, not the waist.",
                             "Drive one knee up, toes up, then put it back down under the hip.",
                             "Switch legs. Do singles first, then quick switches."],
                   "cue": "Straight line, knee drive.", "dose": "2 × 10 each leg."},
    "falling_start": {"name": "Falling start", "for": "Leaning from the ankles instead of standing upright or bending at the waist.",
                      "steps": ["Stand tall, feet together.",
                                "Keeping the body straight, lean forward from the ankles.",
                                "Lean until you have to step, then run 20 m with that lean.",
                                "Don't bend at the waist."],
                      "cue": "Fall forward, then run.", "dose": "4-5 × 20 m."},
    "strides": {"name": "Strides", "for": "Practicing good form at a faster pace.",
                "steps": ["On flat grass or a track, build up over 80-100 m to about mile-race effort.",
                          "Stay relaxed: loose hands, shoulders down, quick light steps.",
                          "Walk back to recover. Think about one cue each time."],
                "cue": "Fast and relaxed.", "dose": "4-6 × 80-100 m after easy runs."},
    "low_strides": {"name": "Low and smooth strides", "for": "Less up-and-down bounce: pushing back along the ground, not up.",
                    "steps": ["Run strides of 80-100 m at a quick, relaxed pace.",
                              "Think of the head staying at one height, like running under a low ceiling.",
                              "Push back along the ground with each step rather than up."],
                    "cue": "Forward, not up.", "dose": "4-6 × 80-100 m after easy runs."},
    "tall_strides": {"name": "Tall-posture strides", "for": "Running tall, with the eyes ahead and the hips forward.",
                     "steps": ["Before each stride, stand tall and imagine a string pulling the head up.",
                               "Keep the chest up, hips forward and eyes about 20 m ahead.",
                               "Run 80-100 m relaxed and quick, holding that posture."],
                     "cue": "Tall, eyes ahead.", "dose": "4-6 × 80-100 m."},
    "hill_strides": {"name": "Hill strides", "for": "Knee lift, a slight forward lean and a powerful push-off.",
                     "steps": ["On a moderate hill, run up 60-80 m at a strong, quick effort.",
                               "Lean slightly into the hill from the ankles; drive the knees and arms.",
                               "Walk or jog back down to recover fully."],
                     "cue": "Drive the knees, push the hill away.", "dose": "4-6 × 60-80 m."},
    "bounding": {"name": "Bounding", "for": "Opening up the stride from the hips with a strong push-off.",
                 "steps": ["Run with exaggerated, springy strides, pushing hard off the back foot.",
                           "Drive the front knee up and forward and float before landing.",
                           "Land under the hips and bounce off again. Big arm swing."],
                 "cue": "Push off and float.", "dose": "2-3 × 30 m, on grass."},
    "metronome": {"name": "Step-rate run (metronome)", "for": "A quicker step rate (about 5% more) without forcing it.",
                  "steps": ["Set the metronome to your target steps a minute (your step rate plus about 5%).",
                            "Run easily so that each foot lands on a click.",
                            "Do 1-2 minutes at a time inside an easy run, then run normally and let it settle."],
                  "cue": "Slightly quicker, lighter steps.", "dose": "3-4 × 1-2 min in an easy run."},
    "bridges": {"name": "Glute bridge", "for": "Hip strength, so the hips stay high when the foot lands.",
                "steps": ["Lie on your back, knees bent, feet flat and close to the seat.",
                          "Squeeze the glutes and lift the hips until there's a straight line from shoulders to knees.",
                          "Hold for a second, then lower slowly. Progress to one leg."],
                "cue": "Squeeze, lift, straight line.", "dose": "2-3 × 12-15."},
    "single_leg_squat": {"name": "Single-leg squat", "for": "Hip and knee control on one leg, as in every running step.",
                         "steps": ["Stand on one leg, the other foot just off the ground in front.",
                                   "Sit the hips back and bend the knee as far as you can with control.",
                                   "Keep the knee in line with the toes (not caving in), then stand back up."],
                         "cue": "Knee over toes, hips back.", "dose": "2-3 × 8-10 each leg."},
    "plank": {"name": "Plank", "for": "Core strength, so the trunk stays tall instead of folding at the waist.",
              "steps": ["Rest on the forearms with the elbows under the shoulders.",
                        "Lift onto the toes so the body makes one straight line from head to heels.",
                        "Squeeze the glutes and keep breathing. Don't let the hips sag or rise."],
              "cue": "Straight line, breathe.", "dose": "3 × 30-45 s."},
    "dead_bug": {"name": "Dead bug", "for": "Core control while the arms and legs move, as in running.",
                 "steps": ["Lie on your back, arms pointing at the ceiling, knees and hips at 90°.",
                           "Press the lower back into the floor.",
                           "Slowly lower one arm overhead and the opposite leg toward the floor, then return.",
                           "Switch sides. Keep the lower back down the whole time."],
                 "cue": "Back flat, move slowly.", "dose": "2-3 × 8 each side."},
    "arm_swing": {"name": "Arm-swing drill", "for": "Elbows near 90° with a relaxed, forward-and-back swing.",
                  "steps": ["Stand tall, one foot slightly in front of the other.",
                            "Bend the elbows to about 90° and swing the arms from the shoulders.",
                            "Hands go from hip height to chest height, forward and back, not across the body.",
                            "Relax the hands and shoulders; speed up for the last few seconds."],
                  "cue": "Swing from the shoulder, hands relaxed.", "dose": "3 × 20 s."},
}

# What each form-feedback outcome prescribes (form.FEEDBACK: (measure, state)) → drills, most useful first
FOR_FEEDBACK: dict[tuple[str, str], list[str]] = {
    ("overstride_deg", "high"): ["a_skip", "quick_feet", "metronome"],
    ("overstride_deg", "low"): ["tall_strides"],
    ("overstride_deg", "good"): ["strides"],
    ("shin_deg", "low"): ["wall_drill"],
    ("shin_deg", "high"): ["b_skip", "straight_leg_bounds", "quick_feet"],
    ("shin_deg", "good"): ["quick_feet", "strides"],
    ("front_knee_deg", "low"): ["bridges", "single_leg_squat", "marching"],
    ("front_knee_deg", "high"): ["quick_feet", "strides"],
    ("front_knee_deg", "good"): ["hill_strides"],
    ("back_knee_deg", "high"): ["butt_kicks", "high_knees", "a_skip"],
    ("back_knee_deg", "good"): ["butt_kicks", "high_knees"],
    ("trunk_deg", "low"): ["falling_start"],
    ("trunk_deg", "high"): ["plank", "dead_bug", "tall_strides"],
    ("trunk_deg", "good"): ["hill_strides"],
    ("head_deg", "low"): ["tall_strides"],
    ("head_deg", "high"): ["tall_strides", "arm_swing"],
    ("elbow_deg", "low"): ["arm_swing", "strides"],
    ("elbow_deg", "high"): ["arm_swing"],
    ("elbow_deg", "good"): ["arm_swing"],
    ("cadence", "low"): ["metronome", "quick_feet"],
    ("cadence", "high"): ["hill_strides", "bounding"],
    ("cadence", "good"): ["strides"],
    ("oscillation_pct", "high"): ["low_strides", "quick_feet"],
}

VIDEOS_KEY = "drill_videos"


def for_focus(key: str, state: str) -> list[dict]:
    """The drills for one form-feedback outcome, each {"key", "name", ...}."""
    return [{"key": k, **DRILLS[k]} for k in FOR_FEEDBACK.get((key, state), [])]


def used_for() -> dict[str, list[str]]:
    """drill key → the form problems it's prescribed for (their feedback titles)."""
    from . import form as F
    out: dict[str, list[str]] = {}
    for (measure, state), keys in FOR_FEEDBACK.items():
        if state == "good":
            continue
        title = F.FEEDBACK.get((measure, state), ("",))[0].rstrip(".")
        for k in keys:
            out.setdefault(k, []).append(title)
    return out


def videos(db: Session) -> dict[str, str]:
    """The coach's own video link for each drill that has one."""
    try:
        raw = json.loads(A.get_setting(db, VIDEOS_KEY, "{}") or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in raw.items() if k in DRILLS and isinstance(v, str) and video_ok(v)}


def video_ok(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.netloc) and not u.username and len(url) <= 500


def set_video(db: Session, key: str, url: str) -> None:
    """Save (or with an empty url, remove) the coach's video link for one drill; ValueError for a bad link."""
    if key not in DRILLS:
        raise ValueError("Unknown drill.")
    url = (url or "").strip()
    if url and not video_ok(url):
        raise ValueError("The video link should start with https://, such as a YouTube or Google Drive link.")
    links = videos(db)
    if url:
        links[key] = url
    else:
        links.pop(key, None)
    A.set_setting(db, VIDEOS_KEY, json.dumps(links, separators=(",", ":")))

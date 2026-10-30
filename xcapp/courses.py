"""Comparing race times like for like: a course factor and a race-day conditions factor on pace.

The baseline is a track at 55°F or cooler, at sea level, with a low dew point. A time is turned into what it would be
there: pace is slowed by the conditions (a percentage) and by the course (seconds per mile), so the adjusted pace is
    (pace / (1 + conditions % / 100)) - course seconds per mile - footing seconds per mile.

Temperature and altitude follow Daniels (Running Formula, tables 6.1 and 6.2). His temperature table is built on
marathons by fast runners, so it's a starting point for high school 5K efforts. The humidity, hill, surface, turns and
footing values are PLACEHOLDERS with no validated source: coaches can override a course's adjustment, and the plan is to
let each course's own results correct them over a season.
"""
from __future__ import annotations

from .models import Course, MeetConditions

METERS_PER_MILE = 1609.344
# Race distances offered as buttons on the Courses page: label -> meters.
KM_DISTANCES = {f"{k}K": k * 1000.0 for k in (2, 3, 4, 5)}
MILE_DISTANCES = {f"{m:g} mi": m * METERS_PER_MILE for m in (1.5, 2, 2.25, 2.5)}
# Older choices, no longer offered but still read and named on courses saved with them.
RETIRED = {**{f"{k}K": k * 1000.0 for k in (6, 8, 10)}, **{f"{m:g} mi": m * METERS_PER_MILE for m in (1, 3, 4)}}
DISTANCES = {**KM_DISTANCES, **MILE_DISTANCES, **RETIRED}


def main_distance(raced: list[str], current: float | None) -> float | None:
    """The distance the total climb is measured over: the saved one while it's still ticked, else 5K, else the longest."""
    if not raced:
        return None
    if current and distance_label(current) in raced:
        return current
    return DISTANCES["5K"] if "5K" in raced else max(DISTANCES[k] for k in raced)


def distance_label(meters: float | None) -> str:
    """'5K' or '3 mi' for a standard distance, else '3.11 mi'."""
    if not meters:
        return ""
    return next((k for k, m in DISTANCES.items() if abs(m - meters) < 1), f"{meters / METERS_PER_MILE:.2f} mi")
SURFACES = {"Track": 0, "Road": 0, "Firm grass": 5, "Soft grass or dirt": 10, "Trail": 12, "Sand or mud": 25}  # s/mile
TURNS = {"Few": 0, "Some": 2, "Many": 4}                                                                      # s/mile
FOOTING = {"": 0, "Dry": 0, "Wet": 3, "Muddy": 10}                                                            # s/mile on the day
# Hill tier from feet climbed per mile: up to 10 is 1 (flat), 25 is 2, 45 is 3, 70 is 4, more is 5 (very hilly).
HILL_TIERS = [(10, 1), (25, 2), (45, 3), (70, 4)]
TIER_SECONDS = {1: 0, 2: 5, 3: 10, 4: 18, 5: 28}                                                              # s/mile
TEMP_BASE_F, TEMP_PCT_PER_F = 55, 0.154      # Daniels: about 0.77% slower for every 5°F over 55°F
# Daniels' altitude table for acclimatized runners (middle of each range), feet -> % slower; little effect below ~1,000 ft.
ALTITUDE = [(1000, 0.0), (3281, 0.675), (4921, 1.95), (6562, 3.25), (7382, 4.0)]
# Dew point (°F) tiers -> % slower. Daniels gives no number for humidity: a starting guess for coaches to adjust.
DEW_TIERS = [(55, 0.0), (60, 0.5), (65, 1.0), (70, 2.0)]
DEW_TOP = 3.0


def hill_tier(c: Course) -> int | None:
    """The coach's tier, else one from the climb per mile."""
    if c.hill_tier:
        return c.hill_tier
    if c.elevation_gain_ft is None or not c.distance_m:
        return None
    per_mile = c.elevation_gain_ft / (c.distance_m / METERS_PER_MILE)
    return next((tier for limit, tier in HILL_TIERS if per_mile <= limit), 5)


def course_seconds(c: Course | None) -> float:
    """Seconds per mile this course adds to a track pace: the coach's figure, else hills + surface + turns."""
    if c is None:
        return 0.0
    if c.adjust_s_per_mile is not None:
        return c.adjust_s_per_mile
    return TIER_SECONDS.get(hill_tier(c) or 1, 0) + SURFACES.get(c.surface, 0) + TURNS.get(c.turns, 0)


def temp_pct(f: float | None) -> float:
    return max(0.0, f - TEMP_BASE_F) * TEMP_PCT_PER_F if f is not None else 0.0


def altitude_pct(ft: float | None) -> float:
    if not ft or ft <= ALTITUDE[0][0]:
        return 0.0
    for (a, pa), (b, pb) in zip(ALTITUDE, ALTITUDE[1:]):
        if ft <= b:
            return pa + (pb - pa) * (ft - a) / (b - a)
    return ALTITUDE[-1][1]


def dew_pct(f: float | None) -> float:
    if f is None:
        return 0.0
    return next((pct for limit, pct in DEW_TIERS if f < limit), DEW_TOP)


def conditions_pct(cond: MeetConditions | None, course: Course | None) -> float:
    """How much slower (%) the day's heat, humidity and the course's altitude make the same effort."""
    return (temp_pct(cond.temp_f) + dew_pct(cond.dew_point_f) if cond else 0.0) + altitude_pct(course.altitude_ft if course else None)


def adjusted_time(time_s: float, meters: float, course: Course | None, cond: MeetConditions | None) -> float | None:
    """The time on a track at 55°F, sea level and a low dew point. None when nothing is known about the race."""
    if course is None and cond is None or not meters:
        return None
    miles = meters / METERS_PER_MILE
    pace = time_s / miles
    pace = pace / (1 + conditions_pct(cond, course) / 100) - course_seconds(course) - FOOTING.get(cond.footing if cond else "", 0)
    return pace * miles


def explain(course: Course | None, cond: MeetConditions | None) -> str:
    """'4.2% slower for heat and humidity, 15 s/mile for the course' for the Races page."""
    bits = []
    pct = conditions_pct(cond, course)
    if pct:
        bits.append(f"{pct:.1f}% for heat, humidity and altitude")
    s = course_seconds(course) + FOOTING.get(cond.footing if cond else "", 0)
    if s:
        bits.append(f"{s:g} s/mile for the course" + (" and footing" if cond and FOOTING.get(cond.footing, 0) else ""))
    return "; ".join(bits) or "no adjustment"

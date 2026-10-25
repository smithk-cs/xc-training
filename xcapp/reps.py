"""Find the work reps in a lapped workout and measure heart-rate recovery after each.

Two cases:
1. Structured workouts from the watch: laps carry an intensity (active/interval vs rest/recovery).
2. Manual lap presses: reps are the clearly faster laps, separated from slower jog laps.
   Auto-laps on an easy run have no clear speed gap, so no reps are reported.

Heart-rate recovery is the drop in beats per minute during the first 60 seconds after a rep ends,
measured only when the athlete actually rests or jogs between reps (not after the final rep).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .fitfile import ParsedLap

WORK = {"active", "interval"}
EASY = {"rest", "recovery"}
MIN_REP_M = 100.0
MIN_SPEED_GAP = 1.15  # reps must be at least 15% faster than the jog laps
SAME_DISTANCE = 0.25  # reps are repeats: within 25% of the typical fast-lap distance
RECOVERY_WINDOW_S = 60.0
MATCH_TOLERANCE_S = 3.0
MIN_REST_S = 55.0


@dataclass
class LapAnalysis:
    is_rep: bool = False
    rep_num: int | None = None
    hr_drop_60s: float | None = None


def detect_reps(laps: list[ParsedLap]) -> list[int]:
    """Indices of laps that are work reps."""
    if len(laps) < 2:
        return []
    intensities = {l.intensity for l in laps}
    if intensities & (EASY | {"interval"}):
        return [i for i, l in enumerate(laps) if l.intensity in WORK and l.distance_m >= MIN_REP_M and l.timer_s > 0]

    cand = [i for i, l in enumerate(laps) if l.distance_m >= MIN_REP_M and l.timer_s > 0]
    if len(cand) < 3:
        return []
    speeds = sorted(laps[i].speed for i in cand)
    gaps = []
    for k in range(1, len(speeds)):
        lo, hi = speeds[k - 1], speeds[k]
        if lo > 0 and hi / lo >= MIN_SPEED_GAP and len(speeds) - k >= 2:
            gaps.append((hi / lo, (lo + hi) / 2))
    # Try the clearest speed gap first. A valid split gives at least two fast laps of about the same
    # distance (warm-up and cool-down laps are usually longer), each followed by a slower lap.
    for _, threshold in sorted(gaps, reverse=True):
        fast = [i for i in cand if laps[i].speed > threshold]
        dists = sorted(laps[i].distance_m for i in fast)
        med = dists[len(dists) // 2] if len(dists) % 2 else (dists[len(dists) // 2 - 1] + dists[len(dists) // 2]) / 2
        reps = [i for i in fast if abs(laps[i].distance_m - med) <= SAME_DISTANCE * med]
        if len(reps) < 2:
            continue
        rep_set = set(reps)
        separated = sum(1 for i in reps if i + 1 < len(laps) and (i + 1) not in rep_set)
        if separated >= len(reps) - 1:
            return reps
    return []


def _hr_at(samples: list[tuple[float, int]], times: list[float], t: float, before_only: bool) -> int | None:
    if not samples:
        return None
    j = bisect_right(times, t)
    best = None
    for k in (j - 1, j):
        if 0 <= k < len(samples):
            st, hr = samples[k]
            if before_only and st > t + 1e-6:
                continue
            d = abs(st - t)
            if d <= MATCH_TOLERANCE_S + (2.0 if before_only else 0.0) and (best is None or d < best[0]):
                best = (d, hr)
    return best[1] if best else None


def analyze(laps: list[ParsedLap], hr_samples: list[tuple[float, int]], activity_start) -> list[LapAnalysis]:
    out = [LapAnalysis() for _ in laps]
    reps = detect_reps(laps)
    if not reps:
        return out
    samples = sorted(hr_samples)
    times = [s[0] for s in samples]
    for n, i in enumerate(reps, start=1):
        out[i].is_rep = True
        out[i].rep_num = n
        if n == len(reps):
            continue  # no rest after the last rep, only the cool-down
        lap = laps[i]
        end = (lap.start - activity_start).total_seconds() + lap.elapsed_s
        next_rep_start = (laps[reps[n]].start - activity_start).total_seconds()
        if next_rep_start - end < MIN_REST_S:
            continue
        hr_end = _hr_at(samples, times, end, before_only=True)
        hr_after = _hr_at(samples, times, end + RECOVERY_WINDOW_S, before_only=False)
        if hr_end is not None and hr_after is not None:
            out[i].hr_drop_60s = float(hr_end - hr_after)
    return out

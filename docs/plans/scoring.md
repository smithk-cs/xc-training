# Scoring Spec

Source: Jack Daniels, *Daniels' Running Formula*, 4th ed. (Human Kinetics, 2022).
Items marked **PLACEHOLDER** are not from the book. Keep them in config.

## 1. Goals

- Score each athlete against their own plan so different mileage levels are comparable.
- Reward doing the planned work at the planned intensity.
- Easy for high school athletes to understand; low maintenance for coaches.

Athlete-facing explanation (use this wording as the basis for in-app help):

> Every minute you run earns points. Harder minutes earn more. Your coach sets your weekly
> point goal. Hit it and you're at 100%.

## 2. Zones and points

Points per minute (from Daniels' training-stress points, Figure 4.5). The 10K-pace (0.8) and
Fast Repetition (2.0) zones are intentionally omitted for simplicity; add them later via config.

| Code | Name | Pts/min | Daniels zone | Intensity (% of VDOT) |
|---|---|---|---|---|
| S | Steady | 0.2 | Easy (E) | 59-74 (book) |
| M | Moderate | 0.4 | Marathon (M) | 75-84 (book) |
| T | Threshold | 0.6 | Threshold (T) | about 84-88 (book: "mid- to upper-80s"; VERIFY exact bounds) |
| I | Interval | 1.0 | Interval (I) | about 95-100 (VERIFY against VDOT tables) |
| R | Repetition | 1.5 | Repetition (R) | 105-110 (book) |

"Moderate" is the assumed name for the Marathon-equivalent zone. It is a display label only;
code uses `M`.

The authoritative pace for each zone comes from the VDOT tables (Chapter 5). If the app
computes paces instead of storing the tables, use the reference implementation in section 10
and validate its output against the book's tables in a test.

## 3. Weekly targets

- **Target points** per athlete per week, set by the coach. Daniels' reference points for
  context only: about 50/week for beginning high schoolers, 100 a year or two later,
  150 in college, 200+ after.
- **Target days** per athlete per week. Default **6**.
- **Planned points per day** are derived from the coach's plan: each planned segment has a
  zone and minutes; planned points = sum(minutes x rate). If the plan is written in miles for
  steady runs, convert to minutes using the athlete's steady pace from their VDOT snapshot.
- Week-over-week target increases are capped at about 10% (Daniels' mileage guidance).
  Warn the coach; do not block.

## 4. Per-workout processing (Garmin FIT data)

1. Read per-second (or per-record) data. Use **moving time only**; exclude paused time.
2. Smooth pace with a rolling window (default **30 seconds**) to reduce GPS noise.
3. Convert pace to intensity (% of the athlete's snapshotted VDOT pace) and classify each
   record into a zone using the midpoint rule below.
4. Points earned = sum(seconds / 60 x zone rate).
5. Treadmill or indoor runs: accept only with calibrated or footpod pace; otherwise flag
   "pace unreliable" and score as Steady for the full duration (coach can override).

### Zone classification

Zone bounds are expressed as % of VDOT. Where adjacent zones have gaps, split the gap at the
midpoint.

- Slower than `min_running_pct` (PLACEHOLDER default 50% of VDOT) or walking: 0 points.
- Below the Steady lower bound but above `min_running_pct`: Steady.
- Faster than the Repetition upper bound: Repetition.
- Otherwise: nearest zone by midpoint cutoffs. With the default bounds above the cutoffs are
  S/M 74.5, M/T 84, T/I 91.5, I/R 102.5 (% of VDOT). Recompute from config, do not hardcode.

All of these live in config. Conditions and course adjustments (see
`conditions-and-courses.md`) shift the expected pace before classification.

## 5. Daily cap

```
day_points = min(earned_points, planned_points_for_that_day)
```

Effects: running faster than planned earns nothing extra; running slower earns less; an
easy day run at threshold pace cannot inflate the score. This is the key anti-gaming rule.

Unplanned extra runs count as Steady minutes, limited so the weekly total never exceeds
**110%** of the weekly target.

Known limitation: the cap is per day, not per segment. A strong interval session can offset
a short warm-up shortfall. Per-segment caps are a possible later refinement.

## 6. Weekly score

```
points_pct = min(sum(day_points) / target_points, 1.0)
days_pct   = min(days_run / target_days, 1.0)
base       = 100 * (0.80 * points_pct + 0.20 * days_pct)
score      = base + min(bonus, max_bonus)
```

- A day counts as "run" if moving time >= `min_run_minutes` (default **30**, from Daniels'
  suggested minimum easy run).
- Weights `0.80 / 0.20` are config.
- Display the weekly score and a rolling fitness trend (exponentially weighted moving
  average over the last 4-8 weeks, config).
- Show the breakdown: points %, days %, bonus. Never show only an opaque number.

## 7. Bonus layer (additive, capped)

- **Race PR** at a standard distance: fixed points scaled by margin, capped.
- **VDOT gain:** reward increases in VDOT from a race or time trial. Scale as a percent of the
  athlete's own level (Daniels' diminishing returns: gains get harder as fitness rises).
- **Course improvement:** same course ID and distance, compared on conditions-adjusted time.
  Once per course per rolling window (default 30 days) to prevent farming.
- `max_bonus` is config (PLACEHOLDER default 10).

## 8. Excused days and layoffs

- **Excused day** (illness, injury, travel): remove that day's planned points from the target.
  Do not count it against days run (reduce `target_days` by one).
- **Breaks of 6+ days:** apply Daniels' Table 9.1 VDOT adjustment fractions and Table 9.2
  workload ramp. Training-break VDOT fractions (FVDOT-1 = no leg-based cross-training,
  FVDOT-2 = with cross-training):

| Days off | FVDOT-1 | FVDOT-2 |
|---|---|---|
| up to 5 | 1.000 | 1.000 |
| 6 | .997 | .998 |
| 7 | .994 | .997 |
| 10 | .985 | .992 |
| 14 | .973 | .986 |
| 21 | .952 | .976 |
| 28 | .931 | .965 |
| 35 | .910 | .955 |
| 42 | .889 | .944 |
| 49 | .868 | .934 |
| 56 | .847 | .923 |
| 63 | .826 | .913 |
| 70 | .805 | .902 |
| 72+ | .800 | .900 |

  Interpolate for days between rows. Workload ramp (Table 9.2): 6-28 days off, first half of
  the return period at 50% load and second half at 75%; 4-8 weeks off, thirds at 33%, 50%,
  75% (see book for full categories). Apply the ramp to the weekly target and zone paces, and
  let the coach override.

## 9. Coach warnings (not scored)

From Daniels' workout limits (verify wording against the book before shipping):

- Long run: no more than the lesser of 25% of weekly mileage or 150 minutes
  (30% of weekly mileage if under 40 miles per week).
- Threshold work: no more than 10% of weekly mileage in a single session.
- Interval work: no more than the lesser of 8% of weekly mileage or 10K in a single session.
- Easy run shorter than 30 minutes.

Also flag: easy-day pace faster than the Steady range; weekly points jumping more than 10%
over the prior week; indoor runs with unreliable pace.

## 10. Reference implementation notes

Daniels-Gilbert oxygen-cost equations (v in meters/minute, t in minutes). Validate against the
book's VDOT tables in a test; the tables win if they differ.

```
VO2(v)      = -4.60 + 0.182258 * v + 0.000104 * v^2
pct_max(t)  = 0.8 + 0.1894393 * exp(-0.012778 * t) + 0.2989558 * exp(-0.1932605 * t)
VDOT        = VO2(v_race) / pct_max(t_race)        # v_race = distance / t_race

# pace for a given % of VDOT:
target_vo2 = pct * VDOT
solve 0.000104 v^2 + 0.182258 v - (4.60 + target_vo2) = 0   # take the positive root
```

```python
RATES = {"S": 0.2, "M": 0.4, "T": 0.6, "I": 1.0, "R": 1.5}

def day_points(records, zones, planned_pts):
    earned = sum(RATES.get(zone_for(r.smooth_pace, zones), 0) / 60
                 for r in records if r.moving)
    return min(earned, planned_pts)

def week_score(days, target_pts, target_days, min_minutes=30):
    pts = min(sum(d.points for d in days) / target_pts, 1.0)
    run_days = sum(d.minutes >= min_minutes for d in days)
    return 100 * (0.8 * pts + 0.2 * min(run_days / target_days, 1.0))
```

## 11. Required test fixtures

- Perfect week: score equals 100 before bonus.
- Missed week: points and days both reflect the miss.
- Easy days run at threshold pace: capped at planned points, flagged.
- Easy days run too slowly: reduced points.
- Week with an excused day: target reduced, no penalty.
- 14-day layoff: VDOT adjusted by .973 (or .986 with cross-training); load ramped.
- Garmin file with paused time and GPS spikes: paused time excluded, spikes smoothed.
- PR week: bonus applied and capped.
- Formula version change: old scores unchanged, new scores carry the new `score_version`.

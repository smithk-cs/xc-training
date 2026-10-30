# Conditions and Course Difficulty Spec

Purpose: make pacing, points, and "same course" improvements fair across hot days, altitude,
and hilly courses. Source for temperature and altitude: Daniels' Tables 6.1 and 6.2.
Items marked **PLACEHOLDER** are not from the book and must live in config.

## 1. Baseline ("standard conditions")

Factor = 1.00 under these conditions:

- Flat, firm surface (track or equivalent)
- Air temperature at or below **55 F (13 C)**. This is the zero point in Daniels' Table 6.2.
- Sea level
- Low dew point (PLACEHOLDER: at or below 50 F / 10 C)
- No significant wind

Note: "0% humidity" is not a real condition. Use dew point from weather data instead.

## 2. Total factor

Each component is a fractional slowdown (0.02 = 2% slower). Expected pace and zone
boundaries are multiplied by `(1 + total)`.

```
total = temperature + dew_point + altitude + course + surface
expected_pace_s_per_mile = vdot_zone_pace * (1 + total)
```

Store each component per workout (a "conditions snapshot") so old scores stay explainable.

## 3. Temperature (from Daniels' Table 6.2)

Derived from the table: roughly **0.77% slower per 5 F above 55 F**, about **0.15% per F**
(e.g. a 2:25 marathoner loses 1:07 at 60 F and 7:49 at 90 F; a 2:07 marathoner loses 0:59
and 6:53). The table is based on fast marathon times, so treat it as a starting point for
high school 5K-scale efforts.

```
temperature = max(0, temp_F - 55) * 0.0015      # cap at config max, default 100 F
```

Source of temperature: weather API historical or current data for the run's start time and
GPS location. Do not use the wrist sensor (reads warm).

Daniels also says personal data beats formulas. See section 7 on calibration.

## 4. Altitude (from Daniels' Table 6.1)

Added time (seconds) for runners with some time at altitude:

| Duration | 1,000 m (3,281 ft) | 1,500 m (4,921 ft) | 2,000 m (6,562 ft) | 2,250 m (7,382 ft) |
|---|---|---|---|---|
| 5 min | 1.5 | 3.75 | 6.0 | 7.75 |
| 10 min | 4.25 | 12.5 | 21.0 | 25.5 |
| 20 min | 9.75 | 30.0 | 51.0 | 61.0 |
| 30 min | 15.25 | 47.5 | 81.0 | 96.5 |

As percent slower (added time / duration): about 0.5-0.85% at 1,000 m; 1.3-2.6% at
1,500 m; 2-4.5% at 2,000 m; 2.6-5.4% at 2,250 m. Longer efforts are penalized more.

Implementation:

- Interpolate bilinearly over altitude and workout duration.
- Between sea level and 1,000 m, interpolate linearly from 0 (PLACEHOLDER simplification;
  Daniels notes real problems begin around 3,000 ft).
- Races of 800 m and shorter: no altitude adjustment (per Daniels).
- The table assumes some acclimatization. For athletes new to altitude, Daniels says paces
  will be slower still but gives no number. Provide a coach-set `unacclimatized_extra`
  config (PLACEHOLDER default 0).
- Source of altitude: Garmin barometric elevation if available; otherwise a terrain lookup
  from GPS.

## 5. Dew point / humidity (NOT in Daniels)

Daniels gives no humidity adjustment. Use dew point from the weather API and expose coach
configurable tiers, default to no adjustment until a coach or athletic trainer calibrates:

| Dew point | Adjustment (PLACEHOLDER) |
|---|---|
| at or below 50 F | 0 |
| 50-59 F | 0 (set by coach) |
| 60-64 F | 0 (set by coach) |
| 65 F and above | 0 (set by coach) |

Add a safety warning, separate from scoring, when temperature plus dew point is high
(thresholds are config, PLACEHOLDER). Coaches decide whether to modify practice.

## 6. Course factor

### 6.1 Computed from Garmin data

- Elevation gain per mile (ft/mi) from smoothed barometric elevation.
- Assign tiers 1-5. Starting values are **PLACEHOLDERS** to be replaced by calibration:

| Tier | Description | Elevation gain (ft/mi) | Starting adjustment |
|---|---|---|---|
| 1 | Flat | under 20 | 0% |
| 2 | Gently rolling | 20-50 | config |
| 3 | Rolling | 50-100 | config |
| 4 | Hilly | 100-200 | config |
| 5 | Very hilly | over 200 | config |

Sanity reference from Daniels (treadmill, Table 7.1 text): each 1% of grade is roughly
equivalent to 10-15 seconds per mile of pace. Use only as a check for sustained uphills.

### 6.2 Coach tags (dropdown, one-time per course)

- Surface: track, road, firm grass, soft grass or dirt, trail, sand or mud
- Turns: few, moderate, many
- Each tag maps to a config adjustment (PLACEHOLDER default 0).

### 6.3 Course records

Table `courses`: `id`, `name`, `distance_m`, `elevation_gain_ft_per_mi`, `surface_tag`,
`turns_tag`, `tier`, `computed_factor`, `calibrated_factor`, `n_observations`, `notes`.
Match a run to a course by saved route or GPS segment overlap (threshold in config) plus
distance tolerance.

### 6.4 Snapshot-based rating (optional first pass)

A coach may upload a Strava or map screenshot. Treat any rating from an image as a draft
for coach review; it cannot reliably show elevation, surface, or exact distance. Do not
scrape Strava pages that require login.

## 7. Self-calibration

Existing XC rating systems are results-based (MileSplit course ratings, FloRatings, speed
ratings, LACCTiC). A patent-described approach compares a runner's track time with their
time on a measured course (e.g. 15:30 on a track vs 16:30 on a course is a 6.45% rating).
Use the same idea at team scale:

1. For each hard effort or race on a course, compute `ratio = actual_pace / expected_pace`
   where expected pace uses the athlete's VDOT-predicted pace corrected for temperature,
   dew point, and altitude (not course).
2. `observed_course_factor = median(ratio) - 1` across athletes and efforts.
3. Blend with the computed starting factor:
   `calibrated = w * observed + (1 - w) * computed`, with `w = n / (n + k)` and `k`
   configurable (PLACEHOLDER 10).
4. Recompute on a schedule; version the calibration; show coaches the data behind it.
5. Require a minimum `n` (PLACEHOLDER 5) before overriding the computed value.

## 8. Uses of the factors

- **Pacing / zones:** shift expected zone paces before classification (see `scoring.md`).
- **Course improvement bonus:** compare conditions-adjusted times. Flag comparisons where
  total conditions factors differ by more than a threshold (PLACEHOLDER 2%) so humans can
  review.
- **Newsletter course snapshot:** distance, terrain note, prior team results.

## 9. Tests

- Baseline run yields total factor 0.
- 80 F, sea level, flat: temperature factor = 25 * 0.0015 = 3.75%.
- Altitude interpolation matches Table 6.1 at every table point.
- 800 m race at altitude: no adjustment.
- Course calibration moves toward observed ratio as `n` grows; stays at computed when n is
  below minimum.
- Changing a config value creates a new factor version without altering stored snapshots.

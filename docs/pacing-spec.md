# Pacing Spec: Jesuit Distance Runners Training Guide

**Status:** v1, transcribed from `Jesuit - Distance Running Guide.xlsx` (Sheet1). Read this whole file before writing or changing any pace, level, or workout code.
**Owner:** Coach Smith. Anything marked **OPEN** in Section 8 needs his decision. Do not resolve it by guessing.

---

## 1. Purpose and source of truth

This spec defines how the web app turns an athlete's race performances into a **performance level (1–19)** and from that level into training paces, interval workouts, route target times, and weekly mileage.

Rules for implementation:

1. **The tables in Section 3 are the source of truth.** Paces and workouts are looked up by level, not calculated from a formula. Do not substitute Jack Daniels' published VDOT equations or any other pace calculator. The VDOT numbers here are labels from the coach's guide, and the paces are the coach's own.
2. **One module owns all of this logic.** Put it in `src/lib/pacing/` (or the project's equivalent) as pure functions with no database, network, or UI imports. Every screen (coach dashboard, athlete mobile page, training calendar) calls this module. Never duplicate pace math elsewhere.
3. **Reference data lives in one data file** (e.g. `src/lib/pacing/trainingGuide.data.ts` or `.json`), transcribed exactly from Section 3. Do not correct values that look wrong. Section 7 lists known oddities, which stay as-is until the coach decides.
4. **Paces are always derived, never stored per athlete.** The database stores performances, the assigned level, and any coach override. Paces are computed at read time so they never go stale.

---

## 2. Conventions

- **Internal unit for time is seconds** (a number; may be fractional, e.g. `37.5`). Convert at the edges only.
- **Training paces are per mile.** Interval rep times are per rep. Route and long-run times are total elapsed time.
- **Distances:** race events and interval reps in meters; routes and long runs in miles.
- **Ranges are stored as `{ slow, fast }`** in seconds. The guide always writes the slow value first (e.g. `8:52-8:37`), so `slow` ≥ `fast` numerically.
- **Level numbering:** 1 is fastest, 19 is slowest. Athletes move from 19 toward 1 over their careers. "Higher level" is ambiguous, so code and UI text should say "faster level" / "slower level".
- **Time string parsing** must accept `ss`, `ss.s`, `m:ss`, `mm:ss`, and `h:mm:ss`. Interval cells use bare seconds for 400s and 200s (`@71`, `@37.5`) and `m:ss` for 1000s and 800s.
- **Display formatting:** `m:ss` for anything under an hour, `h:mm:ss` at or above (but long-run targets like `85:00` may display as minutes, matching the guide; see OPEN-7). Round to whole seconds for display except 200m reps, which keep one decimal.

---

## 3. Reference data

### 3.1 Performance levels and event equivalents

Event columns are the time an athlete at that level is expected to run. They double as the **benchmarks used to assign a level** (Section 4.2). 5k XC times assume a standard course. "Band" is the column color in the spreadsheet; the guide says most new runners start in the orange or green bands. The sheet also labels levels 19–12 "freshman year," 11–8 "sophomore," 7–4 "junior," and 3–1 "senior" as the expected progression. That is informational only and must not affect level assignment.

| Level | VDOT | Band | 800m | 1600m | 3200m | 5k XC | Team status (2011) | Weekly miles |
|---|---|---|---|---|---|---|---|---|
| 19 | 48 | orange | 2:40 | 6:00 | 12:49 | 20:42 | — | 20 |
| 18 | 50 | orange | 2:35 | 5:50 | 12:28 | 20:07 | — | 22 |
| 17 | 51 | orange | 2:32 | 5:40 | 12:07 | 19:37 | — | 24 |
| 16 | 53 | orange | 2:27 | 5:30 | 11:46 | 19:12 | — | 26 |
| 15 | 55 | green | 2:24 | 5:20 | 11:36 | 18:52 | Top 10 FS Jesuit | 28 |
| 14 | 56 | green | 2:20 | 5:15 | 11:25 | 18:35 | Top 6 FS Jesuit | 30 |
| 13 | 57 | green | 2:18 | 5:10 | 11:15 | 18:19 | Top 6 FS Jesuit | 33 |
| 12 | 58 | green | 2:16 | 5:05 | 11:05 | 18:03 | Top 6 FS Jesuit | 37 |
| 11 | 59 | bronze | 2:13 | 5:00 | 10:55 | 17:40 | Top 6 FS Jesuit | 40 |
| 10 | 60 | bronze | 2:11 | 4:55 | 10:41 | 17:25 | Top 4 FS Jesuit | 42 |
| 9 | 61 | bronze | 2:09 | 4:50 | 10:32 | 17:05 | Top 6 Jesuit | 45 |
| 8 | 62.5 | bronze | 2:07 | 4:45 | 10:19 | 16:51 | Top 6 FS DRL | 48 |
| 7 | 64 | silver | 2:05 | 4:40 | 10:11 | 16:37 | Top 4 FS DRL | 50 |
| 6 | 65 | silver | 2:03 | 4:35 | 9:58 | 16:18 | Top 10 DRL | 54 |
| 5 | 66 | silver | 2:01 | 4:30 | 9:47 | 16:00 | Top 4 DRL | 57 |
| 4 | 68 | silver | 1:59 | 4:25 | 9:35 | 15:47 | #1 DRL | 60 |
| 3 | 69 | gold | 1:57 | 4:20 | 9:25 | 15:28 | Top 8 SJS | 63 |
| 2 | 71 | gold | 1:54 | 4:15 | 9:11 | 15:10 | Top 25 Calif | 65 |
| 1 | 72.5 | gold | 1:52 | 4:10 | 9:00 | 14:54 | Top 25 USA | 70 |
"Team status" is the guide's 2011 estimate of where that level places on the team (FS = frosh-soph, DRL = Delta River League, SJS = Sac-Joaquin Section). It is display text only.

### 3.2 Training paces (per mile)

Easy runs are about 72–74% of max heart rate, tempo 87–90%, long runs 70–72%. Tempo is roughly one minute per mile slower than the athlete's 1600 time. Long run is a single target pace, not a range.

| Level | Easy slow | Easy fast | Tempo slow | Tempo fast | Long run |
|---|---|---|---|---|---|
| 19 | 8:52 | 8:37 | 7:20 | 7:05 | 8:49 |
| 18 | 8:38 | 8:24 | 7:09 | 6:59 | 8:32 |
| 17 | 8:24 | 8:11 | 6:57 | 6:41 | 8:24 |
| 16 | 8:11 | 7:58 | 6:46 | 6:33 | 8:08 |
| 15 | 7:57 | 7:44 | 6:35 | 6:25 | 7:54 |
| 14 | 7:50 | 7:37 | 6:29 | 6:16 | 7:47 |
| 13 | 7:44 | 7:30 | 6:24 | 6:11 | 7:41 |
| 12 | 7:37 | 7:22 | 6:18 | 6:05 | 7:34 |
| 11 | 7:30 | 7:18 | 6:12 | 6:00 | 7:28 |
| 10 | 7:23 | 7:07 | 6:07 | 5:58 | 7:22 |
| 9 | 7:16 | 7:00 | 6:01 | 5:53 | 7:16 |
| 8 | 7:09 | 6:55 | 5:55 | 5:43 | 7:05 |
| 7 | 7:02 | 6:50 | 5:50 | 5:38 | 6:59 |
| 6 | 6:56 | 6:42 | 5:44 | 5:32 | 6:54 |
| 5 | 6:49 | 6:35 | 5:38 | 5:27 | 6:49 |
| 4 | 6:42 | 6:30 | 5:32 | 5:21 | 6:39 |
| 3 | 6:35 | 6:25 | 5:27 | 5:16 | 6:35 |
| 2 | 6:28 | 6:18 | 5:21 | 5:10 | 6:26 |
| 1 | 6:21 | 6:10 | 5:15 | 5:05 | 6:19 |
### 3.3 Interval workouts (at vO₂max, ~100% max HR)

Each level has four interval options. Cells are stored verbatim and parsed with this grammar:

```
<repsMin>[-<repsMax>]x<distance>@<repTime>
distance: 1k → 1000 m, otherwise meters (800, 400, 200)
repTime:  m:ss, or bare seconds (may be decimal)
```

Example: `4-5x1k@3:27` → `{ repsMin: 4, repsMax: 5, distanceM: 1000, repSec: 207 }`. A single count like `9x400@71` has `repsMin = repsMax = 9`.

The guide says interval sessions should total 1–3 miles of fast running and gives one recovery example at level 11: 1k reps on a 7:00 send-off, 400 reps on a 2:30 send-off. Recovery for other levels is not specified (OPEN-5).

| Level | 1000m reps | 800m reps | 400m reps | 200m reps |
|---|---|---|---|---|
| 19 | `3x1k@4:00` | `3x800@3:08` | `6-7x400@90` | `10-12x200@40` |
| 18 | `3x1k@3:44` | `4x800@3:00` | `8x400@86` | `12x200@38` |
| 17 | `4x1k@3:50` | `4x800@3:00` | `8x400@86` | `12x200@37.5` |
| 16 | `4-5x1k@3:47` | `4x800@2:47` | `8x400@80` | `12x200@35` |
| 15 | `4-5x1k@3:43` | `4x800@2:42` | `8x400@74` | `12x200@33` |
| 14 | `4-5x1k@3:38` | `4x800@2:40` | `8x400@73` | `12x200@32` |
| 13 | `4-5x1k@3:34` | `4x800@2:38` | `8x400@72` | `12x200@32` |
| 12 | `4-5x1k@3:30` | `4x800@2:36` | `8x400@71` | `12x200@32` |
| 11 | `4-5x1k@3:27` | `5x800@2:37` | `9x400@71` | `14x200@32` |
| 10 | `4-5x1k@3:23` | `5x800@2:35` | `9x400@70` | `14x200@31.5` |
| 9 | `4-5x1k@3:19` | `5x800@2:31` | `9x400@68` | `14x200@31` |
| 8 | `4-5x1k@3:15` | `6x800@2:33` | `10x400@69` | `16x200@31.5` |
| 7 | `4-5x1k@3:11` | `6x800@2:30` | `10x400@68` | `16x200@31` |
| 6 | `4-5x1k@3:08` | `6x800@2:28` | `10x400@67` | `16x200@30.5` |
| 5 | `4-5x1k@3:02` | `6x800@2:25` | `10x400@66` | `16x200@30` |
| 4 | `4-5x1k@3:00` | `6x800@2:23` | `10x400@65` | `16x200@29.5` |
| 3 | `5-6x5k@3:00` | `6x800@2:20` | `10x400@64` | `16x200@29.3` |
| 2 | `5x1k@2:52` | `6x800@2:17` | `10x400@62` | `16x200@28.8` |
| 1 | `5-6x1k@2:54` | `6x800@2:15` | `10x400@62` | `16x200@28.5` |
**Optional display helper:** lap splits within a rep are linear, `splitSec = repSec × splitM / distanceM` (e.g. 1k @ 3:27 → 82.8 s per 400). Display rounded to whole seconds.

### 3.4 Route catalog

The guide names training routes but does not list their distances. The distances below were back-calculated as `route time ÷ pace` from the guide and are consistent across levels to within ±1 second (one exception in Section 7). **The coach should confirm them (OPEN-6).** Route IDs are new, made up for code; display names come from the guide.

| Route ID | Display name | Type | Distance (mi) |
|---|---|---|---|
| `watt_ave` | Watt Ave | easy | 4.96 |
| `mid_woods` | Mid-Woods | easy | 5.11 |
| `ten_k` | "10k" | easy | 6.01 |
| `little_bridge` | Little Bridge | easy | 6.34 |
| `drinking_fountain` | Drinking Fountain | easy | 6.52 |
| `nine_mile_mark` | 9 mi Mark | easy | 7.10 |
| `cordova_park` | Cordova Park | easy | 7.13 |
| `pump` | Pump | easy | 8.04 |
| `howe_ave` | Howe Ave | easy | 8.14 |
| `guy_west` | Guy West | easy | 9.36 |
| `seventeen_mile_mark` | 17mi mark | easy | 9.77 |
| `over_guy_west` | Over Guy West | easy | 9.84 |
| `rossmoor_bar` | Rossmoor Bar | easy | 10.62 |
| `t_14_5_mile_mark` | 14.5mi mark to Jesuit | tempo | 2.65 |
| `t_15_mile_mark` | 15mi mark to Jesuit | tempo | 3.15 |
| `t_little_bridge` | Little Bridge to Jesuit | tempo | 3.41 |
| `t_15_5_mile_mark` | 15.5mi mark to Jesuit | tempo | 3.65 |
| `t_cordova_4mi` | Cordova Pk 4mi tempo to Jesuit | tempo | 4.00 |

The guide's long-run cells imply slightly different distances for some of the same route names (e.g. Drinking Fountain 6.50 vs 6.52, Over Guy West 9.40 vs 9.84). See Section 7. Long-run route times are therefore stored verbatim, not computed.

### 3.5 Route workouts by level

Published target times. Store these verbatim; they are what the athlete sees. Easy runs list two route options. Long runs at levels 10–1 are a distance and time rather than a named route (stored as `distance_<miles>`).

| Level | Easy-run route options (slow–fast) | Tempo route (slow–fast) | Long run (target time) |
|---|---|---|---|
| 19 | `watt_ave` 43:58–42:44 ; `mid_woods` 45:18–44:01 | `t_14_5_mile_mark` 19:26–18:46 | `drinking_fountain` 57:18 ; `little_bridge` 55:32 |
| 18 | `watt_ave` 42:49–41:39 ; `mid_woods` 44:06–42:55 | `t_14_5_mile_mark` 18:56–18:30 | `drinking_fountain` 55:28 ; `little_bridge` 53:45 |
| 17 | `watt_ave` 41:39–40:35 ; `mid_woods` 42:55–41:49 | `t_15_mile_mark` 21:53–21:03 | `nine_mile_mark` 59:38 ; `cordova_park` 59:38 |
| 16 | `ten_k` 49:10–47:52 ; `little_bridge` 51:52–50:30 | `t_15_mile_mark` 21:18–20:37 | `nine_mile_mark` 57:44 ; `cordova_park` 57:44 |
| 15 | `ten_k` 47:46–46:28 ; `little_bridge` 50:24–49:01 | `t_15_mile_mark` 20:44–20:12 | `nine_mile_mark` 56:05 ; `cordova_park` 56:05 |
| 14 | `ten_k` 47:04–45:46 ; `little_bridge` 49:39–48:17 | `t_15_mile_mark` 20:25–19:44 | `nine_mile_mark` 55:15 ; `cordova_park` 55:15 |
| 13 | `ten_k` 46:28–45:04 ; `little_bridge` 49:01–47:33 | `t_15_mile_mark` 20:09–19:28 | `howe_ave` 62:14 ; `pump` 61:28 |
| 12 | `drinking_fountain` 49:39–48:01 ; `little_bridge` 48:17–46:42 | `t_15_mile_mark` 19:50–19:09 | `guy_west` 71:07 |
| 11 | `drinking_fountain` 48:54–47:35 ; `little_bridge` 47:33–46:16 | `t_little_bridge` 21:08–20:27 | `over_guy_west` 70:11 |
| 10 | `nine_mile_mark` 52:25–50:31 ; `cordova_park` 52:38–50:44 | `t_little_bridge` 20:51–20:20 | `distance_10.5` 77:21 |
| 9 | `nine_mile_mark` 51:35–49:42 ; `cordova_park` 51:48–49:54 | `t_little_bridge` 20:31–20:03 | `distance_11` 79:56 |
| 8 | `howe_ave` 58:12–56:18 ; `pump` 57:29–55:36 | `t_little_bridge` 20:10–19:29 | `distance_12` 85:00 |
| 7 | `howe_ave` 57:15–55:37 ; `pump` 56:32–54:56 | `t_15_5_mile_mark` 21:17–20:33 | `distance_12` 83:48 |
| 6 | `guy_west` 64:53–62:42 ; `seventeen_mile_mark` 67:44–65:27 | `t_15_5_mile_mark` 20:55–20:11 | `distance_13` 89:42 |
| 5 | `guy_west` 63:48–61:37 ; `seventeen_mile_mark` 66:35–64:19 | `t_15_5_mile_mark` 20:33–19:53 | `distance_13.2` 90:00 |
| 4 | `over_guy_west` 65:55–63:57 ; `rossmoor_bar` 71:09–69:01 | `t_15_5_mile_mark` 20:11–19:31 | `distance_13.5` 90:00 |
| 3 | `over_guy_west` 64:46–63:08 ; `rossmoor_bar` 69:54–68:08 | `t_cordova_4mi` 21:48–21:04 | `distance_14` 92:00 |
| 2 | `over_guy_west` 63:37–61:59 ; `rossmoor_bar` 68:40–66:54 | `t_cordova_4mi` 21:24–20:40 | `distance_14` 91:00 |
| 1 | `over_guy_west` 62:29–61:40 ; `rossmoor_bar` 67:26–65:29 | `t_cordova_4mi` 21:00–20:20 | `distance_14` 90:00 |
### 3.6 Volume guidance

The weekly mileage target per level is in Section 3.1. The guide also gives each component's share of total weekly miles:

| Component | Share of weekly miles (as written in guide) |
|---|---|
| Easy runs | 75% (guide text elsewhere says "about 70%") |
| Intervals | 8% |
| Tempo runs | 10% |
| Long run | 16–20% |

These add up to 109–113%, so they cannot all be applied literally (OPEN-4). Until the coach decides, the app may **display** these as guidance but must not enforce or auto-allocate mileage from them.

---

## 4. Calculations

All functions are pure. Signatures are suggestions; match the project's language and naming style.

### 4.1 Time helpers

- `parseTime(str) → seconds` per Section 2. Throws on unparseable input; never returns `NaN`.
- `formatTime(seconds, opts?) → string` per Section 2.
- `parsePaceRange("8:52-8:37 pace") → { slow, fast }` (strip the word "pace" and non-breaking spaces; the source file contains `\u00a0`).

### 4.2 Level for a single performance

```
levelForPerformance(event, timeSec) → { level, status }
event: '800' | '1600' | '3200' | '5k_xc'
```

Rule (default, see OPEN-1): the athlete qualifies for level L in that event if `timeSec ≤ benchmark(L, event)`. Return the **fastest level qualified for** (smallest L).

- Exact match counts: 1600 in 5:30 → level 16.
- Between benchmarks rounds toward the slower level: 1600 in 5:25 → level 16 (qualifies for 16, not for 15's 5:20).
- Slower than every level-19 benchmark → `{ level: 19, status: 'below_scale' }`.
- Faster than level 1 → `{ level: 1, status: 'above_scale' }`.
- Otherwise `status: 'in_scale'`.

### 4.3 Assigned level for an athlete

```
assignLevel(performances[], override?) → { level, source, perEvent }
```

1. If the coach has set an override level, return it with `source: 'coach_override'`. Overrides always win.
2. Otherwise compute `levelForPerformance` for the athlete's best eligible time in each event.
3. **Default (OPEN-2):** the assigned level is the fastest (smallest) level among **1600, 3200, and 5k XC**. The 800 is reported in `perEvent` but excluded from assignment, since speed-oriented runners can over-place on it. Eligible performances are those within the last 12 months (OPEN-3).
4. If the athlete has no eligible performances, return `{ level: null, source: 'no_data' }`. The UI should prompt the coach to set a level manually. Do not default to 19 silently.
5. Always return `perEvent` so the coach can see why a level was chosen.

### 4.4 Training prescription for a level

```
prescriptionForLevel(level) → {
  vdot, band, weeklyMiles,
  easyPace: { slow, fast }, tempoPace: { slow, fast }, longPace,
  intervals: ParsedInterval[4],
  easyRoutes: [{ routeId, time: { slow, fast } }],
  tempoRoute: { routeId, time: { slow, fast } },
  longRun: [{ routeId | distanceMi, timeSec }]
}
```

Pure lookup from Section 3. Throws if level is not an integer 1–19.

### 4.5 Route times for an arbitrary pace

```
routeTime(routeId, paceSecPerMile) → seconds = round(distanceMi × pace)
```

Used for coach-overridden paces and for custom workouts. For the published levels, this formula reproduces the Section 3.5 easy and tempo route times to within ±1 second. Use that as a **data-integrity test**, not as a replacement for the published values.

### 4.6 Resolving calendar workouts

Calendar entries store the **intent**, not the numbers. For example: `{ type: 'tempo', routeId: 't_15_mile_mark' }`, `{ type: 'interval', option: '400' }`, `{ type: 'easy' }`, `{ type: 'long' }`. Each athlete's view resolves the intent with `prescriptionForLevel(athlete.level)`, so one team calendar entry shows every athlete his own paces. If the calendar names a route that isn't in that athlete's level row (e.g. a level-19 athlete on the Guy West route), fall back to `routeTime(routeId, easyPace)` and label it "calculated" in the UI.

---

## 5. Integration with athlete data

**Stored (database):**
- Athlete performances: event, time in seconds, date, meet/course name, and whether it's a valid race result.
- Coach level override (nullable), with who set it, when, and an optional note (e.g. returning from injury).
- Optionally a cached assigned level, recomputed whenever performances or the override change.

**Derived at read time (never stored):** paces, interval prescriptions, route targets, weekly mileage target.

**Existing training groups (OPEN-8):** the app already groups athletes as Platinum, Gold, Silver, Bronze, Emerald, Aquamarine, and Ivory. The guide has five color bands. Group membership should be defined as level ranges in a single config file, filled in by the coach:

| Group | Levels (fastest–slowest) |
|---|---|
| Platinum | _TBD_ |
| Gold | _TBD_ |
| Silver | _TBD_ |
| Bronze | _TBD_ |
| Emerald | _TBD_ |
| Aquamarine | _TBD_ |
| Ivory | _TBD_ |

Do not invent this mapping. Until it's filled in, leave existing group logic untouched.

---

## 6. Test cases

These must pass before any UI work uses the module. Cases marked **(guide)** come from the worked examples in the guide's own explanation text.

| # | Call | Expected |
|---|---|---|
| 1 | `levelForPerformance('1600', 5:30)` | level 16, in_scale **(guide)** |
| 2 | `prescriptionForLevel(16)` event equivalents | 800 2:27, 3200 11:46, 5k XC 19:12 **(guide)** |
| 3 | `levelForPerformance('1600', 5:00)` | level 11 **(guide)** |
| 4 | `prescriptionForLevel(11).easyPace` | slow 7:30, fast 7:18 **(guide)** |
| 5 | `prescriptionForLevel(11).easyRoutes` | drinking_fountain 48:54–47:35; little_bridge 47:33–46:16 **(guide)** |
| 6 | `prescriptionForLevel(11).intervals` | includes 4–5 × 1000 @ 207 s and 9 × 400 @ 71 s **(guide)** |
| 7 | `prescriptionForLevel(11).tempoPace` | slow 6:12, fast 6:00 (guide: "about 6:00" for a 5:00 miler) |
| 8 | `levelForPerformance('1600', 5:25)` | level 16 |
| 9 | `levelForPerformance('1600', 5:31)` | level 17 |
| 10 | `levelForPerformance('1600', 6:10)` | level 19, below_scale |
| 11 | `levelForPerformance('1600', 4:05)` | level 1, above_scale |
| 12 | `levelForPerformance('5k_xc', 18:00)` | level 12 |
| 13 | `assignLevel([1600 5:20, 5k_xc 18:19])` | level 13 (5k qualifies for 13, faster than 1600's 15) |
| 14 | `assignLevel([800 2:13, 1600 5:40])` | level 17 (800 excluded by default) |
| 15 | `assignLevel([...], override = 14)` | level 14, source coach_override |
| 16 | `assignLevel([])` | level null, source no_data |
| 17 | `parseInterval('6-7x400@90')` | repsMin 6, repsMax 7, distanceM 400, repSec 90 |
| 18 | `parseInterval('12x200@37.5')` | repsMin 12, repsMax 12, distanceM 200, repSec 37.5 |
| 19 | `routeTime('drinking_fountain', 7:37)` | 49:39 ± 1 s |
| 20 | `routeTime('t_15_5_mile_mark', 5:50)` | 21:17 ± 1 s |
| 21 | Data integrity: every easy and tempo route time in 3.5 vs `routeTime` | within ± 1 s, except the known level-1 Over Guy West fast value |
| 22 | Data integrity: event benchmarks get strictly faster from level 19 to 1 in every event | passes |

---

## 7. Known data issues in the source spreadsheet

Transcribe these **as written**. Do not fix them in code. Surface them to the coach, and update this spec once he decides.

1. **Header cells corrupted by Excel.** Levels 12, 11, 10, 9, 7, 6, 5, 4, 3, and 2 have their "level/vDOT" header stored as dates (e.g. `12/58` became Dec 1958). The VDOT values in Section 3.1 were decoded from these and are correct.
2. **Level 18, 1k reps @ 3:44** is faster than levels 17 (3:50) and 16 (3:47). It is probably a typo (3:54?).
3. **Level 3, `5-6x5k@3:00`** is almost certainly `5-6x1k`. Its pace (3:00) also equals level 4's.
4. **Level 2, 1k @ 2:52** is faster than level 1's 2:54.
5. **Levels 18 and 17** have identical 800 (3:00) and 400 (86) reps.
6. **Rep count vs. pace trade-offs** (probably intentional): when the rep count goes up (level 12→11, 9→8), the 800/400/200 pace eases slightly (e.g. level 9 `5x800@2:31` → level 8 `6x800@2:33`). Do not "fix" these.
7. **Level 1, Over Guy West fast time 61:40** doesn't fit the route distance; 9.84 mi × 6:10 = 60:41. Probably should be 60:40.
8. **Long-run route distances differ from easy-run distances for the same name** (Drinking Fountain 6.50 vs 6.52, Little Bridge 6.30 vs 6.34, Cordova Park 7.10 vs 7.13, Howe 8.10 vs 8.14, Pump 8.00 vs 8.04, Guy West 9.40 vs 9.36, and Over Guy West 9.40 vs 9.84). The large Over Guy West gap suggests "Guy West" and "over Guy West" in the long-run row may mean the same route.
9. **Long-run pace vs. guide text.** The text says long runs should be 30–40 s/mile slower than easy, but the table's long pace sits at or near the slow end of the easy range. The table wins.
10. **Component percentages sum to 109–113%** (Section 3.6).
11. **Layout differences:** for levels 19–16 the tempo pace is one row lower than for other levels, and level 7's tempo time is inside the route cell. This matters only if someone re-imports the spreadsheet. Use the normalized tables in this spec.

---

## 8. Open decisions for the coach

Each has a default that the code uses until changed. Put each default in one config object so it's a one-line change.

| ID | Question | Default in v1 |
|---|---|---|
| OPEN-1 | Between two benchmarks, place the athlete at the slower level (conservative) or the nearer one? | Slower level |
| OPEN-2 | Which events determine the level, and how are they combined (fastest, 1600 only, most recent race)? | Fastest of 1600 / 3200 / 5k XC; 800 excluded |
| OPEN-3 | How old can a performance be and still count? Do track times count during XC season? | Last 12 months, all seasons |
| OPEN-4 | How should the component percentages be interpreted (does "easy 75%" include the long run)? | Display only, no enforcement |
| OPEN-5 | Interval recovery / send-off times for each level | Show only level 11's example (7:00 per 1k, 2:30 per 400) as a note |
| OPEN-6 | Confirm the back-calculated route distances in 3.4 | Use them for `routeTime` only |
| OPEN-7 | Display long-run targets as `85:00` (guide style) or `1:25:00`? | Guide style |
| OPEN-8 | Map the app's seven training groups to level ranges | Unmapped; existing group logic unchanged |
| OPEN-9 | Should 5k XC times be adjusted for course difficulty? | No adjustment |
| OPEN-10 | Resolve each item in Section 7 | Transcribe as written |

---

## 9. Instructions for Claude Code

1. Use plan mode first. Show the plan (files, data shape, function list) before writing code.
2. Transcribe Section 3 into the data file exactly. Then write a test that re-reads the markdown tables in this spec and checks the data file matches them, so transcription errors are caught.
3. Implement Section 4 as pure functions and write every test in Section 6. All must pass before any integration work.
4. Keep every OPEN default in one config object, referenced by ID in a comment.
5. If something in this spec is ambiguous or contradicts existing code, stop and ask. Don't pick an interpretation silently.
6. When the coach resolves an OPEN item or a Section 7 issue, update this spec first, then the code and tests.

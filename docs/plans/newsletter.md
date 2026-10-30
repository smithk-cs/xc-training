# Newsletter Spec

Audience assumption: **athletes and their families**. Use the strictest privacy defaults.
See `privacy.md`.

## 1. Principles

1. **Auto-draft, human-approve.** The app builds a draft weekly. A coach edits text, swaps
   photos, and approves. The app never auto-sends.
2. **Snapshot at generation.** Store the issue's data when it is created so past issues do
   not change when scores or results are corrected later.
3. **Section model.** Every section is a block that can be enabled, disabled, and reordered,
   with its own "no content this week" behavior.
4. **One template, three outputs:** web page in the app, email, and PDF.
5. **Statuses:** `draft -> ready_for_review -> approved -> sent`, recording who approved.
6. **Results entry:** CSV upload or form first. Automatic imports from results sites come
   later, only after confirming each site's terms.

## 2. Sections

Default order. Only the coach's note, spotlight, captains' corner, and thank-yous need fresh
human writing each week.

| # | `section_key` | Filled by | Empty behavior |
|---|---|---|---|
| 1 | `coach_note` | Coach writes (cap about 100 words) | skip |
| 2 | `week_ahead` | Auto from calendar: practices, weight-room sessions, races, events | skip |
| 3 | `race_logistics` | Auto from race event; coach confirms: departure and return times, what to bring, forecast | skip; show "forecast pending" until within weather-service range |
| 4 | `deadlines` | Coach enters once with a due date and a "show starting X days before" setting | skip |
| 5 | `race_recap` | Auto from results: team place and score, top 10 with times, course, conditions | skip |
| 6 | `prs` | Auto from results: athletes with a PR, with previous best | skip |
| 7 | `course_snapshot` | Auto from course records: distance, terrain note, last year's team result | for a new course, show "first time here" without history |
| 8 | `team_numbers` | Auto: team totals for miles, points, days run, plus progress to team goal. No individual names. | skip if no data |
| 9 | `streaks_vdot` | Auto: athletes on weekly mileage or points streaks; athletes who reached a new VDOT level. Alphabetical, milestone-based, consent-filtered. | skip |
| 10 | `spotlight` | Coach writes a few lines (cap about 60 words); app suggests who is next | skip |
| 11 | `captains_corner` | Captains write; coach approves | skip |
| 12 | `recovery_tip` | Rotates from an approved library | skip if library empty |
| 13 | `photos` | Coach or captains upload; consent flag required | skip |
| 14 | `thank_yous` | Coach writes | skip |

Weight-room sessions refer to strength training. Never include athletes' body weight.

### Rules by section

- **Streaks and VDOT:** not a leaderboard. Alphabetical order or milestone reached. Showing a
  numeric VDOT is optional and off by default; "moved up a fitness level" is enough. A streak
  is consecutive weeks at or above 80% of target; excused weeks pause rather than break it.
- **Team numbers:** a coach-set team goal per season or month, shown as a progress bar.
- **Spotlight:** track who has been featured so everyone gets a turn. Skip athletes without
  consent when suggesting.
- **Recovery tips:** a pool of 20-30 short, positive, general tips on sleep, hydration, and
  general fueling. No content about weight, dieting, or restricting food. Have a coach, school
  nurse, or athletic trainer review the library once.
- **Race recap tone:** show team place and times. No "bottom" lists or negative framing.
- **Race results:** even when public, apply the same opt-out settings in the newsletter.

## 3. Data model

| Table | Key fields |
|---|---|
| `issues` | `id`, `team_id`, `week_start`, `status`, `template_version`, `created_by`, `approved_by`, `approved_at`, `sent_at` |
| `issue_sections` | `issue_id`, `section_key`, `enabled`, `position`, `coach_text` (editable), `data_snapshot` (JSON, generated) |
| `send_log` | `issue_id`, `recipient_group`, `sent_at`, `sent_by` |
| `results_imports` | `id`, `race_id`, `uploaded_by`, `status`, `row_count`, `error_rows` |
| `tips_library` | `id`, `text`, `category`, `last_used`, `approved_by` |
| `spotlights` | `athlete_id`, `issue_id` |
| `deadlines` | `id`, `title`, `due_date`, `show_from_days_before` |

`coach_text` and `data_snapshot` are separate so regenerating refreshes numbers without
wiping the coach's edits.

## 4. Section registry

```python
SectionSpec(
    key="race_recap",
    build=build_race_recap,       # (week_ctx) -> dict | None
    empty_behavior="skip",        # or "placeholder"
    needs_coach_text=False,
)
```

When `build` returns `None`, the block is skipped or shows its placeholder. A week without a
race drops `race_recap`, `prs`, and `course_snapshot` automatically.

## 5. Workflow and status rules

- Only a coach role can approve. Captains can submit Captains' Corner text only.
- Editing an approved issue returns it to `draft` and clears `approved_by` and `approved_at`.
- Sent issues are read-only. Corrections go in a short note in the next issue.
- Sending is a manual action after approval and writes to `send_log`.
- A "Refresh data" action re-runs section builders into a new snapshot and keeps coach text.

## 6. Consent filtering

Apply at snapshot time and again at render time.

- Each athlete has display settings for name, photo, spotlight, and VDOT display.
- An athlete without consent is omitted from names, photos, spotlight suggestions, streaks,
  PRs, and recap lists in the newsletter (aggregate totals still count them).
- Photos are tagged with the athletes shown. A photo cannot appear if any tagged athlete lacks
  photo consent. Strip EXIF location data at upload. Require alt text.

## 7. Outputs

- **Web:** page in the app.
- **Email:** table-based layout, inline styles, about 600 px wide, absolute image URLs, alt
  text on every image, plus a plain-text version. MJML is a good option for compiling.
- **PDF:** render the web page with a print stylesheet via headless Chromium or WeasyPrint.
- Keep `template_version` on each issue so old issues render as sent.
- The family email list needs opt-in and an unsubscribe link; use an email service provider.

## 8. Results CSV import

| Column | Notes |
|---|---|
| `race_date`, `race_name`, `course_id`, `distance` | one race per file, or include a race column |
| `athlete` | match by ID where possible, otherwise by name |
| `place`, `time` | time as `mm:ss.s` |
| `team`, `team_place`, `team_score` | optional, for team recap |

Flow: upload, preview with validation, confirm. Flag unmatched names, unparseable times, and
duplicate rows; let the coach fix them before saving. After import, run PR detection against
history for that distance and conditions-adjusted course comparisons.

## 9. Tests

- Week with no race skips recap, PRs, and course snapshot.
- Athlete without consent never appears in names, photos, or spotlight.
- Editing an approved issue returns it to draft and clears the approver.
- Regenerating a draft keeps coach text.
- Correcting a race result does not change a sent or approved issue.
- Email HTML has no external CSS and stays at or under 600 px wide.
- A CSV with a bad time or an unknown athlete is flagged, not silently imported.
- Sending requires `approved` status and a coach role.
- Team numbers contain no individual names.

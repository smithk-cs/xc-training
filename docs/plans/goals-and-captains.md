# Goals and Captain Posts Spec

Core principle: AI interprets, code judges.

## 1. Athlete weekly goals

### Flow

1. Athlete enters a goal in plain words, or picks from a short menu.
2. AI translates the text into a **structured check** drawn from an allowed list (below).
3. The app shows the interpretation back in plain language. Example:
   "I'll check: 4 runs of at least 30 minutes, with at least 3 in your Steady zone. Is that right?"
4. Athlete confirms (or edits). Only confirmed goals are saved.
5. At week end, **code** evaluates the check against workout data. AI plays no role in
   deciding met or not met.
6. For goals the data cannot measure ("sleep better", "feel stronger on hills"), the app
   asks a simple yes/no check-in instead of guessing from numbers.

### Allowed check types

| `check_type` | Parameters | Evaluated from |
|---|---|---|
| `total_miles` | minimum | workouts |
| `days_run` | minimum days, minimum minutes per day | workouts |
| `total_points` | minimum | scoring engine |
| `zone_minutes` | zone, minimum minutes | workouts |
| `pace_target` | distance, target time or pace | workouts |
| `race_pr_attempt` | race, distance | race results |
| `yes_no_checkin` | prompt text | athlete answer |

AI output must validate against this schema. If the text cannot be mapped, ask the athlete
to pick a type or use `yes_no_checkin`. Never store unvalidated AI output as a check.

### Guardrails

- If a mileage or points goal exceeds about 110% of the coach's plan, show a gentle nudge
  toward the plan before saving. Allow saving after the nudge.
- Goals about weight, appearance, or dieting are not tracked. Show a short supportive message
  and route the athlete to the coach (and notify the coach, without quoting the goal text
  to other athletes).
- Results are shown as "Met" or "Not yet" with no commentary on a miss. Offer an
  "adjust for next week" button on a miss.
- Goals are private to the athlete and coaches by default.

### Data model

`goals`: `id`, `athlete_id`, `week_start`, `raw_text`, `check_type`, `check_params` (JSON),
`confirmed_at`, `status` (active, met, not_met, cancelled), `evaluated_at`,
`evaluation_detail` (JSON with the numbers used).

## 2. Captain-pushed goals

### What captains can post

- **Daily practice focus:** a short message for that day, from a template list. Examples:
  "Hold Steady pace on the easy run", "Finish the last rep strong", "Everyone logs their
  warm-up."
- **Weekly team challenge:** a shared target, such as combined team points or combined days
  run for the week.

### What captains cannot post

- Individual mileage, pace, or points targets (that is the coach's plan).
- Anything directed at a single teammate.
- Free-form text without review (see moderation).

### Moderation

- Coach approval before a post goes live, or a short review window set by the coach
  (config). Coaches can remove any post at any time.
- Character limit (default 140).
- Posts expire automatically at end of day (daily focus) or end of week (challenge).
- Captain role is limited to creating posts and submitting Captains' Corner text for the
  newsletter. Captains cannot approve anything.

### Results

- Team goals are measured from data and reported as group results only. Example: "78% of the
  team stayed in the Steady zone on the easy run."
- Never list individuals who missed a team goal.

### Data model

`team_posts`: `id`, `team_id`, `type` (daily_focus, team_challenge), `template_key`,
`text`, `created_by`, `approved_by`, `approved_at`, `starts_at`, `expires_at`, `check_type`,
`check_params`, `status` (pending, approved, rejected, expired), `result_summary` (JSON).

## 3. Tests

- AI-style output with an unknown `check_type` is rejected, not saved.
- A goal is only evaluated after confirmation.
- A goal evaluation changes only when underlying data changes; AI is never called during
  evaluation (assert by mocking the AI client to fail).
- Goals about weight or dieting are not stored as trackable goals and notify the coach.
- A captain cannot approve their own post or any other post.
- Daily focus expires at end of day; team challenge at end of week.
- Team result summaries contain no individual names.

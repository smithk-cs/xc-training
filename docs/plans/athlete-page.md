# Athlete Page Spec

Design rule: the home screen stays quiet. It shows only what is new or in progress.

## 1. Home screen: four elements, nothing else

1. **Weekly mileage tracker** (existing; keep as is).
2. **Weekly points tracker** (new), placed beside the mileage tracker with the same style of
   progress bar so they read as a pair.
3. **Message card**: one latest daily or weekly message. If none, show nothing (no empty box).
4. **Tag row**: tags earned in the last 7 days, maximum 3 shown (newest first).

Everything else (history, trend lines, past tags, goals archive, VDOT history, course PR
history) goes behind a single **History** link. Do not add new elements to the home screen
without removing one.

Optional element that is allowed only when active: the **goal card** (see
`goals-and-captains.md`) and the **captain focus banner** for today. Both replace the message
card slot or sit directly under it, and expire automatically.

## 2. Tags

Awarded automatically by rules. Coaches can hide or remove a tag but do not need to manage
them.

| Tier | Tag | Rule |
|---|---|---|
| Great | Race PR | New best at a standard race distance. Shows previous best. |
| Great | Course Record | Best conditions-adjusted time on a repeated course, by a meaningful margin (config, PLACEHOLDER 1%). |
| Great | Best Mile Pace | Fastest mile split in a workout. Excludes treadmill and mostly-downhill miles. Labeled differently from Race PR. |
| Good | Course Improvement | Faster than the athlete's recent average on the same course and distance, conditions-adjusted. |
| Good | Full Week | Hit 100% of both mileage and points targets in the same week, without exceeding the 110% cap. |
| Good | Welcome Back | First full-plan week after a layoff of 6+ days. |
| Good | Zone Control | At least 90% of planned quality-workout minutes in the correct zone (config). |

Rules:

- A repeated tag type has a cooldown (default once per course per 30 days; once per week for
  weekly tags) so tags stay meaningful.
- Never tag weekly volume above plan.
- Use conditions-adjusted times for course tags.
- Tags never compare athletes to each other.
- Every tag stores `rule_id`, `rule_version`, and the source data, so it can be explained.

## 3. Messages

Rules choose the highlight; templates produce the text. AI may reword a template only if a
coach has approved the style. Never generate free text about an individual with AI.

- **Daily:** at most one message, and only when something good happened. Example template:
  "Your threshold run was on pace for {minutes_in_zone} of {minutes_planned} minutes."
  On ordinary days show nothing.
- **Weekly (Sunday):** one highlight and one forward-looking line. Example: "Next week's
  goal: {next_target} points."

Constraints for all messages:

- Positive framing only; no mention of missed days, low scores, or comparison to teammates.
- Never mention body weight, appearance, or food.
- Use only data the athlete consented to display.
- Template library lives in the repo with unit tests that render every template with sample
  data.

## 4. Points tracker display

- Show points earned vs weekly target as a progress bar and a simple number ("52 of 76").
- Cap the bar at 100%. If the athlete is at the 110% limit, show "Target met" rather than more.
- Tapping it opens the History view with the day-by-day breakdown.

## 5. Tests

- Home screen renders at most four elements (plus goal card and captain banner when active).
- Tag row shows at most 3 and only tags from the last 7 days.
- No message renders when there is no qualifying event.
- Tag cooldown prevents duplicate tags within the window.
- Treadmill and downhill-heavy miles do not produce Best Mile Pace.
- Athlete without consent for a display field never sees that field leak into shared views.

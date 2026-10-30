# Privacy and Safety Spec

The athletes are minors. Treat every feature that displays a name, photo, time, or health-
adjacent number as a privacy decision.

Before launch, confirm the school or district's policies on naming minors, photos, and
sharing performance data, and any applicable law. This spec sets conservative defaults;
adjust only to what the school approves.

## 1. Consent flags (per athlete)

| Flag | Controls | Default |
|---|---|---|
| `show_name` | name in newsletter, tags visible to others, team feeds | off until set |
| `show_photo` | appearing in newsletter or team photo areas | off until set |
| `show_spotlight` | eligibility for athlete spotlight | off until set |
| `show_vdot` | numeric VDOT display in shared views | off |
| `show_times` | race times in newsletter lists | follow `show_name` |

- Consent is recorded with who set it (athlete, parent or guardian, coach), when, and how.
- Defaults are off. Missing consent means "not allowed".
- Honor changes immediately. Past sent issues cannot be recalled, so say so in the consent form.

## 2. Hard rules

1. No body weight, body composition, or related metrics. Do not collect or display them.
2. No content about dieting, restricting food, or weight loss. Messages, tips, and goals
   that touch these topics are blocked and routed to the coach.
3. No public leaderboards by mileage or points. Compare athletes only to their own history
   or targets. Team totals are fine.
4. No negative or shaming lists (worst times, missed targets, bottom finishers).
5. Positive framing in all family-facing and athlete-facing text.
6. Athletes' goals and check-ins are private to the athlete and coaches.

## 3. Photos

- Tag every athlete in a photo at upload; block use if any tagged athlete lacks photo consent.
- Strip EXIF metadata (including GPS location) at upload.
- Require alt text.
- Only coaches and approved captains can upload; coaches can remove any photo.

## 4. Roles

| Role | Can do |
|---|---|
| Athlete | view own data, set own goals, set own display preferences where allowed |
| Captain | post approved templates for daily focus and team challenges, submit Captains' Corner text |
| Coach | everything above plus approve posts and newsletters, set plans and targets, manage consent flags, send newsletters |
| Admin | manage users and roles |

Captains never see other athletes' individual workout data, goals, or consent details.

## 5. Data handling

- Garmin and workout data are visible only to the athlete and coaches by default.
- Audit log for: consent changes, newsletter approvals and sends, role changes, data
  exports, and deletions.
- Provide a way to delete an athlete's data when they leave the team, following school policy.
- Email list: opt-in with an unsubscribe link; store only what is needed.
- Weather and location lookups use run start coordinates only for conditions; do not expose
  home or start locations in shared views.

## 6. Tests

- Athlete without a consent flag never appears in the corresponding shared output.
- Photo with any non-consenting tagged athlete is blocked.
- EXIF location is stripped on upload.
- Messages and tips containing blocked topics (weight, dieting) fail validation.
- Captain role cannot read individual athlete data or approve content.
- Consent changes are logged and take effect on the next render.

# Team Fitness App

Web app for a high school running team. Athletes' Garmin data produces a weekly
fitness score based on Jack Daniels' VDOT system (*Daniels' Running Formula*, 4th ed.).
Coaches, team captains, and athletes each have their own views. A weekly newsletter is
auto-drafted from team data, then approved by a coach before it goes to families.

Full design lives in `docs/spec/`. Read the relevant file before working on that area.
Progress and phases live in `docs/BUILD_PLAN.md` (do not put plans or checklists in this file).

## Stack and commands

<!-- Fill these in once; Claude Code will keep them current. -->
- Language / framework: TODO
- Database: TODO
- Run tests: TODO
- Run app locally: TODO
- Lint / format: TODO

## Non-negotiable rules

These apply to every change. Encode them as tests, not just reminders.

1. **Athletes are minors.** Respect per-athlete consent flags for name, photo, spotlight,
   and VDOT display everywhere (app, newsletter, exports). See `docs/spec/privacy.md`.
2. **Never store, collect, or display body weight**, or any metric derived from it.
3. **Newsletters are never sent without coach approval.** Sending is always a manual,
   logged action after approval. See `docs/spec/newsletter.md`.
4. **Scores are derived, not stored as truth.** Keep raw workouts, plans, and VDOT history
   as source records. Compute scores with pure, re-runnable functions. Every score carries
   a `score_version`. Never mutate history to "fix" a formula; bump the version.
5. **Snapshot the VDOT and conditions used for each workout** so past scores stay explainable.
6. **No individual leaderboards ranked by mileage or points.** Show progress against the
   athlete's own target or history. Team totals are fine.
7. **Positive framing in all athlete-facing and family-facing text.** No shaming, no
   "worst" lists, no comparisons between named athletes on volume.
8. **AI never judges outcomes.** AI may translate free-text goals into a structured check
   and may reword approved templates. Code evaluates data and decides met / not met.
9. **Daniels' tables are the source of truth** for pace zones, layoff adjustments, altitude,
   and temperature. Values marked PLACEHOLDER in the spec are not from the book and must
   stay in config so coaches can change them.

## Conventions

- Keep tunable numbers (weights, rates, caps, thresholds) in one config module, not inline.
- Zone codes: `S` Steady, `M` Moderate, `T` Threshold, `I` Interval, `R` Repetition.
- Time values are stored in seconds, distances in meters, pace in seconds per mile
  (display units are a separate concern).
- Every new scoring or newsletter behavior needs a test fixture before the implementation.
- Prefer small, reviewable commits. One phase of `BUILD_PLAN.md` per branch.

## Spec index

- `docs/spec/scoring.md` : weekly points, daily cap, weekly score, bonuses, layoffs
- `docs/spec/conditions-and-courses.md` : temperature, dew point, altitude, course factor
- `docs/spec/athlete-page.md` : athlete home screen, tags, messages
- `docs/spec/goals-and-captains.md` : athlete goals, captain-posted focus and team challenges
- `docs/spec/newsletter.md` : sections, statuses, snapshots, outputs, results import
- `docs/spec/privacy.md` : consent, minors, roles, audit

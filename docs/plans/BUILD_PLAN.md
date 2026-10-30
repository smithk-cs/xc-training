# Build Plan

Work one phase per branch. Write the test fixtures listed in each spec before the
implementation. Update the checkboxes as work completes.

## Open decisions (resolve early)

- [ ] **Second zone name.** Using "Moderate" (code `M`) unless changed. Display label only.
- [ ] **Newsletter audience.** Assumed athletes and families (strict privacy defaults).
- [ ] **Consent policy.** Confirm school or district rules on naming minors and photos.
- [ ] **Tech stack** and test commands (fill in `CLAUDE.md`).
- [ ] **Humidity / dew point tiers.** Not in Daniels; need coach or athletic trainer input.
- [ ] **Course difficulty starting values** (tiers, surfaces). Placeholders until calibrated.
- [ ] **Email provider** for the family list (opt-in, unsubscribe).
- [ ] **Confirm Daniels details** marked VERIFY in `docs/spec/scoring.md` (zone bounds,
      workout caps) against the book.

## Phase 0: Foundation

- [ ] Fill in stack, commands, and conventions in `CLAUDE.md`
- [ ] Config module for all tunable numbers (weights, rates, caps, thresholds)
- [ ] Data model: athletes, plans, workouts (raw Garmin records), VDOT history, consent flags
- [ ] Role model: athlete, captain, coach, admin
- [ ] Test harness and fixtures folder

Acceptance: tests run in CI; consent and role checks exist as reusable helpers.

## Phase 1: Scoring engine (`docs/spec/scoring.md`)

- [ ] VDOT calculation and zone pace computation, validated against the book's tables
- [ ] Zone classification with smoothing, moving-time filter, midpoint cutoffs
- [ ] Daily cap, weekly score, days component, 110% weekly cap
- [ ] Excused days and layoff adjustments (Tables 9.1 and 9.2)
- [ ] Coach warnings (workout caps, week-over-week jumps, easy-day pace)
- [ ] Bonus layer: race PR, VDOT gain, course improvement (basic)
- [ ] `score_version` and VDOT snapshot per workout
- [ ] All scoring fixtures pass

Acceptance: a perfect week scores 100 before bonus; too-fast easy days are capped and flagged.

## Phase 2: Athlete page (`docs/spec/athlete-page.md`)

- [ ] Mileage tracker (keep) plus points tracker
- [ ] Message card with template library and rules
- [ ] Tag rules, cooldowns, tag row
- [ ] History view behind a single link
- [ ] Layout test: at most four home elements

## Phase 3: Conditions and courses (`docs/spec/conditions-and-courses.md`)

- [ ] Weather lookup by run time and GPS; store conditions snapshot per workout
- [ ] Temperature and altitude factors from Daniels' tables
- [ ] Dew point tiers (config, default no adjustment)
- [ ] Course records, matching, computed difficulty tiers, coach tags
- [ ] Self-calibration with minimum observations and blending
- [ ] Feed factors into zone classification and the course improvement bonus

## Phase 4: Goals and captain posts (`docs/spec/goals-and-captains.md`)

- [ ] Structured goal schema, confirmation flow, code-based evaluation
- [ ] AI translation behind a schema validator (AI never evaluates)
- [ ] Blocked-topic routing to coach
- [ ] Captain posts with templates, moderation, expiry
- [ ] Team challenge evaluation with group-only results

## Phase 5: Newsletter (`docs/spec/newsletter.md`)

- [ ] Section registry and issue model with statuses and approval
- [ ] CSV results import with preview and validation
- [ ] Core sections: week ahead, race recap, PRs, streaks and VDOT, team numbers
- [ ] Coach-written sections: coach note, spotlight, thank-yous, captains' corner flow
- [ ] Logistics, deadlines, course snapshot, recovery tips library
- [ ] Photos with consent flags and EXIF stripping
- [ ] Web, email (600 px, table layout), and PDF renderers
- [ ] Send flow with send log and manual trigger
- [ ] Newsletter test list passes

## Phase 6: Polish and rollout

- [ ] Coach onboarding screens (plan entry, consent management)
- [ ] Pilot with one group; review tags, messages, and scores with coaches
- [ ] Revisit placeholder values using real data; bump `score_version` as needed

## Later ideas (not scheduled)

- Fast Repetition and 10K-pace zones
- Per-segment daily caps
- Automatic results imports (check site terms first)
- Heart rate as context only: measured max HR, resting HR trend flag, easy-day check
- Age-adjusted VDOT views
- Daily readiness check-in (1-5)

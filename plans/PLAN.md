# XC Training: project plan

Jesuit Cross Country. Last updated October 25, 2026.

## Goal

Every athlete's Garmin runs arrive automatically and are analyzed against the coaches' benchmarks, goals and
training groups: heart rate rep by rep, training load against injuries, athlete effort and pain, and a nightly AI
briefing, so the staff can adjust training before athletes get hurt.

## Where things stand

| Area | Status |
|---|---|
| App (code) | Built. Sync, analysis, dashboard, charts, AI briefing, injuries, effort, missed days, training groups. |
| Validation | 47 automated tests and 41 end-to-end checks pass, including all 24 chart combinations in a headless browser. |
| Design | Jesuit red and gold; phone mockups of every athlete screen on the design canvas. |
| Garmin developer program | **Approved.** Credentials not yet added to the app. |
| Garmin notification format | **Not yet verified** against Garmin's documentation (needs the Activity API docs). |
| Hosting | **Not started.** Needs a server and an https address from school IT. |
| School approvals | **Not started.** Athletic director / district data approval; communications office permission for name and logo. |
| Claude API account | **Not started.** Decide who owns it and pays. |
| Logo | Placeholder. Waiting on the approved file. |
| Training groups | Boys' groups and VDOT targets set from the VDOT JHS sheet. Girls' cutoffs still to confirm. |

## Phases

### Phase 1: get ready (now to mid-November)
- [ ] Upload Garmin's Activity API notification docs; check `xcapp/garmin.py` against them (see `plan-garmin-go-live.md`)
- [ ] Ask the AD who approves student-data tools; start that approval (see `plan-team-rollout.md`)
- [ ] Send the communications office the permission request for the school name and logo
- [ ] Ask school IT for a small server and an address such as `xc.jesuithighschool.org`
- [ ] Decide who owns the Claude API account and billing (booster club is common)
- [ ] Confirm girls' training-group cutoffs; send the easy-pace column and VDOT rows below 48

### Phase 2: go live with staff (after hosting is up, about 1-2 weeks)
- [ ] Deploy with Docker; run `python -m xcapp.cli check`
- [ ] Add Garmin credentials and notification endpoints
- [ ] Coach connects his own watch, runs, and checks the Sync log and athlete page
- [ ] Import last season's results and this season's goals
- [ ] Look at every page on a real phone (visual check not possible in testing)

### Phase 3: pilot (winter, 2-4 weeks)
- [ ] Assistants plus 3-5 athletes connect (with parent consent)
- [ ] Plan workouts daily; athletes add effort and missed days; trainer logs injuries
- [ ] Read the AI briefing each morning; note anything wrong or unhelpful
- [ ] Fix issues, then decide on full rollout timing

### Phase 4: full team (by early May 2027)
- [ ] Parent consent forms out and back
- [ ] Download every athlete's link; mail-merge to athletes and parents
- [ ] Connect night at practice; chase anyone not connected
- [ ] At least four weeks of history before the June build, so load ratios are meaningful by summer

### Phase 5: summer build and camp (June to August 2027)
- [ ] Watch load ratios through the July-to-August ramp and camp week; the sample data showed most risk there
- [ ] Time trials for 1 mile, 1.5 mile and 2 mile on a fixed course; groups update automatically

### Phase 6: 2027 cross country season
- [ ] Daily routine from `TEAM_SETUP.md`
- [ ] End-of-season review: injuries vs load, group changes, what the staff actually used

## Decisions still open

| Decision | Who | Notes |
|---|---|---|
| Girls' training-group cutoffs | Coaches | With boys' cutoffs, 16 of 28 sample girls land in Ivory. |
| Who logs injuries | Coaches / trainer | App supports a trainer role (private notes) and coach logging. |
| Show athletes their group and VDOT? | Coaches | On by default; one switch on the Groups page. |
| Who owns the Claude API account | AD / booster club | Billing owner. |
| Data retention after graduation | AD / district | Admin can delete an athlete and all their data. |
| Hosting location and address | School IT | Needs https. |

## Related plans

- `plan-garmin-go-live.md`: technical steps to switch on Garmin sync and verify it
- `plan-team-rollout.md`: approvals, consent, onboarding and communication
- `plan-development.md`: remaining development work and ideas, with file references

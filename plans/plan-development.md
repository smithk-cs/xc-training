# Plan: remaining development

Run the checks after any change: `pip install -r requirements-dev.txt && pytest -q`, then
`cd scripts && npm install && python e2e.py`.

## Needed before go-live

| # | Task | Files | Blocked on |
|---|---|---|---|
| 1 | Verify Garmin notification fields against Garmin's docs; adjust and update tests | `xcapp/garmin.py`, `tests/test_garmin.py` | Activity API docs |
| 2 | Build and run the Docker image once (not possible in the build environment) | `Dockerfile`, `docker-compose.yml` | A server |
| 3 | Visual check of every page on real phones and a laptop, light and dark mode | `xcapp/templates/`, `xcapp/static/app.css` | Hosting |
| 4 | Add the approved logo | `TEAM_LOGO_FILE` in `.env` | Communications office |

## Data from the coaches (no code)

| # | Item | Where it goes |
|---|---|---|
| 5 | Girls' training-group cutoffs | Groups page |
| 6 | VDOT target rows below 48 | Groups page, target table |
| 7 | Easy-pace column from the VDOT JHS sheet | Needs a small feature (item 8) |

## Small features

| # | Task | Files |
|---|---|---|
| 8 | Easy-pace range per training group, shown to athletes with their targets | `xcapp/models.py` (TrainingGroup), `xcapp/web.py` (groups), `templates/groups.html`, `templates/athlete.html` |
| 9 | Update the published dashboard prototype to the current page (missed-day watch list reasons) | `xcapp/static/dashboard_body.html` |
| 10 | Optional email to coaches when the watch list changes | `xcapp/cli.py` (nightly), new mail settings |

## Ideas for later

- Send planned workouts to athletes' watches with Garmin's Training API (available on the Starter tier), so reps
  are recorded as structured laps and match the plan exactly
- Course difficulty adjustment for cross country 5K times (XC Stats does this; the app compares same-course times)
- Separate easy-pace and workout-pace targets per group derived from VDOT

## Known limits (by design)

- Load ratio is a screening signal, not a diagnosis; thresholds in `xcapp/analysis.py` and the dashboard page
- Manual-lap rep detection expects repeats of similar distance; ladders should be structured watch workouts
- VDOT is never calculated from 800s (the formula is not meant for races that short); 800 targets come from the sheet
- Athlete injury reports do not change injured weeks or load ratios until a trainer adds them to the injury log

# XC Training app: notes for Claude sessions

Training app for the Jesuit High School (Sacramento) cross country team, live at https://jesuitxc.org. The head coach
is a novice programmer: explain steps in plain words before running them, and report results briefly.

## Rules
- This repo is public. Never commit keys, passwords, real student names, or staff/student email addresses.
  Real student data stays out of the repo (scratchpad only). Test data uses made-up names.
- Secrets live in GitHub Secrets (see DEPLOY.md); non-secret settings in `wrangler.jsonc` `vars`; settings a coach
  changes in the app live in the database (`AppSetting`, via `analysis.get_setting` / `set_setting`).
- Commit as the repo's configured identity (smithk-cs). If a hook says commits are "Unverified", that is expected:
  don't rewrite or force-push.
- Before pushing: `git fetch origin main` and check for a coworker's new commits; rebase onto them if needed.

## Stack and layout
- FastAPI + SQLAlchemy + Jinja, Python 3.13. Runs as a Cloudflare Container behind a Worker (`cloudflare/`).
  Database: Supabase Postgres in production, SQLite locally and in most tests.
- Every push to `main` deploys (GitHub Actions `deploy.yml`: tests, Postgres tests, Worker tests, then deploy;
  ~25-30 min). Watch with `gh run list --workflow deploy.yml`.
- `xcapp/web.py` builds the app; features live in their own modules with a `*_web.py` that has
  `register(app, h)` (h = helpers passed in from `create_app`), e.g. `time_trials.py` + `time_trials_web.py`.
  The athlete's own pages (`/a/{token}`) are in `athlete_web.py`.
- New tables: add a model in `models.py` (created on start). New columns on existing tables: also add them to
  `ADDED_COLUMNS` in `db.py`.
- New staff pages must belong to a menu section: add it to `sections.py` and the grouped menu in
  `templates/base.html` (a test fails otherwise).
- Page query budgets are tested (`tests/test_query_budget.py`): avoid a query per athlete/run; batch or preload.

## Testing
- `.venv/bin/python -m pytest -q -p no:cacheprovider` (whole suite ~8 min; run the relevant files while working).
- Postgres: `TEST_DATABASE_URL=postgresql+psycopg://...` with a local Postgres (CI does this too).
- End to end: `cd scripts && ../.venv/bin/python e2e.py` (36 checks). Worker: `npm run test:worker`.
- Outside network access is limited in cloud sessions; tests stand in for weather, Garmin, email and USGS.

## Working with the coach (keeping sessions quick and low-cost)
- Batch related changes into one round of tests and one deploy.
- Match the checking to the change: quick fixes (wording, labels) need the relevant tests only, no screenshots;
  new features get the full suite, e2e, and a phone-size screenshot.
- Ask only when a decision is truly the coach's; otherwise pick the sensible option and say so.
- Keep replies short: what changed, where to find it, anything the coach must do. Say when a deploy is live.

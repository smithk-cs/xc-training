# XC Training

Garmin-synced training analysis for a high school cross country team: workouts arrive automatically after each
athlete's watch syncs, every rep is matched to the coach's plan with heart rate and recovery, results are tracked
against benchmarks and goals, injuries are linked to training load, and Claude writes a nightly coaching briefing.

For getting athletes connected and using the charts, read **TEAM_SETUP.md**.

## What it does

| Area | What you get |
|---|---|
| Garmin sync | Athletes connect once from a personal link (OAuth 2.0 PKCE, per Garmin's published spec). Runs, laps and heart rate arrive by webhook. GPS routes are never stored. Athletes without Garmin can upload `.fit` files. |
| Workouts | Plan the day's workout (e.g. 6 x 1K @ 5K pace). Synced runs that day are matched automatically; each athlete gets a target pace from their own benchmark times. Reps are found from structured watch workouts or manual lap presses. |
| Heart rate by rep | Pace vs target, average/max heart rate per rep, and how far heart rate drops in the first 60 s of rest, compared with the athlete's previous session at the same rep distance. |
| Benchmarks and goals | 1 mile, 1.5 mile, 2 mile and 5K (track 1600 m / 3200 m converted). Season-over-season change and gap to goal. CSV import from XC Stats or Athletic.net exports. |
| Charts | Career progression, compare up to six athletes, improvement vs last season, top-7 team average. On each athlete's own page and the coach's athlete page: one chart with two dropdowns, chart type (miles, load ratio, effort, heart rate, pace, benchmark times) and time range (past week, past month, this season, all years), with injuries and pain reports marked. |
| My runs (athletes) | For every synced run, athletes add effort (0-10), how their legs felt, any pain (where and how much) and a private note for coaches. Pain of 4/10 or more reaches the watch list; effort x minutes becomes a weekly effort load the AI briefing reads. |
| Days without a run | My runs lists practice days (Monday to Saturday) with no run and asks why: injury, sick, planned rest day, school or family, or other. For an injury the athlete gives where, which side, pain (1-10), when it started, a description, who has looked at it, and whether they expect to miss more days. Injury and sickness reports reach the watch list and the Injuries page, where a trainer adds an injury report to the injury log in one click. The AI gets the structured answers, never the description. |
| Run type and workout | Athletes (on their run page) and coaches (on the coach run page) set the type of run (easy, long, workout, race, recovery, other) and which planned team workout it was, from workouts within three days of the run. A choice made by hand is kept: automatic matching never overwrites it. |
| Training groups | Jesuit's groups: Platinum (VDOT 73+), Gold (68-72), Silver (62-67), Bronze (58-61), Emerald (54-57), Aquamarine (49-53) and Ivory (48 and below), in the team sheet's colors, with each athlete's 800 and 1600 targets from the team's VDOT table. Rename, recolor, add or remove groups and edit the table on the Groups page. VDOT is worked out from each athlete's best 1 mile, 1.5 mile, 2 mile or 5K in the look-back window (365 days by default) and read on the team table's scale: a result counts as VDOT 60 if its 1600-equivalent reaches the table's 4:57. Separate boys' and girls' cutoffs are supported. Coaches can pin an athlete to a group; planned workouts can target a group; athletes see their group unless you switch that off. Garmin's watch VO2max is not shared through Garmin's developer API, so the estimate comes from results. |
| Look | Jesuit red and gold, team name from `TEAM_NAME`, logo uploaded on the Staff page (or `TEAM_LOGO_FILE`), fonts stored in the app (SIL Open Font License), phone-first athlete pages, light and dark mode. |
| Injuries and load | Injury log (trainer-only notes), athlete pain reports, weekly load ratio (this week / previous 4-week average), injury rate by load ratio, and the peak load before each injury. |
| Watch list | Anyone above a 1.3 load ratio, injured, recovering slowly between reps, or reporting pain of 4/10 or more this week. |
| AI briefing | Each night Claude reads the team's numbers (athletes sent as codes, never names; injury notes never sent) and writes 4 to 8 prioritized findings. Coaches can also ask questions. Replies are checked before they are shown. |
| Staff roles | Admin, coach, athletic trainer. Sign-in rate limiting, CSRF protection, encrypted Garmin tokens, strict security headers. |

## Try it with the sample team (about 5 minutes)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m xcapp.cli gen-keys            # copy the lines into your shell or a .env file
export SECRET_KEY=... TOKEN_KEY=... GARMIN_WEBHOOK_SECRET=...
export SECURE_COOKIES=false AS_OF_DATE=2026-10-25 BASE_URL=http://localhost:8000
python -m xcapp.cli create-user coach --role admin
python -m xcapp.cli load-demo           # 62 invented athletes, about 7,800 runs
uvicorn --factory xcapp.web:create_app
```
Open http://localhost:8000 and sign in.

## Run it for real

1. **Server and address.** Any Linux server with Docker and a DNS name pointed at it (school IT can usually
   provide a small virtual machine). Garmin requires HTTPS; the included Caddy proxy gets a certificate automatically.
2. **Settings.** `cp .env.example .env`, fill it in (`python -m xcapp.cli gen-keys` makes the secrets). Never change
   `TOKEN_KEY` after athletes connect: it encrypts their Garmin tokens.
3. **Start.** `docker compose up -d`, then `docker compose exec web python -m xcapp.cli create-user yourname --role admin`.
4. **Garmin.** Once Garmin approves your Connect Developer Program app, add `GARMIN_CLIENT_ID` and
   `GARMIN_CLIENT_SECRET`, restart, and paste the webhook address shown on the home page (admins only) into Garmin's
   portal for activity, activity-file, deregistration and user-permission notifications.
5. **AI.** Add `ANTHROPIC_API_KEY` (from the Claude Console). The `scheduler` container writes the briefing every
   night at `NIGHTLY_HOUR`.
6. **Check.** `docker compose exec web python -m xcapp.cli check`.

Backups: the database is one file in the `xcdata` volume (`/data/xc.db`); copy it nightly. For Postgres, set
`DATABASE_URL` and add `psycopg[binary]` to requirements.

## How it was validated

* `pytest` (47 tests). Real Garmin `.FIT` files are generated and decoded with Garmin's official FIT SDK; Garmin
  OAuth, token refresh and every notification type are exercised against a mock Garmin; the AI layer is checked for
  de-identification, reply validation, retries and failures; the web app for sign-in, CSRF, roles, private injury
  notes, athlete link pages, uploads and imports.
* The largest test loads the 62-athlete sample team as about 7,800 runs with second-by-second heart rate, through
  the same ingest code real files use, and checks that weekly mileage, injury weeks, load ratios, every rep's pace,
  heart rate and 60-second recovery, the headline numbers and the watch list all come back out matching the sample.
* `scripts/e2e.py` (41 checks) starts the real server with stand-in Garmin and Claude services and walks through a
  coach signing in, an athlete connecting Garmin, a webhook delivering a workout, the AI briefing and a coach
  question (verifying no names reach Claude), an athlete adding effort and pain to a synced run, and then renders the dashboard, charts and both athlete pages (all 24 chart/time-range combinations) in a headless browser
  (`scripts/harness.mjs`, needs `npm install jsdom@24`) and checks every number against the server's own calculation.

Run them: `pip install -r requirements-dev.txt && pytest -q`, then `cd scripts && python e2e.py`.

Bugs these checks caught and fixed before release: a CSS class clash that would have broken number columns on the dashboard; the "all years" charts starting at the oldest race result and understating weekly mileage; second on-screen notices being dropped (Starlette only saves
reassigned session keys); database changes committing after the redirect was sent (now committed first); new
athletes showing a false mileage spike (load ratio now waits for four weeks of history); slower runners' warm-ups
being mistaken for reps; the Garmin FIT decoder silently returning nothing after an integrity check.

## What could not be tested here

* **Live Garmin.** The flow follows Garmin's published OAuth 2.0 PKCE specification, but the exact notification
  field names are only given to approved developers. They are read in one function, `handle_notification` in
  `xcapp/garmin.py`; compare them with Garmin's Activity API documentation when you are approved, and send yourself
  one test run before inviting the team.
* **Live Claude API.** Tested against a stand-in that speaks the Messages API through the official SDK. The first
  real nightly run will confirm your key and model name (`ANTHROPIC_MODEL`).
* **The Docker image** was not built in this environment; the app was run directly with the same commands.

## Limits worth knowing

* Load ratio is a screening signal, not a diagnosis; research on exact cutoffs is mixed. The thresholds (1.3 load
  ratio, 25 bpm recovery, pain 4/10) live in `xcapp/analysis.py` and at the top of the dashboard's calculations.
* Manual-lap rep detection expects repeats of similar distance; ladders and pyramids should be run as structured
  workouts on the watch.
* XC 5K times depend on the course. Record the course with every result, and run 1 / 1.5 / 2 mile benchmarks as
  time trials on the same course.

## Upgrading

Starting the new version on an existing database adds the new columns automatically (effort, legs, notes, and the link from a pain report to its run). Back up `/data/xc.db` first anyway.

## Layout

`xcapp/` app code: `garmin.py` (connection, webhooks), `fitfile.py` + `reps.py` (FIT decoding, reps, recovery),
`ingest.py`, `analysis.py` (all coaching math), `ai.py`, `web.py`, `cli.py`, `demo.py`, `templates/`, `static/`
(`dashboard_body.html` is the dashboard page; the published prototype runs an earlier copy of it). `tests/`, `scripts/`.

# Deploying to Cloudflare and Supabase

The app runs in a **Cloudflare container**, and its data lives in a **Supabase** Postgres database. Every change pushed
to `main` on GitHub is tested and then deployed automatically by `.github/workflows/deploy.yml`.

Keys and passwords live only in **GitHub Secrets** (and from there, in Cloudflare). Never put them in a file in this
repository: it is public.

Cost: Cloudflare's Workers Paid plan ($5/month, includes some container time). Supabase's free plan is enough to start.

## 1. Cloudflare

1. Sign in at dash.cloudflare.com.
2. **Workers Paid plan:** left menu **Workers & Pages** → **Plans** → choose **Workers Paid**. Containers need it.
3. **workers.dev address:** if Workers & Pages asks you to pick a `workers.dev` subdomain, pick one (for example
   `jesuit-xc`). The app's address will be `https://xc-training.<your-subdomain>.workers.dev`.
4. **Account ID:** on the Workers & Pages overview page, copy **Account ID** (right-hand side). Save it for step 4.
5. **API token:** click your profile icon (top right) → **My Profile** → **API Tokens** → **Create Token** → use the
   **Edit Cloudflare Workers** template → under Account Resources pick your account → **Continue to summary** →
   **Create Token**. Copy the token now (Cloudflare shows it only once). If a deploy later fails with a permission error
   that mentions containers, edit the token and add the **Containers: Edit** permission.

## 2. Supabase database address

1. Sign in at supabase.com/dashboard and open your project.
2. Click **Connect** (top of the project page), then the **Direct — Connection string** box (not "Framework"). Choose
   the **Session pooler** connection string (not "Transaction pooler", which ends in `:6543`, and not "Direct
   connection", which needs IPv6). It contains `pooler.supabase.com` and ends in `:5432/postgres`, like:
   `postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-1-us-west-2.pooler.supabase.com:5432/postgres`
3. Change it in three places to make `DATABASE_URL`:
   * the start `postgresql://` becomes `postgresql+psycopg://`
   * `[YOUR-PASSWORD]` becomes your database password (forgot it? **Project Settings** → **Database** → **Reset
     database password**). If the password has symbols such as `@ : / ? # %`, reset it to one with only letters and
     numbers; those symbols break the address.
   * add `?sslmode=require` at the end.

   Result: `postgresql+psycopg://postgres.abcdefghijkl:yourpassword@aws-1-us-west-2.pooler.supabase.com:5432/postgres?sslmode=require`

The app creates its own tables the first time it starts.

## 3. The app's own keys

Run `python -m xcapp.cli gen-keys` (or ask whoever set this up to generate them for you). It prints five lines:
`SECRET_KEY`, `TOKEN_KEY`, `GARMIN_WEBHOOK_SECRET`, `CRON_SECRET` and `SETUP_CODE`. Keep a copy somewhere safe, such as
a password manager. **Never change `TOKEN_KEY` once athletes have connected Garmin**: it encrypts their Garmin tokens.

## 4. GitHub Secrets

On GitHub, open the repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
Add each one with exactly this name (paste the value with no spaces or quotes around it):

| Name | Value | Needed |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Token from step 1.5 | Yes |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID from step 1.4 | Yes |
| `DATABASE_URL` | Address from step 2 | Yes |
| `SECRET_KEY` | From step 3 | Yes |
| `TOKEN_KEY` | From step 3 | Yes |
| `GARMIN_WEBHOOK_SECRET` | From step 3 | Yes |
| `CRON_SECRET` | From step 3 | Yes |
| `SETUP_CODE` | From step 3 | To create the first admin |
| `GARMIN_CLIENT_ID`, `GARMIN_CLIENT_SECRET` | Garmin Developer Portal | Later, for Garmin sync |
| `ANTHROPIC_API_KEY` | Claude Console | Later, for the AI briefing |

GitHub never shows a secret again after you save it, not even to you, and never prints it in logs.

## 5. Deploy

1. On GitHub: **Actions** tab → **Test and deploy** (left) → **Run workflow** → **Run workflow**. (Every later push
   to `main` deploys by itself.)
2. Wait for both jobs to turn green (the first deploy takes several minutes while the container is built).
3. Open the **deploy** job → **Deploy to Cloudflare** step. Near the end it prints the app's address
   (`https://xc-training....workers.dev`).
4. Set that address as `BASE_URL` in `wrangler.jsonc` (under `vars`) and push. Athlete links and Garmin use it.

## 6. First sign-in

Open the app's address. The first visit after a quiet spell takes a few seconds while the container starts. The
sign-in page shows **Create the first admin**: enter your `SETUP_CODE`, choose a username and a password of 10 or more
characters. The form disappears once an account exists. Then follow `TEAM_SETUP.md` from section 3.

## Privacy policy

The app has a public privacy policy at `https://<your-address>/privacy` (for this team,
https://xc-training.jesuit-xc.workers.dev/privacy). Garmin asks for this link when you set up your app, and athletes see it
on their page. Its details come from `vars` in `wrangler.jsonc`:

| Setting | What to put |
|---|---|
| `ORG_NAME` | Who runs the app (set to Jesuit High School) |
| `PRIVACY_CONTACT` | The email address for privacy questions and deletion requests. It is shown publicly. |
| `PRIVACY_EFFECTIVE_DATE` | The date the policy takes effect, for example `November 15, 2026`, once the AD has approved it |
| `HOSTING_PROVIDER` | Already set: Cloudflare (runs the app) and Supabase (stores the database) |
| `CERTIFICATE_PROVIDER` | Already set: Cloudflare |
| `DATA_RETENTION_DAYS` | Days after an athlete leaves before their data is deleted (90) |

Empty settings show as placeholders such as `[privacy contact email]`, so fill in the contact and date before giving
Garmin the link. Edit `wrangler.jsonc` on GitHub (open the file, click the pencil, change the value between the quotes,
**Commit changes**); the change deploys by itself. These are not secrets.

## Later

* **Garmin:** add `GARMIN_CLIENT_ID` and `GARMIN_CLIENT_SECRET` as GitHub secrets, run the workflow again, then follow
  `plans/plan-garmin-go-live.md`. The webhook address is on the Sync log page (admins only); in Garmin's Endpoint Configuration,
  enable it for Activities, Activity Details, Activity Files, Deregistrations and User Permissions Change, and, for daily
  resting heart rate, **Dailies** (in the Health section). When an
  athlete connects, the app asks Garmin for their past `GARMIN_BACKFILL_DAYS` (90) days of runs; coaches can repeat
  that from the athlete's page (**Load past runs**).
* **AI briefing:** get a key at platform.claude.com (**API Keys** → **Create Key**), add it as the `ANTHROPIC_API_KEY`
  secret, and run the workflow again. Also set a monthly spend limit in the Claude Console. The app keeps its own
  budget too: `AI_MONTHLY_BUDGET_USD` in `wrangler.jsonc` (25 dollars to start); the AI page shows this month's spending,
  and requests stop for the month once the next one could go over. `AI_EFFORT` (medium) sets how much Claude thinks
  before answering; higher costs more. A nightly briefing for about 60 athletes costs roughly $0.12–0.17. The nightly job runs at 4 a.m. Pacific
  (`NIGHTLY_HOUR` and `TEAM_TIMEZONE` in `wrangler.jsonc`; change the two `crons` there if you change them).
* **Your own address** (for example `xc.yourschool.org`): Cloudflare → the `xc-training` Worker → **Settings** →
  **Domains & Routes**, then update `BASE_URL`.
* **Logs:** Cloudflare → Workers & Pages → `xc-training` → **Logs**. The app's own **Sync log** page shows syncs and
  nightly runs.
* **Backups:** the data is in Supabase. Check which backups your Supabase plan includes, and download one before big
  changes.

## Troubleshooting

| Problem | What to do |
|---|---|
| Deploy job says "Cloudflare is not set up yet" | Add `CLOUDFLARE_API_TOKEN` (step 4). |
| Deploy job lists missing secrets | Add each one it names, then run the workflow again. |
| Deploy fails mentioning a plan or containers | Turn on Workers Paid (step 1.2); check the token's permissions (step 1.5). |
| Deploy fails mentioning a workers.dev subdomain | Pick one (step 1.3). |
| Page says the app could not start, or Logs show a database error | Check `DATABASE_URL` (step 2): session pooler, `postgresql+psycopg://`, password, `?sslmode=require`. |
| Sign-in page has no "Create the first admin" form | `SETUP_CODE` is not set, or an account already exists. |

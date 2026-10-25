# Getting the whole team connected

## Garmin approved: switching it on

1. **Host the app first.** Garmin sends athletes back to the app and delivers runs to it, so it needs its public
   https address (for example `https://xc.jesuithighschool.org`) before anything else. See README, "Run it for real".
2. **Credentials.** From your app in the Garmin Developer Portal, copy the client ID and client secret into `.env` as
   `GARMIN_CLIENT_ID` and `GARMIN_CLIENT_SECRET`, and restart.
3. **Redirect address.** If the portal asks for one, use `https://your-address/garmin/callback`.
4. **Notifications.** Sign in as admin: the home page shows your private notification address
   (`https://your-address/garmin/webhook/...`). In the portal's endpoint settings, point these at it: activity
   summaries, activity files (FIT), deregistrations, and user permission changes. If the portal offers a choice of
   push or ping, either works.
5. **Check the notification format.** Garmin's Activity API documentation (in the portal now that you're approved)
   shows the exact notification fields. The app reads them in one place, `handle_notification` in `xcapp/garmin.py`;
   compare the field names before the team connects.
6. **Test with your own watch.** Add yourself as an athlete, open your link, connect Garmin, go for a short run with a
   few laps, sync, and check **Sync log** and your athlete page. Then try **Disconnect** and reconnect.
7. **Pilot, then the team.** Connect your assistants and three to five athletes for a week or two, then follow the
   steps below for everyone.

Plan on about two weeks for approvals, then one 15-minute "connect night" at practice.

## 1. Approvals first (week 1)

**District and athletic director.** Your athletes are minors, and this stores their training and injury records.
Ask your AD who signs off on student data tools (often district IT, under a student data privacy agreement). What
to tell them:
* Stored: runs (date, distance, time, laps, heart rate), benchmark results, goals, injuries, and pain reports.
  No GPS routes or locations.
* Who sees it: staff accounts only. Injury notes are visible only to the athletic trainer and admins. Each athlete
  sees their own page.
* AI: each night a summary goes to Anthropic's Claude API with athletes as codes (R01, R02, ...), never names;
  injury notes and free text are never sent. Have the district review Anthropic's commercial terms.
* Deletion: an admin can delete an athlete and all their data; disconnecting Garmin also revokes access at Garmin.

**Garmin.** Apply to the Garmin Connect Developer Program as an organization (the school or booster club). The free
Starter tier covers up to 1,000 users. You need the app's public web address for the redirect URL
(`https://your-address/garmin/callback`).

**Hosting and AI.** Ask school IT (or a tech-savvy parent) to run it with the included Docker setup
(see README). Decide who owns the Anthropic API account and billing; booster clubs often do.

## 2. Parent consent (week 1-2)

Send a short form home. Suggested wording:

> Our cross country team uses a training system that collects your athlete's running data from their Garmin watch
> (distance, pace, laps and heart rate, not locations), their race and time-trial results, and any injuries the
> coaches or athletic trainer log. Coaches use it to adjust training and reduce injuries. An AI tool summarizes the
> team's numbers using codes instead of names. Your athlete can disconnect at any time, and you can ask us to
> delete their data. Participation is optional and does not affect team selection.
> [ ] I give permission for my athlete to connect their Garmin account. Parent/guardian signature, date.

Keep the signed forms. On their link page, athletes must also tick a box confirming a parent approved before they
can connect.

## 3. Set up the app (1 hour)

1. Sign in as admin. Under **Staff**, add assistant coaches (role *coach*) and your athletic trainer (role *trainer*).
2. Under **Athletes**, import the roster CSV:
   ```
   name,gender,grade,group,hr_max
   Ava Doyle,Girls,11,Varsity,
   Mateo Garza,Boys,9,JV,
   ```
   `gender` is Boys or Girls; `group` is Varsity, JV or Development; `hr_max` is optional.
3. Under **Results and goals**, import last season's results and this season's goals (formats in section 7).
4. Send yourself a test: make a staff test athlete, connect your own Garmin, go for a run, and check that it shows
   up in **Sync log** and on the athlete's page.

## 4. Send every athlete their link

On **Athletes**, click **Download every athlete's link**. You get a CSV with each name and personal link. Use it in
a mail merge (Gmail, Outlook or your team messaging app) to send each athlete, and parent, only their own link. A
link works like a password: anyone holding it can connect a Garmin account or report pain for that athlete. If one
leaks, open the athlete and click **Make a new link**.

## 5. Connect night (15 minutes at practice)

Have athletes bring their phones with the Garmin Connect app installed and signed in.

1. Open their personal link.
2. Tick the parent-approval box (only if the form came back signed), then tap **Connect Garmin**.
3. Sign in to Garmin and allow the team app to read **activities**.
4. They land back on their page showing **Connected**.

Watch the **Athletes** page fill up with green *Connected* labels. Chase the rest the next day. Runs appear after
the watch syncs to the Garmin Connect app, usually within minutes.

**No Garmin?** Watches from COROS, Suunto and others can export `.fit` files; athletes upload them on their link
page (after each workout, or weekly). Apple Watch needs an app that exports workouts as `.fit` files. Phone-only
runners can be added as results only.

## 6. Logo and team name

Once the communications office approves it, copy the logo file into the server's data folder (for example
`/data/team-logo.png`), set `TEAM_LOGO_FILE=/data/team-logo.png` and `TEAM_NAME=Jesuit Cross Country` in `.env`, and
restart. It appears on every coach page and every athlete page.

## 7. What athletes do after each run (and on days they don't)

1. Their watch syncs, and the run appears on their page within minutes.
2. They tap **Add effort** (or open **My runs**) and pick how hard the run felt (0 = rest, 10 = max), how their legs
   felt, and any pain: where and how much. The optional note goes to coaches and the athletic trainer only.
3. Their **My training** chart lets them switch between miles, load ratio, effort, heart rate, pace and benchmark
   times, over the past week, past month, this season or all years.

4. On a practice day with no run, **My runs** shows "Tell coaches why". The athlete picks the reason; for an
   injury they say where, which side, how much it hurts, when it started, describe it in their own words, and say
   who has looked at it and whether they expect to miss more days. It reaches your watch list right away and the
   **Injuries** page, where the athletic trainer can add it to the injury log in one click.

Effort takes ten seconds per run. Ask for it after every workout and long run at minimum: effort and pain, alongside
mileage, are what make the injury picture clear.

## 8. Training groups

Open **Groups**. The app starts with the team's groups from the VDOT JHS sheet: Platinum (73+), Gold (68-72), Silver
(62-67), Bronze (58-61), Emerald (54-57), Aquamarine (49-53) and Ivory (48 and below), in the sheet's colors, plus the
sheet's 800 and 1600 target for every VDOT from 48 to 74. Each athlete's VDOT comes from their best 1 mile, 1.5 mile,
2 mile or 5K result, read on the sheet's scale: a result counts as VDOT 60 if its 1600-equivalent reaches 4:57, so the
app and the sheet agree. Athletes see their group, VDOT and 800/1600 targets. Rename or recolor groups, change the
boys' and girls' cutoffs, add or remove groups, add rows to the target table (below 48, for example) and choose how
far back results count. Each row shows how many boys and girls are in that group
now, so you can tune the cutoffs until the groups match how you train. To keep an athlete in a group regardless of
their times (coming back from injury, for example), pin them on their athlete page. When you plan a workout, tick the
groups it is for: Gold and Platinum doing 6 x 1K while Bronze does 4 x 1K, for example.

## 9. Fixing a run's type or workout

If a run was matched to the wrong workout, or a race shows as an easy run, open it (athletes: **My runs**; coaches:
click the date on the athlete's page) and set the type of run and the team workout. That choice sticks.

## 10. The weekly routine

* **Before practice:** add the day's rep workout under **Workouts** (date, name, rep distance, pace event and
  factor). If you forget, add it later and already-synced runs are matched automatically.
* **When someone gets hurt:** the trainer or a coach logs it under **Injuries**; add the return date when cleared.
  Athletes can report pain from their link; 4/10 or more puts them on the watch list.
* **Each morning:** read the watch list and the AI briefing (home page, **AI** or the dashboard).
* **After each meet:** import the results (section 7) so the charts stay current.

## 11. XC Stats-style charts

Open **Charts**. Everything comes from the benchmark results you import or enter, so import every meet and time trial.

**Importing results.** Export results from XC Stats or Athletic.net (or type them in on **Results and goals**) and
make sure the CSV has these columns:
```
athlete,event,time,date,course,kind
Ava Doyle,5K,18:42.3,2026-10-17,Mather Park,race
Ava Doyle,1600m,5:31.0,04/18/2026,,track
```
`event` is 1 Mile, 1.5 Mile, 2 Mile, 5K, 1600m or 3200m (track times are converted to mile and 2-mile equivalents
at the same pace); `kind` is race, time trial or track. Names must match the roster ("Last, First" also works). If
any row is wrong, nothing is imported and every bad row is listed, so fix and re-import the whole file.

**What each chart shows**
* **Career:** one panel per benchmark event, every result over time, personal record highlighted. Hover for date,
  course and season.
* **Compare athletes:** pick an event and season, then tick up to six athletes to see their results through the
  season on one chart. Good for choosing your top seven.
* **Improvement vs last season:** this XC season's best minus last season's, per athlete, fastest improvers first.
* **Top-7 team average:** for every date with at least seven 5K results from one team, the average of the seven
  fastest, tracked across meets and seasons.

The dashboard adds what XC Stats doesn't: heart rate rep by rep, load ratio and injury risk, the watch list, and the
AI briefing. One thing XC Stats does that this does not: adjust times for course difficulty. Compare 5K times on
the same course, and run the 1, 1.5 and 2 mile benchmarks as time trials on the same course each time.

## Troubleshooting

| Problem | What to do |
|---|---|
| Athlete shows *Needs reconnect* | Their Garmin authorization lapsed. They open their link and connect again. |
| A run is missing | Check the Garmin Connect app synced. Then look at **Sync log**. If it's still missing, export the `.fit` from Garmin Connect (Export Original) and upload it on their link page. |
| Reps not detected | Use a structured workout on the watch, or press lap at the start and end of every rep and rest. |
| Workout not matched | Make sure a workout is planned for that date and the athlete's group is included. |
| Load ratio blank | Normal for an athlete's first four weeks of data. |
| AI briefing failed | The reason is shown on the **AI** page. Most often the API key or model name in `.env`. |

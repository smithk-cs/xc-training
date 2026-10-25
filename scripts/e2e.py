"""End-to-end run: real server + demo team + Garmin connection + webhook + AI + headless page checks."""
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP, FAKE = "http://127.0.0.1:8765", "http://127.0.0.1:9100"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


tmp = tempfile.mkdtemp()
from cryptography.fernet import Fernet  # noqa: E402
names = [r["athlete"] for r in csv.DictReader(open(os.path.join(ROOT, "xcapp/demo/roster.csv")))]
env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/xc.db", "SECRET_KEY": "k" * 48, "TOKEN_KEY": Fernet.generate_key().decode(),
       "BASE_URL": APP, "SECURE_COOKIES": "false", "AS_OF_DATE": "2026-10-25", "GARMIN_CLIENT_ID": "e2e-client",
       "GARMIN_CLIENT_SECRET": "e2e-secret", "GARMIN_WEBHOOK_SECRET": "e2e-hook", "GARMIN_AUTHORIZE_URL": f"{FAKE}/oauth2Confirm",
       "GARMIN_TOKEN_URL": f"{FAKE}/di-oauth2-service/oauth/token", "GARMIN_API_BASE": FAKE, "GARMIN_FILE_HOSTS": FAKE,
       "ANTHROPIC_API_KEY": "sk-test-e2e", "ANTHROPIC_BASE_URL": FAKE, "ROSTER_NAMES": "|".join(names + ["Test Garmin"]),
       "PYTHONPATH": ROOT}
run = lambda *a: subprocess.run([sys.executable, "-m", "xcapp.cli", *a], env=env, cwd=ROOT, capture_output=True, text=True)
r = run("create-user", "coachk", "--role", "admin", "--password", "e2e-password-1")
check("CLI creates the admin account", r.returncode == 0, r.stderr[-300:])
r = run("load-demo")
check("CLI loads the 62-athlete demo through the real pipeline", r.returncode == 0 and "62 athletes" in r.stdout, (r.stdout + r.stderr)[-300:])
r = run("check")
check("CLI settings check passes", r.returncode == 0, r.stdout)

fake = subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_services:app", "--port", "9100", "--log-level", "warning"], env=env, cwd=HERE)
web = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "xcapp.web:create_app", "--port", "8765", "--log-level", "warning"], env=env, cwd=ROOT)
try:
    for _ in range(100):
        try:
            if httpx.get(APP + "/healthz").status_code == 200 and httpx.get(FAKE + "/_log").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    check("Server starts and reports healthy", httpx.get(APP + "/healthz").json() == {"ok": True})
    c = httpx.Client(base_url=APP, follow_redirects=False)
    tok = lambda url: re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)
    r = c.post("/login", data={"csrf": tok("/login"), "username": "coachk", "password": "e2e-password-1"})
    check("Coach signs in", r.status_code == 303 and r.headers["location"] == "/")
    home = c.get("/").text
    check("Home shows the watch list and Garmin webhook address", "Watch list" in home and "/garmin/webhook/e2e-hook" in home)

    r = c.post("/athletes", data={"csrf": tok("/athletes"), "first_name": "Test", "last_name": "Garmin", "gender": "Girls", "grade": "10", "group": "Varsity"})
    links = c.get("/athletes/links.csv").text
    link = next(l for l in links.splitlines() if l.startswith("Test Garmin")).split(",")[-1]
    check("New athlete appears in the link export", link.startswith(APP + "/a/"))

    a = httpx.Client(follow_redirects=False)  # the athlete's phone
    page = a.get(link).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = a.post(link + "/connect", data={"csrf": csrf, "consent": "yes"})
    to_garmin = r.headers.get("location", "")
    r = a.get(to_garmin)  # "Garmin" sends the athlete back with a code
    back = r.headers.get("location", "")
    r = a.get(back)
    after = a.get(link).text
    check("Athlete connects Garmin through the OAuth PKCE flow", r.status_code == 303 and 'pill good">✓ Connected' in after,
          (r.status_code, r.headers.get("location"), re.findall(r'<div class="flash[^>]*>([^<]*)', after)))
    forms = httpx.get(FAKE + "/_log").json()["token_forms"]
    check("Token request carried client secret and PKCE verifier", forms and {"client_secret", "code_verifier", "code"} <= set(forms[0]), forms)

    note = {"activityFiles": [{"userId": "e2e-garmin-user", "summaryId": "f1", "fileType": "FIT", "callbackURL": FAKE + "/files/1"}]}
    r = httpx.post(APP + "/garmin/webhook/e2e-hook", json=note, headers={"garmin-client-id": "e2e-client"})
    check("Webhook accepted", r.status_code == 200)
    time.sleep(1.5)
    segs = list(csv.DictReader(c.get("/api/datasets/segments.csv").text.splitlines()))
    mine = [s for s in segs if s["athlete"] == "Test Garmin"]
    check("Synced workout reaches the dashboard data: 5 reps, matched to the Oct 20 plan",
          len(mine) == 5 and {s["workout"] for s in mine} == {"5 x 1K @ 5K pace"}, len(mine))
    check("Heart-rate recovery measured from the synced file", [s["hr_drop_60s"] for s in mine] == ["34", "31", "27", "21", ""], [s["hr_drop_60s"] for s in mine])
    check("Device attribution recorded", mine and mine[0]["device"] == "Garmin Forerunner 265")
    httpx.post(APP + "/garmin/webhook/e2e-hook", json=note)
    time.sleep(1.0)
    check("Repeat notification does not duplicate the run", len([s for s in csv.DictReader(c.get("/api/datasets/segments.csv").text.splitlines()) if s["athlete"] == "Test Garmin"]) == 5)

    r = c.post("/ai/run", data={"csrf": tok("/ai")})
    ai_page = c.get("/ai").text
    calls = httpx.get(FAKE + "/_log").json()["claude"]
    check("AI briefing generated through the Anthropic SDK", "Check in today" in ai_page and calls, ai_page[:200] if not calls else "")
    check("Claude request had API key, version header and model", calls and calls[-1]["api_key"] == "sk-test-e2e" and calls[-1]["version"] and calls[-1]["model"] == "claude-sonnet-5-5", calls[-1:] )
    check("No athlete names were sent to Claude", calls and all(not c_["names_found"] for c_ in calls), [c_["names_found"] for c_ in calls])
    scopes = re.findall(r'<span class="small muted">[^<]*· ([^<]+)</span>', ai_page)
    codes_left = re.findall(r"\bR\d{2,3}\b", ai_page)
    check("Names restored in the briefing the coach sees", scopes and all(x == "Team" or any(n in x for n in names) for x in scopes) and not codes_left, (scopes, codes_left))
    c.post("/ai/ask", data={"csrf": tok("/ai"), "question": "Is Lucia Sorensen OK to race Saturday?"})
    calls = httpx.get(FAKE + "/_log").json()["claude"]
    check("Coach question answered without sending names", not calls[-1]["names_found"] and "has the most flags" in c.get("/ai").text)

    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    out = subprocess.run(["node", "harness.mjs", "dashboard", APP + "/dashboard", cookie], cwd=HERE, capture_output=True, text=True, timeout=120)
    dash = json.loads(out.stdout or "{}") if out.returncode == 0 else {"errors": [out.stderr[-500:]]}
    json.dump(dash, open(os.path.join(tmp, "dash.json"), "w"), indent=1)
    check("Dashboard renders in a browser with no script errors", dash.get("ready") and not dash.get("errors"), dash.get("errors"))
    check("Dashboard shows no NaN/undefined values", dash.get("bad_values") == [], dash.get("bad_values"))
    sys.path.insert(0, ROOT)
    os.environ.update({k: v for k, v in env.items() if k in ("DATABASE_URL", "TOKEN_KEY", "SECRET_KEY", "AS_OF_DATE")})
    from xcapp import config, db as D, datasets as DS  # noqa: E402
    config.reload_settings()
    D.init_engine(env["DATABASE_URL"])
    s = D.SessionLocal()
    snap = DS.snapshot(s, dt.date(2026, 10, 25))
    k = snap["kpis"]
    want = {"injured_now": str(k["injured_now"]), "over_now": str(k["over_now"]), "over_prev": str(k["over_prev"]),
            "median_gain": f"{k['median_gain']:.1f}", "returners": str(k["returners"]), "goals_met": str(k["goals_met"]), "goal_total": str(k["goal_total"])}
    check("Dashboard headline numbers equal the server's own calculation", dash.get("kpis") == want, (dash.get("kpis"), want))
    check("Dashboard watch list equals the server's", [r_[0] for r_ in dash.get("watch", [])] == [w["athlete"] for w in snap["watchlist"]],
          ([r_[0] for r_ in dash.get("watch", [])], [w["athlete"] for w in snap["watchlist"]]))
    check("Season columns follow the data", dash.get("season_headers", [])[2:5] == ["2025 XC", "2026 track", "2026 XC"], dash.get("season_headers"))
    check("Every tab draws", dash.get("bench_points", 0) > 0 and dash.get("rep_svg_circles", 0) > 0 and dash.get("load_bars", 0) > 0 and dash.get("team_svg") == 1 and dash.get("injury_rows") == len(snap["injuries"]))
    out = subprocess.run(["node", "harness.mjs", "charts", APP + "/charts", cookie], cwd=HERE, capture_output=True, text=True, timeout=120)
    charts = json.loads(out.stdout or "{}") if out.returncode == 0 else {"errors": [out.stderr[-500:]]}
    json.dump(charts, open(os.path.join(tmp, "charts.json"), "w"), indent=1)
    check("Results charts render with no script errors", charts.get("ready") and not charts.get("errors") and charts.get("bad_values") == [], charts)
    check("Charts: 4 career panels, comparison, improvement bars and top-7 points", charts.get("career_panels") == 4 and charts.get("compare_lines", 0) >= 3
          and charts.get("improve_bars", 0) > 0 and charts.get("top7_points") == 4 and charts.get("compare_after_more") == 6 and charts.get("compare_disabled_at_6"), charts)

    # ---- athlete pages: charts with dropdowns, My runs, effort and pain per run
    lucia = next(l for l in links.splitlines() if l.startswith("Lucia Sorensen")).split(",")[-1]
    page = httpx.get(lucia).text
    check("Athlete page is branded and shows the charts", "Hi Lucia" in page and "My training" in page and "Time range" in page)
    for who, url, ck in (("athlete", lucia, ""), ("coach", APP + "/athletes/59", cookie)):
        out = subprocess.run(["node", "harness.mjs", "athlete", url, ck], cwd=HERE, capture_output=True, text=True, timeout=180)
        res = json.loads(out.stdout or "{}") if out.returncode == 0 else {"errors": [out.stderr[-500:]]}
        json.dump(res, open(os.path.join(tmp, f"athlete_charts_{who}.json"), "w"), indent=1)
        combos = res.get("combos", [])
        check(f"{who.title()} charts: all 24 chart/range combinations draw with no script errors or NaN",
              res.get("data_state") == "1" and len(combos) == 24 and not res.get("errors") and res.get("bad_values") == []
              and all((c_["svg"] or c_["empty"]) and c_["summary"] for c_ in combos), {k: res.get(k) for k in ("data_state", "errors", "bad_values")})
        get = lambda t, r_: next(c_ for c_ in combos if c_["type"] == t and c_["range"] == r_)
        check(f"{who.title()} charts show real data (miles, load ratio, effort, heart rate, pace, benchmarks, injury shading)",
              get("miles", "season")["marks"] > 10 and get("load", "season")["svg"] and get("effort", "month")["marks"] > 5
              and get("hr", "season")["svg"] and get("pace", "month")["svg"] and get("results", "years")["marks"] >= 4
              and "Injured" in get("miles", "month")["legend"], [(c_["type"], c_["range"], c_["marks"], c_["empty"][:30]) for c_ in combos])
    runs_page = a.get(link + "/runs").text
    run_id = re.search(r'/runs/(\d+)"', runs_page).group(1)
    check("New athlete is prompted to add effort", "needs your effort rating" in a.get(link).text and "Add effort" in runs_page)
    r = a.post(f"{link}/runs/{run_id}", data={"csrf": csrf, "rpe": "8", "feel": "Heavy", "pain_area": "Lower leg", "pain_level": "6", "note": "shin tight"})
    check("Athlete saves effort, legs and pain for a synced run", r.status_code == 303 and "Effort 8 · Heavy" in a.get(link + "/runs").text)
    aid = re.search(r'href="/athletes/(\d+)">Test Garmin<', c.get("/athletes").text).group(1)
    detail = c.get(f"/athletes/{aid}").text
    check("Coach sees the effort, legs, pain and note on the athlete page", "6/10 Lower leg" in detail and "Athlete note: shin tight" in detail and ">Heavy<" in detail)
    check("Pain from the run puts the athlete on the watch list", "Pain 6/10: Lower leg" in c.get("/").text)

    r = a.post(f"{link}/missed/2026-10-24", data={"csrf": csrf, "reason": "Injury", "body_area": "Ankle", "side": "Left", "pain": "5",
                                                "started": "Today", "description": "rolled it on a trail", "seen_by": "Not yet", "more_days": "Not sure yet"})
    check("Athlete reports a day missed through injury", r.status_code == 303 and "Sent to your coaches" in a.get(f"{link}/missed/2026-10-24").text)
    check("The missed-run injury reaches the watch list and the injuries page",
          "Missed run (injury): Ankle, left, 5/10" in c.get("/").text and "rolled it on a trail" in c.get("/injuries").text)
    out = subprocess.run(["node", "harness.mjs", "dashboard", APP + "/dashboard", cookie], cwd=HERE, capture_output=True, text=True, timeout=120)
    dash2 = json.loads(out.stdout or "{}") if out.returncode == 0 else {"errors": [out.stderr[-500:]]}
    check("Dashboard watch list shows the missed-run injury (same rule as the server)",
          not dash2.get("errors") and any(r_[0] == "Test Garmin" and "Missed run (injury): Ankle, left, 5/10" in r_[-1] for r_ in dash2.get("watch", [])),
          [r_ for r_ in dash2.get("watch", []) if r_[0] == "Test Garmin"])

    r = a.post(link + "/disconnect", data={"csrf": csrf})
    check("Athlete disconnect deregisters at Garmin", httpx.get(FAKE + "/_log").json()["deletes"] == 1 and "Connect Garmin" in a.get(link).text)
    r = run("nightly")
    check("Nightly job writes a new briefing", r.returncode == 0 and "'briefing': 'ok'" in r.stdout, (r.stdout + r.stderr)[-300:])
    print("DASH", json.dumps({k_: dash.get(k_) for k_ in ("sub", "take", "kpis", "first_insight_scope", "season_headers", "workout_buttons", "rep_legend", "bands", "after_row_click_athlete", "sorted_days", "search_rows", "ev_label")}))
    print("CHARTS", json.dumps({k_: v for k_, v in charts.items() if k_ != "errors"}))
finally:
    web.terminate()
    fake.terminate()
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)} of {len(results)} end-to-end checks passed")
sys.exit(1 if failed else 0)

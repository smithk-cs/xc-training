"""Command-line tools.  Run `python -m xcapp.cli --help`."""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import secrets
import sys
import time
from pathlib import Path

from sqlalchemy import select


def _setup():
    from .config import settings
    from .db import create_all
    problems = [p for p in settings().problems() if not p.startswith("GARMIN_WEBHOOK")]
    if any(p.startswith("TOKEN_KEY") for p in problems):
        sys.exit("TOKEN_KEY is not set. Run `python -m xcapp.cli gen-keys` and add the values to your .env file.")
    create_all()


def cmd_gen_keys(_):
    from .security import generate_token_key
    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"TOKEN_KEY={generate_token_key()}")
    print(f"GARMIN_WEBHOOK_SECRET={secrets.token_urlsafe(24)}")
    print(f"CRON_SECRET={secrets.token_urlsafe(32)}")
    print(f"SETUP_CODE={secrets.token_urlsafe(18)}")


def cmd_init_db(_):
    _setup()
    print("Database ready.")


def cmd_create_user(args):
    _setup()
    from .db import session_scope
    from .models import StaffUser
    from .security import hash_password
    if args.role not in ("admin", "coach", "trainer"):
        sys.exit("Role must be admin, coach or trainer.")
    pw = args.password or getpass.getpass("Password (10+ characters): ")
    with session_scope() as db:
        name = args.username.strip().lower()
        if db.scalar(select(StaffUser).where(StaffUser.username == name)):
            sys.exit("That username already exists.")
        db.add(StaffUser(username=name, role=args.role, password_hash=hash_password(pw)))
    print(f"Created {args.role} account '{name}'.")


def cmd_load_demo(_):
    _setup()
    from .db import session_scope
    from .demo import load_demo
    with session_scope() as db:
        out = load_demo(db)
    print(f"Loaded the sample team: {out['athletes']} athletes, {out['activities']} runs, {out['workouts']} planned workouts.")
    print("Set AS_OF_DATE=2026-10-25 so the dashboard shows the sample season as current.")


def _import(fn_name, path):
    _setup()
    from . import imports as I
    from .analysis import benchmark_rows, current_xc_season, season_for, team_today
    from .db import session_scope
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        with session_scope() as db:
            if fn_name == "goals":
                season = current_xc_season(benchmark_rows(db)) or season_for(team_today())
                n = I.import_goals(db, text, season)
            else:
                n = getattr(I, f"import_{fn_name}")(db, text)
    except I.ImportErrors as exc:
        print("Nothing was imported. Fix these rows:")
        for e in exc.errors:
            print("  " + e)
        sys.exit(1)
    print(f"Imported {n} rows.")


def is_nightly_hour(now: dt.datetime | None = None) -> bool:
    """True during NIGHTLY_HOUR in the team's time zone (timers on UTC call at both daylight-saving offsets)."""
    from .config import settings
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(settings().tz).hour == settings().nightly_hour


def nightly() -> dict:
    from . import ai as AI
    from . import garmin as G
    from .config import settings
    from .db import session_scope
    from .ingest import log
    out = {"tokens_refreshed": 0, "briefing": "skipped"}
    with session_scope() as db:
        if settings().garmin_configured:
            with G.make_client() as http:
                out["tokens_refreshed"] = G.refresh_expiring(db, http)
        if settings().ai_configured:
            run = AI.generate_briefing(db)
            out["briefing"] = run.status if run.status == "ok" else f"failed: {run.error}"
        log(db, "nightly", f"Nightly job: {out}")
    return out


def cmd_nightly(_):
    _setup()
    print(nightly())


def cmd_scheduler(_):
    _setup()
    from .config import settings
    print(f"Scheduler running; nightly job at {settings().nightly_hour}:00 {settings().team_timezone}.", flush=True)
    while True:
        tz = settings().tz
        now = dt.datetime.now(tz)
        nxt = now.replace(hour=settings().nightly_hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        try:
            print(dt.datetime.now(tz).isoformat(), nightly(), flush=True)
        except Exception as exc:  # keep the scheduler alive
            print("Nightly job failed:", exc, flush=True)


def cmd_check(_):
    from .config import settings
    s = settings()
    problems = s.problems()
    print("Settings problems:" if problems else "Settings look complete.")
    for p in problems:
        print("  - " + p)
    print(f"Garmin: {'configured' if s.garmin_configured else 'not configured (athletes can still upload .fit files)'}")
    print(f"AI briefing: {'configured, model ' + s.anthropic_model if s.ai_configured else 'not configured'}")
    print(f"Public address: {s.base_url}  (Garmin redirect: {s.garmin_redirect_uri})")
    sys.exit(1 if problems else 0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m xcapp.cli", description="XC Training command-line tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-keys", help="print new SECRET_KEY, TOKEN_KEY, GARMIN_WEBHOOK_SECRET, CRON_SECRET and SETUP_CODE values").set_defaults(fn=cmd_gen_keys)
    sub.add_parser("init-db", help="create the database tables").set_defaults(fn=cmd_init_db)
    sub.add_parser("check", help="check the settings").set_defaults(fn=cmd_check)
    c = sub.add_parser("create-user", help="create a staff account")
    c.add_argument("username")
    c.add_argument("--role", default="coach")
    c.add_argument("--password", help="omit to be prompted")
    c.set_defaults(fn=cmd_create_user)
    sub.add_parser("load-demo", help="load the 62-athlete sample team into an empty database").set_defaults(fn=cmd_load_demo)
    for name in ("roster", "results", "goals"):
        x = sub.add_parser(f"import-{name}", help=f"import a {name} CSV file")
        x.add_argument("path")
        x.set_defaults(fn=lambda a, n=name: _import(n, a.path))
    sub.add_parser("nightly", help="refresh Garmin tokens and write the AI briefing now").set_defaults(fn=cmd_nightly)
    sub.add_parser("scheduler", help="run the nightly job every night (for a container)").set_defaults(fn=cmd_scheduler)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

"""Supabase's web API (PostgREST) is closed to the app's tables: row level security on, API roles without grants.
Postgres only (TEST_DATABASE_URL); skipped on SQLite."""
import os

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Postgres only")


def test_tables_locked_to_the_api_but_open_to_their_owner():
    from xcapp import db as D
    url = os.environ["TEST_DATABASE_URL"]
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        for role in ("anon", "authenticated", "xc_owner"):
            c.execute(text(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                           f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$"))
        c.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated"))   # as Supabase does
        # A table owned by a non-superuser, like the app's tables on Supabase
        c.execute(text("GRANT USAGE, CREATE ON SCHEMA public TO xc_owner"))
        c.execute(text("SET ROLE xc_owner"))
        c.execute(text("CREATE TABLE IF NOT EXISTS owned_check (id int)"))
        c.execute(text("RESET ROLE"))
    with D.engine().begin() as conn:
        conn.execute(text("SET ROLE xc_owner"))
        D.lock_down(conn)                          # as the owner: its table gets row level security
        conn.execute(text("RESET ROLE"))
        D.lock_down(conn)                          # as the app's own user: every other table
    with admin.connect() as c:
        open_tables = c.execute(text("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                     "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity")).scalars().all()
        assert open_tables == []
        assert c.execute(text("SELECT has_table_privilege('anon', 'public.athletes', 'SELECT')")).scalar() is False
        assert c.execute(text("SELECT has_table_privilege('authenticated', 'public.activities', 'INSERT')")).scalar() is False
        c.execute(text("SET ROLE xc_owner"))
        c.execute(text("INSERT INTO owned_check VALUES (1)"))
        assert c.execute(text("SELECT count(*) FROM owned_check")).scalar() == 1   # the owner still reads and writes
        c.execute(text("RESET ROLE"))
        c.execute(text("DROP TABLE owned_check"))
    admin.dispose()


def test_app_starts_and_works_with_tables_locked():
    from fastapi.testclient import TestClient

    from xcapp import db as D
    from xcapp.web import create_app
    D.create_all()                                  # runs the lock-down again: nothing left to do, no errors
    assert TestClient(create_app()).get("/login").status_code == 200

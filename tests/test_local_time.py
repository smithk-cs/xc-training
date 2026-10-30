"""Times are stored in UTC and shown in the team's time zone (Pacific by default)."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from xcapp import analysis as A
from xcapp.db import SessionLocal
from xcapp.models import StaffUser, SyncLog
from xcapp.security import hash_password


def test_local_time():
    assert A.local_time(None) is None
    summer = A.local_time(dt.datetime(2026, 7, 1, 3, 30))           # 3:30 UTC is 8:30 p.m. the day before (PDT)
    assert (summer.date(), summer.hour, summer.minute) == (dt.date(2026, 6, 30), 20, 30)
    winter = A.local_time(dt.datetime(2026, 12, 1, 18, 0, tzinfo=dt.timezone.utc))   # 10 a.m. PST
    assert (winter.date(), winter.hour) == (dt.date(2026, 12, 1), 10)


def test_pages_show_pacific_time():
    db = SessionLocal()
    db.add(StaffUser(username="admin1", role="admin", password_hash=hash_password("correct-horse-1")))
    db.add(SyncLog(created_at=dt.datetime(2026, 10, 30, 3, 37), kind="test", message="a test event"))
    db.commit()
    db.close()
    from xcapp.web import create_app
    c = TestClient(create_app(), follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": "admin1", "password": "correct-horse-1"})
    page = c.get("/sync").text
    assert "Recent events (Pacific time)" in page and "Oct 29 20:37" in page and "Oct 30 03:37" not in page

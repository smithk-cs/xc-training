import re

from fastapi.testclient import TestClient

from xcapp import sections as SEC


def test_addresses_belong_to_their_section():
    assert SEC.section_for("/week/settings") == "week" and SEC.section_for("/newsletter-tips") == "newsletter"
    assert SEC.section_for("/api/charts/results.json") == "charts" and SEC.section_for("/data-check") == "sync"
    assert SEC.section_for("/") is None and SEC.section_for("/staff") is None and SEC.section_for("/racesx") is None


def login(app, name):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": name, "password": "correct-horse-1"})
    return c


def test_admin_turns_sections_off_for_a_coach():
    from xcapp.db import SessionLocal
    from xcapp.models import StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    for name, role in (("boss", "admin"), ("coach1", "coach")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    db.commit()
    cid = db.query(StaffUser).filter_by(username="coach1").one().id
    db.close()
    app = create_app()
    admin = login(app, "boss")
    page = admin.get("/staff").text
    assert "Sections each person sees" in page and f'name="show_{cid}" value="week" checked' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    keep = [k for k, _, _ in SEC.SECTIONS if k not in ("week", "ai")]
    admin.post("/staff/sections", data={"csrf": csrf, "users": [str(cid)], f"show_{cid}": keep})
    assert "Saved the sections for coach1." in admin.get("/staff").text
    coach = login(app, "coach1")
    home = coach.get("/").text
    assert '<a href="/week"' not in home and '<a href="/ai"' not in home and '<a href="/races"' in home
    r = coach.get("/week")
    assert r.status_code == 403 and "Week is turned off for your account" in r.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', home).group(1)
    assert coach.post("/week/settings", data={"csrf": csrf, "target_days": "6", "min_minutes": "30"}).status_code == 403
    assert coach.get("/races").status_code == 200
    assert admin.get("/week").status_code == 200 and '<a href="/week"' in admin.get("/").text     # admins see everything
    admin.post("/staff/sections", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', admin.get("/staff").text).group(1),
                                        "users": [str(cid)], f"show_{cid}": [k for k, _, _ in SEC.SECTIONS]})
    assert coach.get("/week").status_code == 200

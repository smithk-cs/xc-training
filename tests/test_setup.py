"""Creating the first admin on the sign-in page with SETUP_CODE (for hosts with no command line)."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_web import csrf_of
from xcapp import cli, config
from xcapp.db import SessionLocal
from xcapp.models import StaffUser, SyncLog

CODE = "setup-code-0123456789abcdef"


def make_client(monkeypatch, code=CODE):
    if code is None:
        monkeypatch.delenv("SETUP_CODE", raising=False)
    else:
        monkeypatch.setenv("SETUP_CODE", code)
    config.reload_settings()
    from xcapp.web import create_app
    return TestClient(create_app(), follow_redirects=False)


def setup(c, code=CODE, username="headcoach", password="correct-horse-1", csrf=None):
    return c.post("/setup", data={"csrf": csrf or csrf_of(c), "setup_code": code, "username": username, "password": password})


def staff():
    db = SessionLocal()
    rows = [(u.username, u.role) for u in db.scalars(select(StaffUser))]
    db.close()
    return rows


def test_without_a_setup_code_the_command_line_is_the_only_way(monkeypatch):
    c = make_client(monkeypatch, code=None)
    page = c.get("/login").text
    assert "python -m xcapp.cli create-user" in page and 'action="/setup"' not in page
    assert setup(c).status_code == 404 and staff() == []


def test_first_admin_from_the_sign_in_page(monkeypatch):
    c = make_client(monkeypatch)
    page = c.get("/login").text
    assert "Create the first admin" in page and 'action="/setup"' in page and 'action="/login"' not in page
    assert setup(c, csrf="wrong").status_code == 400

    for kwargs, message in (({"code": "nope"}, "That setup code is not right."),
                            ({"username": "head coach!"}, "Usernames can use letters"),
                            ({"password": "short"}, "at least 10 characters")):
        r = setup(c, **kwargs)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert message in c.get("/login").text
    assert staff() == []

    r = setup(c, code=f"  {CODE} ", username="HeadCoach")
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = c.get("/")
    assert home.status_code == 200 and "Welcome, headcoach." in home.text
    assert staff() == [("headcoach", "admin")]
    db = SessionLocal()
    assert [x.message for x in db.scalars(select(SyncLog).where(SyncLog.kind == "admin"))] == [
        "First admin account headcoach created on the sign-in page."]
    db.close()

    # Once anyone exists, the form is gone and the code does nothing.
    other = TestClient(c.app, follow_redirects=False)
    page = other.get("/login").text
    assert 'action="/setup"' not in page and 'action="/login"' in page and "No staff accounts" not in page
    assert setup(other, username="intruder").status_code == 404
    assert staff() == [("headcoach", "admin")]


def test_guessing_the_code_is_rate_limited(monkeypatch):
    c = make_client(monkeypatch)
    for _ in range(5):
        setup(c, code="guess")
    setup(c)  # right code, but blocked for now
    assert "Too many attempts" in c.get("/login").text and staff() == []


def test_setup_code_setting_and_generation(monkeypatch, capsys):
    monkeypatch.setenv("SETUP_CODE", "short")
    assert any(p.startswith("SETUP_CODE") for p in config.reload_settings().problems())
    cli.main(["gen-keys"])
    line = [x for x in capsys.readouterr().out.splitlines() if x.startswith("SETUP_CODE=")][0]
    assert len(line.split("=", 1)[1]) >= 20

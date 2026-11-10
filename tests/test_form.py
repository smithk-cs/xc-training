"""Running-form analysis: the stride math (form-core.js, run by node on a made-up runner), the ratings and feedback
(form.py), saving and viewing reports, the PDF, and who can see them."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from xcapp import form as F
from xcapp import form_web as FW

ROOT = pathlib.Path(__file__).resolve().parents[1]
need_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def run_core(*args):
    out = subprocess.run(["node", str(ROOT / "tests" / "form_core_check.mjs"), *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@need_node
def test_stride_math_on_a_known_runner():
    # Made-up runner: 180 steps a minute, 240 ms on the ground, 7° forward lean, foot landing ahead of the hips
    for args in (("still", "60", "right"), ("still", "60", "left"), ("following", "60", "right")):
        r = run_core(*args)
        m = r["metrics"]
        assert m["cadence"] == 180 and m["trunk_deg"] == 7.0, args
        assert 230 <= m["contact_ms"] <= 260 and 30 <= m["duty_pct"] <= 40, args
        assert 5 <= m["overstride_deg"] <= 9, args                      # about 8° at the moment of landing
        assert r["contacts"] >= 6 and r["curves"]["knee"]["left"] == 51
    fast = run_core("still", "120", "right")["metrics"]
    assert abs(fast["contact_ms"] - 240) <= 10                           # more frames, closer timing
    slow = run_core("still", "30", "right")["metrics"]
    assert slow["contact_ms"] is None and slow["cadence"] == 180          # 30 fps: no contact time, cadence still right


def test_ratings_feedback_and_score():
    good = {"overstride_deg": 3, "shin_deg": 4, "front_knee_deg": 160, "back_knee_deg": 80, "trunk_deg": 7, "head_deg": -10,
            "elbow_deg": 90, "cadence": 180, "oscillation_pct": 8, "contact_ms": 230, "flight_ms": 100, "duty_pct": 35}
    r = F.report(good, pace_s=380)
    assert r["score"] == 100 and r["focus"] == [] and all(c["state"] == "good" for c in r["checks"])
    assert r["stride"] == {"step_m": 1.41, "stride_m": 2.82, "pace": "6:20"}
    bad = {**good, "overstride_deg": 16, "cadence": 158, "elbow_deg": 140}
    r = F.report(bad)
    states = {c["key"]: c["state"] for c in r["checks"]}
    assert states["overstride_deg"] == "high" and states["cadence"] == "low" and states["elbow_deg"] == "high"
    assert r["score"] < 100 and [c["key"] for c in r["focus"]] == ["elbow_deg", "overstride_deg", "cadence"]   # furthest off first
    over = next(c for c in r["checks"] if c["key"] == "overstride_deg")
    assert over["cue"] and over["drill"] and over["why"]
    assert F.rate("overstride_deg", 8)[0] == "good" and F.rate("overstride_deg", 16)[1] == 50.0   # 8° past, tolerance 8 → half
    assert F.rate("overstride_deg", None) == ("", None)
    every = {(k, s) for k, *_ in F.CHECKS for s in ("low", "good", "high")}
    assert every <= set(F.FEEDBACK)                                       # feedback written for every case


def test_clean_rejects_junk():
    d = F.clean({"metrics": {"cadence": 178, "trunk_deg": "x", "elbow_deg": float("nan"), "evil": 3},
                 "curves": {"knee": {"left": [1, 2, "x"] + [3] * 60}}, "figure": [[0.1, 0.2]] * 33, "fps": 60, "contacts": 9})
    assert d["metrics"]["cadence"] == 178 and d["metrics"]["trunk_deg"] is None and d["metrics"]["elbow_deg"] is None
    assert "evil" not in d["metrics"] and len(d["curves"]["knee"]["left"]) == 50 and len(d["figure"]) == 33
    assert F.clean({"metrics": {"cadence": 170}, "figure": [[0, 0]] * 5})["figure"] == []
    with pytest.raises(ValueError):
        F.clean({"metrics": {}})
    assert FW.parse_pace("6:20") == 380 and FW.parse_pace("") is None
    for bad in ("620", "2:00", "6:75"):
        with pytest.raises(ValueError):
            FW.parse_pace(bad)


def sample():
    path = ROOT / "tests" / "form_sample.json"
    return json.loads(path.read_text())


def login(app, name):
    c = TestClient(app, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": csrf, "username": name, "password": "correct-horse-1"})
    return c


def test_save_view_pdf_and_who_sees_it():
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, FormAnalysis, StaffUser
    from xcapp.security import hash_password
    from xcapp.web import create_app
    db = SessionLocal()
    for name, role in (("coach1", "coach"), ("trainer1", "trainer")):
        db.add(StaffUser(username=name, role=role, password_hash=hash_password("correct-horse-1")))
    a = Athlete(first_name="Sam", last_name="Reyes", gender="Boys", grade=11, group="Varsity", link_token="tok-sam")
    other = Athlete(first_name="Ana", last_name="Lee", gender="Boys", grade=10, group="Varsity", link_token="tok-ana")
    db.add_all([a, other])
    db.commit()
    aid, oid = a.id, other.id
    db.close()
    app = create_app()
    coach = login(app, "coach1")
    page = coach.get(f"/athletes/{aid}/form/new")
    assert page.status_code == 200 and "'wasm-unsafe-eval'" in page.headers["content-security-policy"]
    assert "connect-src 'self'" in page.headers["content-security-policy"] and "form-capture.js" in page.text
    csrf = re.search(r'"csrf": "([^"]+)"', page.text).group(1)
    body = {**sample(), "pace": "6:20", "note": "tempo pace", "csrf": csrf}
    assert coach.post(f"/athletes/{aid}/form", json={**body, "csrf": "nope"}).status_code == 400
    assert coach.post(f"/athletes/{aid}/form", json={**body, "pace": "fast"}).json()["error"].startswith("Pace looks like")
    r = coach.post(f"/athletes/{aid}/form", json=body)
    url = r.json()["url"]
    fid = int(url.rsplit("/", 1)[1])
    report = coach.get(url).text
    assert "Running form: Sam Reyes" in report and "Form score" in report and "tempo pace" in report
    assert "Foot landing ahead of the hips" in report and "Joint angles through the stride" in report and "<polyline" in report
    assert "Running form" in coach.get(f"/athletes/{aid}").text and url in coach.get(f"/athletes/{aid}").text
    pdf = coach.get(f"{url}/pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert "Running-form-Sam-Reyes" in pdf.headers["content-disposition"]
    # The athlete sees their own report on their page; not anyone else's
    athlete = TestClient(app)
    assert f"/a/tok-sam/form/{fid}/pdf" in athlete.get("/a/tok-sam").text
    assert athlete.get(f"/a/tok-sam/form/{fid}/pdf").content.startswith(b"%PDF")
    assert athlete.get(f"/a/tok-ana/form/{fid}/pdf").status_code == 404
    assert coach.get(f"/athletes/{oid}/form/{fid}").status_code == 404
    # A trainer can read reports but not film or delete
    trainer = login(app, "trainer1")
    assert trainer.get(url).status_code == 200 and trainer.get(f"/athletes/{aid}/form/new").status_code == 403
    assert TestClient(app, follow_redirects=False).get(url).status_code in (302, 303)
    # Deleting
    csrf = re.search(r'name="csrf" value="([^"]+)"', report).group(1)
    coach.post(f"{url}/delete", data={"csrf": csrf})
    db = SessionLocal()
    assert db.query(FormAnalysis).count() == 0
    db.close()

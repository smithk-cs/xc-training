"""Race Day: a live race clock, tap screens for coaches along the course, and team scores at each spot.

How it fits together:
- A board (RaceBoard) has two private codes: coaches at the course spots open /rd/<tap code>, families open
  /rw/<watch code> (clock, scores and the team's YouTube livestream; read only). Setting up a race and running the
  clock need a coach signed in to the app.
- One server clock: every page works out how far its phone's clock is from the server's, so taps and the start time
  are all in server time and splits are right even when phones disagree by seconds.
- Taps are kept on the phone first and sent whenever there's signal. Each phone sends its whole list for its spot
  (one RaceBoardLog per race, spot and phone), so sending again is always safe.
- Scoring follows the usual cross country rules (see score()); the page uses the same rules in JavaScript
  (static/raceday-score.js), and the tests check both against the same cases.
"""
from __future__ import annotations

import json
import re
import secrets
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import RaceBoard, RaceBoardLog, RaceBoardRun

OTHER = "_other"
MAX_TEAMS, MAX_SPOTS, MAX_TAPS = 16, 10, 1500
MAX_INDEX = 20     # spot numbers in use (shown and hidden); a spot keeps its number for the whole race
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def new_code() -> str:
    return secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")[:12]


def new_race_id() -> str:
    return secrets.token_hex(6)


def board_by_code(db: Session, code: str, watch: bool = False) -> RaceBoard | None:
    col = RaceBoard.watch_code if watch else RaceBoard.tap_code
    return db.scalar(select(RaceBoard).where(col == code)) if code else None


def loads(text: str, default):
    try:
        v = json.loads(text) if text else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def clean_race(data: dict) -> dict:
    """The race setup a coach sent, checked: name, teams (id, name, color) and the coach spots."""
    name = str(data.get("name") or "").strip()[:160] or "Cross Country Race"
    teams, seen = [], set()
    for t in (data.get("teams") or [])[:MAX_TEAMS]:
        if not isinstance(t, dict):
            continue
        tid, tname, color = str(t.get("id") or ""), str(t.get("name") or "").strip()[:40], str(t.get("color") or "")
        if not tname or not ID_RE.match(tid) or tid == OTHER or tid in seen:
            continue
        seen.add(tid)
        teams.append({"id": tid, "name": tname, "color": color if COLOR_RE.match(color) else "#888888"})
    raw = data.get("spots")
    if not isinstance(raw, list):   # the older form: names and pins, in order
        names, pins_in = data.get("checkpoints") or [], list(data.get("pins") or [])
        raw = [{"idx": i, "name": c, "pin": pins_in[i] if i < len(pins_in) else None} for i, c in enumerate(names)]
    spots, used = [], set()
    for x in raw:
        if not isinstance(x, dict) or not str(x.get("name") or "").strip():
            continue
        idx = x.get("idx")
        idx = idx if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < MAX_INDEX and idx not in used else None
        if idx is not None:
            used.add(idx)
        spots.append({"idx": idx, "name": str(x["name"]).strip()[:40], "pin": clean_pin(x.get("pin")),
                      "coach": str(x.get("coach") or "").strip()[:60]})
    spots = spots[:MAX_SPOTS]
    if not teams or not spots:
        raise ValueError("Add at least one team and one coach spot.")
    cid = data.get("course_id")
    url = str(data.get("stream_url") or "").strip()
    if url:
        from .analysis import youtube_url
        url = youtube_url(url)   # raises ValueError for anything but a YouTube link
    return {"name": name, "teams": teams, "spots": spots, "stream_url": url,
            "course_id": cid if isinstance(cid, int) and not isinstance(cid, bool) and cid > 0 else None}


def clean_pin(p):
    """A spot's GPS pin, {"lat", "lon"} rounded to about a metre, or None."""
    if not isinstance(p, dict):
        return None
    lat, lon = p.get("lat"), p.get("lon")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return {"lat": round(float(lat), 6), "lon": round(float(lon), 6)}


def clean_taps(taps) -> list[dict]:
    if not isinstance(taps, list) or len(taps) > MAX_TAPS:
        raise ValueError("Too many taps.")
    out = []
    for x in taps:
        if not isinstance(x, dict):
            raise ValueError("Bad tap.")
        tid, team, at = str(x.get("id") or ""), str(x.get("team") or ""), x.get("at")
        if not ID_RE.match(tid) or not (ID_RE.match(team) or team == OTHER) or not isinstance(at, (int, float)) or isinstance(at, bool):
            raise ValueError("Bad tap.")
        out.append({"id": tid, "at": int(at), "team": team})
    return out


def spot_order(checkpoints: list, order_text: str) -> list[int]:
    """The spots in the order shown (their numbers); every spot, in number order, if no order was saved."""
    order = [i for i in loads(order_text, []) if isinstance(i, int) and 0 <= i < len(checkpoints)]
    return list(dict.fromkeys(order)) if order else list(range(len(checkpoints)))


def state(db: Session, board: RaceBoard, race_id: str | None = None) -> dict:
    """What a page needs: the race (current, or an earlier one by its id) and every phone's taps for it.
    Spots are listed by number (checkpoints, pins, coaches); "order" is the order they're shown in. A spot keeps its
    number when the order changes, so taps already sent stay with the right spot; a removed spot is just left out."""
    cps = loads(board.checkpoints, [])
    race = {"race_id": board.race_id, "name": board.race_name, "teams": loads(board.teams, []),
            "checkpoints": cps, "pins": loads(board.pins, []), "coaches": loads(board.coaches, []),
            "order": spot_order(cps, board.spot_order), "start_at": board.start_at,
            "stream_url": board.stream_url, "course": course_info(db, board.course_id), "current": True}
    if race_id and race_id != board.race_id:
        run = db.scalar(select(RaceBoardRun).where(RaceBoardRun.board_id == board.id, RaceBoardRun.race_id == race_id))
        if run is not None:
            rcps = loads(run.checkpoints, [])
            race = {"race_id": run.race_id, "name": run.name, "teams": loads(run.teams, []), "checkpoints": rcps,
                    "pins": [], "coaches": [], "order": spot_order(rcps, run.spot_order), "start_at": run.start_at,
                    "stream_url": "", "course": None, "current": False}
    logs = db.scalars(select(RaceBoardLog).where(RaceBoardLog.board_id == board.id, RaceBoardLog.race_id == race["race_id"])
                      .order_by(RaceBoardLog.cp, RaceBoardLog.id)).all() if race["race_id"] else []
    return {"race": race, "logs": [{"cp": lg.cp, "device": lg.device, "coach": lg.coach, "taps": loads(lg.taps, []),
                                    "updated": lg.updated_ms} for lg in logs], "now": now_ms(), "label": board.label}


def course_info(db: Session, course_id: int | None):
    from .models import Course
    c = db.get(Course, course_id) if course_id else None
    return {"id": c.id, "name": c.name, "map_url": c.map_url, "lat": c.latitude, "lon": c.longitude} if c else None


def save_race(db: Session, board: RaceBoard, data: dict, fresh: bool) -> None:
    """Change the race's setup, or (fresh) keep the current race as an earlier one and begin the next."""
    r = clean_race(data)
    if fresh and board.race_id:
        db.add(RaceBoardRun(board_id=board.id, race_id=board.race_id, name=board.race_name, teams=board.teams,
                            checkpoints=board.checkpoints, spot_order=board.spot_order, start_at=board.start_at))
    renumber = fresh or not board.race_id
    if renumber:
        board.race_id, board.start_at = new_race_id(), None
        names, pins, coaches = [], [], []
    else:   # the same race: spots keep their numbers (phones and taps use them); new ones get the next free number
        names = loads(board.checkpoints, [])
        pins = (loads(board.pins, []) + [None] * len(names))[:len(names)]
        coaches = (loads(board.coaches, []) + [""] * len(names))[:len(names)]
    order = []
    for sp in r["spots"]:
        idx = sp["idx"] if not renumber and sp["idx"] is not None and sp["idx"] < len(names) else None
        if idx is None:
            if len(names) >= MAX_INDEX:
                raise ValueError("Too many spots for this race. Use New race, same teams to start over.")
            idx = len(names)
            names.append(""), pins.append(None), coaches.append("")
        names[idx], pins[idx], coaches[idx] = sp["name"], sp["pin"], sp["coach"]
        order.append(idx)
    board.race_name, board.teams, board.checkpoints = r["name"], json.dumps(r["teams"]), json.dumps(names)
    board.stream_url, board.pins, board.coaches, board.spot_order = r["stream_url"], json.dumps(pins), json.dumps(coaches), json.dumps(order)
    from .models import Course
    board.course_id = r["course_id"] if r["course_id"] and db.get(Course, r["course_id"]) else None
    board.updated_ms = now_ms()


def save_log(db: Session, board: RaceBoard, data: dict) -> None:
    race_id = str(data.get("race_id") or "")
    known = {board.race_id} | set(db.scalars(select(RaceBoardRun.race_id).where(RaceBoardRun.board_id == board.id)).all())
    if not race_id or race_id not in known:
        raise ValueError("That race isn't on this board any more.")
    device, cp = str(data.get("device") or ""), data.get("cp")
    if not ID_RE.match(device) or not isinstance(cp, int) or not 0 <= cp < MAX_INDEX:
        raise ValueError("Bad spot or phone.")
    taps = clean_taps(data.get("taps"))
    row = db.scalar(select(RaceBoardLog).where(RaceBoardLog.race_id == race_id, RaceBoardLog.cp == cp,
                                               RaceBoardLog.device == device))
    if row is None:
        row = RaceBoardLog(board_id=board.id, race_id=race_id, cp=cp, device=device)
        db.add(row)
    row.coach = str(data.get("coach") or "").strip()[:60] or "Coach"
    row.taps = json.dumps(taps, separators=(",", ":"))
    row.updated_ms = now_ms()


def score(taps: list[dict], teams: list[dict]) -> dict:
    """Cross country team scoring at one spot (the same rules as static/raceday-score.js):
    - only teams with at least 5 runners through are scored; places skip runners of other teams and "Other";
    - each scored team's first 7 take places, and an 8th or later runner is skipped;
    - a team's score is its first 5 places added up (low wins); the 6th and 7th only push others back;
    - ties go to the team whose 6th runner placed higher.
    Also each team's 1-to-5 gap (ms) and the running order with team places."""
    ordered = sorted(taps, key=lambda x: x["at"])
    ids = [t["id"] for t in teams]
    counts = {i: 0 for i in ids}
    for x in ordered:
        if x["team"] in counts:
            counts[x["team"]] += 1
    complete = [i for i in ids if counts[i] >= 5]
    per = {i: [] for i in complete}
    place, order = 0, []
    for n, x in enumerate(ordered, 1):
        tp = None
        if x["team"] in per and len(per[x["team"]]) < 7:
            place += 1
            tp = place
            per[x["team"]].append({"place": tp, "at": x["at"]})
        order.append({"overall": n, "at": x["at"], "team": x["team"], "tp": tp})
    standings = []
    for i in complete:
        p = per[i]
        standings.append({"id": i, "score": sum(r["place"] for r in p[:5]), "places": p,
                          "sixth": p[5]["place"] if len(p) > 5 else float("inf"), "spread": p[4]["at"] - p[0]["at"]})
    standings.sort(key=lambda s: (s["score"], s["sixth"]))
    waiting = [{"id": i, "n": counts[i]} for i in ids if i not in complete]
    return {"standings": standings, "waiting": waiting, "order": order, "total": len(ordered)}

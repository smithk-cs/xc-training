"""Race Day live switching: coaches' phones send video to a private video room on Cloudflare RealtimeKit, a signed-in
coach picks which phone is on air, and Cloudflare sends that picture (with our race clock and team scores) to the
team's YouTube stream. No laptop needed.

How it fits together:
- One RealtimeKit meeting per Race Day board (RaceBoard.live_room), made the first time someone goes live.
- Each phone joins with a token from add_participant(): camera phones send video and see no one (cheap on data);
  the director sees every camera; Cloudflare's recorder is hidden.
- The stream: Cloudflare opens our program page (/rw/<watch code>/program) in its own browser, films it, and sends
  it to YouTube over RTMP (start_stream). The program page shows whichever phone is on air (RaceBoard.on_air).
- Settings: REALTIME_ACCOUNT_ID, REALTIME_APP_ID, REALTIME_API_TOKEN (private, never in the code). The YouTube stream
  key is typed in on the Live screen and stored encrypted.
"""
from __future__ import annotations

import httpx

from .config import settings as get_settings
from .models import RaceBoard
from .security import decrypt, encrypt

YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2"
CAMERA, DIRECTOR, RECORDER = "xc_camera", "xc_director", "recorder_preset_v2"
MAX_STREAM_SECONDS = 4 * 3600      # a stream left running stops itself after 4 hours


class LiveError(Exception):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.realtime_account_id and s.realtime_app_id and s.realtime_api_token)


def api(method: str, path: str, body: dict | None = None) -> dict:
    """One call to the RealtimeKit API; returns its "data", raises LiveError with a readable message."""
    s = get_settings()
    if not configured():
        raise LiveError("Live switching isn't set up yet.")
    url = f"{s.realtime_api_base.rstrip('/')}/accounts/{s.realtime_account_id}/realtime/kit/{s.realtime_app_id}{path}"
    try:
        r = httpx.request(method, url, json=body, timeout=20, headers={"Authorization": f"Bearer {s.realtime_api_token}"})
    except httpx.HTTPError as exc:
        raise LiveError(f"Couldn't reach Cloudflare: {exc.__class__.__name__}.") from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or data.get("success") is False:
        err = data.get("error") or (data.get("errors") or [{}])[0]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise LiveError(f"Cloudflare said: {msg or r.status_code}")
    return data.get("data", data)


def _preset(name: str, *, produce: bool, see: int, recorder: bool = False) -> dict:
    """A RealtimeKit preset (a role): camera phones send video and see no one; the director sees every camera."""
    allowed = "ALLOWED" if produce else "NOT_ALLOWED"
    chat = {"can_send": False, "text": False, "files": False}
    color = {"background": {"600": "#222222", "700": "#1f1f1f", "800": "#1b1b1b", "900": "#181818", "1000": "#141414"},
             "brand": {"300": "#d97a88", "400": "#c44a5c", "500": "#a6192e", "600": "#8c1527", "700": "#731120"},
             "danger": "#ff2d2d", "success": "#62a504", "text": "#eeeeee", "text_on_brand": "#ffffff",
             "video_bg": "#191919", "warning": "#ffcd07"}
    return {
        "name": name,
        "config": {"view_type": "GROUP_CALL", "max_screenshare_count": 0, "max_video_streams": {"desktop": see, "mobile": see},
                   "media": {"video": {"quality": "hd", "frame_rate": 30, "simulcast": True},
                             "screenshare": {"quality": "hd", "frame_rate": 5}}},
        "permissions": {
            "accept_waiting_requests": False, "can_accept_production_requests": False, "can_change_participant_permissions": False,
            "can_edit_display_name": False, "can_livestream": False, "can_record": False, "can_spotlight": False,
            "chat": {"private": {**chat, "can_receive": False}, "public": chat},
            "connected_meetings": {"can_alter_connected_meetings": False, "can_switch_connected_meetings": False,
                                   "can_switch_to_parent_meeting": False},
            "disable_participant_audio": False, "disable_participant_screensharing": False, "disable_participant_video": False,
            "hidden_participant": recorder, "is_recorder": recorder, "kick_participant": False,
            "media": {"audio": {"can_produce": allowed}, "screenshare": {"can_produce": "NOT_ALLOWED"}, "video": {"can_produce": allowed}},
            "pin_participant": False,
            "plugins": {"can_close": False, "can_edit_config": False, "can_start": False, "config": {}},
            "polls": {"can_create": False, "can_view": False, "can_vote": False},
            "recorder_type": "RECORDER" if recorder else "NONE", "show_participant_list": True, "waiting_room_type": "SKIP"},
        "ui": {"design_tokens": {"border_radius": "rounded", "border_width": "thin", "colors": color, "spacing_base": 4, "theme": "dark"}},
    }


_presets_ready = False


def ensure_presets() -> None:
    global _presets_ready
    if _presets_ready:
        return
    have = api("GET", "/presets")
    names = {p.get("name") for p in (have if isinstance(have, list) else have.get("presets", []))}
    for p in (_preset(CAMERA, produce=True, see=1), _preset(DIRECTOR, produce=False, see=6),
              _preset(RECORDER, produce=False, see=6, recorder=True)):
        if p["name"] not in names:
            api("POST", "/presets", p)
    _presets_ready = True


def ensure_room(board: RaceBoard) -> str:
    if not board.live_room:
        ensure_presets()
        board.live_room = api("POST", "/meetings", {"title": board.label[:100], "session_keep_alive_time_in_secs": 600})["id"]
    return board.live_room


def join_token(board: RaceBoard, device: str, role: str, name: str) -> str:
    """A token for this phone to join the board's video room as a camera or as the director."""
    room = ensure_room(board)
    ensure_presets()
    body = {"custom_participant_id": f"{role}-{device}", "name": name[:60] or role.title(),
            "preset_name": CAMERA if role == "camera" else DIRECTOR}
    try:
        return api("POST", f"/meetings/{room}/participants", body)["token"]
    except LiveError:
        # Already added (the same phone going live again): find it and refresh its token
        people = api("GET", f"/meetings/{room}/participants")
        people = people if isinstance(people, list) else people.get("participants", [])
        for p in people:
            if p.get("custom_participant_id") == body["custom_participant_id"]:
                return api("POST", f"/meetings/{room}/participants/{p['id']}/token")["token"]
        raise


def set_key(board: RaceBoard, key: str) -> None:
    key = key.strip()
    if key and (len(key) > 100 or not all(c.isalnum() or c in "-_" for c in key)):
        raise LiveError("That doesn't look like a YouTube stream key (letters, numbers and dashes).")
    board.yt_key = encrypt(key) if key else ""


def start_stream(board: RaceBoard, program_url: str, now: int) -> None:
    """Cloudflare films the program page and sends it to YouTube."""
    if not board.yt_key:
        raise LiveError("Add your YouTube stream key first.")
    if board.live_rec:
        return
    room = ensure_room(board)
    ensure_presets()
    rec = api("POST", "/recordings", {
        "meeting_id": room, "url": program_url, "max_seconds": MAX_STREAM_SECONDS,
        "rtmp_out_config": {"rtmp_url": f"{YOUTUBE_RTMP}/{decrypt(board.yt_key)}"},
        "video_config": {"codec": "H264", "width": 1280, "height": 720},
        "file_name_prefix": "race-day"})
    board.live_rec, board.live_since = rec["id"], now


def stop_stream(board: RaceBoard) -> None:
    rec, board.live_rec, board.live_since = board.live_rec, "", None
    if rec:
        try:
            api("PUT", f"/recordings/{rec}", {"action": "stop"})
        except LiveError:
            pass   # it may have stopped already (no one in the room for a minute, or the 4-hour limit)


def status(board: RaceBoard) -> dict:
    """What every Race Day page sees about live switching (never the stream key)."""
    return {"configured": configured(), "on_air": board.on_air, "streaming": bool(board.live_rec),
            "since": board.live_since, "has_key": bool(board.yt_key)}

"""Stand-ins for Garmin and the Claude API, for end-to-end testing. Records what the app sends."""
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from fitgen import rep_workout  # noqa: E402

app = FastAPI()
LOG = {"token_forms": [], "deletes": 0, "claude": [], "file_gets": 0}
NAMES = [n for n in os.environ.get("ROSTER_NAMES", "").split("|") if n]
FIT = rep_workout(dt.datetime(2026, 10, 20, 22, 30, tzinfo=dt.timezone.utc),
                  [(1000, 200, 186, 34), (1000, 199, 188, 31), (1000, 201, 190, 27), (1000, 200, 192, 21), (1000, 198, 194, 0)],
                  rest_s=120, structured=False)


@app.get("/oauth2Confirm")
def confirm(redirect_uri: str, state: str, client_id: str, code_challenge: str, code_challenge_method: str, response_type: str):
    assert response_type == "code" and code_challenge_method == "S256"
    return RedirectResponse(redirect_uri + "?" + urlencode({"code": "fake-code", "state": state}), status_code=302)


@app.post("/di-oauth2-service/oauth/token")
async def token(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    LOG["token_forms"].append(sorted(form))
    if not form.get("client_secret") or (form.get("grant_type") == "authorization_code" and not form.get("code_verifier")):
        return JSONResponse({"error": "bad"}, status_code=400)
    return {"access_token": "AT-" + form["grant_type"], "refresh_token": "RT", "expires_in": 86400, "refresh_token_expires_in": 7775998}


def authed(request):
    return request.headers.get("authorization", "").startswith("Bearer AT-")


@app.get("/wellness-api/rest/user/id")
def user_id(request: Request):
    return {"userId": "e2e-garmin-user"} if authed(request) else Response(status_code=401)


@app.get("/wellness-api/rest/user/permissions")
def perms(request: Request):
    return ["ACTIVITY_EXPORT"] if authed(request) else Response(status_code=401)


@app.delete("/wellness-api/rest/user/registration")
def dereg(request: Request):
    LOG["deletes"] += 1
    return Response(status_code=204)


@app.get("/files/{n}")
def file(n: int, request: Request):
    if not authed(request):
        return Response(status_code=401)
    LOG["file_gets"] += 1
    return Response(FIT, media_type="application/octet-stream")


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    text = body["messages"][0]["content"]
    rec = {"api_key": request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer "), "version": request.headers.get("anthropic-version"),
           "model": body.get("model"), "names_found": [n for n in NAMES if n in text or n.split()[-1] in text], "bytes": len(text)}
    LOG["claude"].append(rec)
    data = json.loads(text.split("DATA:\n", 1)[1].split("\n\n", 1)[0])
    flagged = [a["code"] for a in data["athletes"] if a["flags"]] or [data["athletes"][0]["code"]]
    if "QUESTION:" in text:
        reply = f"{flagged[0]} has the most flags this week; {flagged[-1]} is also worth a check-in."
    else:
        reply = json.dumps({"insights": [
            {"priority": "High", "scope": [flagged[0]], "category": "Athlete check-in", "headline": "Check in today",
             "insight": f"{flagged[0]} has {data['athletes'][[a['code'] for a in data['athletes']].index(flagged[0])]['flags']}.", "action": f"Talk with {flagged[0]}."},
            {"priority": "Medium", "scope": ["Team"], "category": "Injury risk", "headline": "Team load",
             "insight": f"{data['team']['kpis']['over_now']} athletes are above 1.3 this week.", "action": "Hold mileage steady."},
            {"priority": "Low", "scope": flagged[1:3] or ["Team"], "category": "Heart rate", "headline": "Watch recovery",
             "insight": "Recovery between reps is worth watching.", "action": "Check sleep and illness."}]})
    return {"id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": len(text) // 4, "output_tokens": 300}}


@app.get("/_log")
def log():
    return LOG

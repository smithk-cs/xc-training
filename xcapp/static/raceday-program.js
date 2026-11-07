/* The live program (what goes out on YouTube): Cloudflare's recorder opens this page with ?authToken=..., joins the
   board's video room, and films it. It shows the phone the director put on air, with the race clock and team scores. */
(() => {
"use strict";
const cfg = JSON.parse(document.getElementById("rp-cfg").textContent);
const S = window.XCScore;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const OTHER = "_other";
function fmt(ms){
  if (ms == null || isNaN(ms) || ms < 0) return "0:00";
  const s = Math.floor(ms / 1000), m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}
let st = null, offset = 0, meeting = null, shownVideo = null, shownAudio = null;

async function poll(){
  try {
    const t0 = Date.now();
    const r = await fetch(cfg.api + "/state", { cache: "no-store" });
    const d = await r.json(), t1 = Date.now();
    offset = d.now - (t0 + t1) / 2;
    st = d; paintScores(); attach();
  } catch(e){}
  setTimeout(poll, 2000);
}

function team(id){ return id === OTHER ? { name: "Other", color: "#9aa39e" } : (st.race.teams || []).find(t => t.id === id) || { name: "?", color: "#888" }; }
function paintScores(){
  const race = st.race;
  $("#ovRace").textContent = race?.name || cfg.label;
  $("#slName").textContent = race?.name || "Live coverage";
  if (!race?.race_id){ return; }
  // The latest spot with team scores, from the phone with the most taps there
  let shown = null;
  const n = (race.checkpoints || []).length;
  const order = (race.order?.length ? race.order : [...Array(n).keys()]).filter(i => i < n);
  order.forEach(cp => { const name = race.checkpoints[cp];
    const logs = (st.logs || []).filter(l => l.cp === cp);
    if (!logs.length) return;
    const best = logs.reduce((a, b) => b.taps.length > a.taps.length ? b : a);
    const r = S.score(best.taps, race.teams || []);
    if (r.standings.length) shown = { name, r };
  });
  $("#ovCp").textContent = shown ? shown.name + " · Team scores" : "Team scores";
  const html = shown ? `<div class="ov-pills">${shown.r.standings.map((s, i) => { const t = team(s.id);
      return `<span class="ov-pill"><span class="pl">${i + 1}</span><span class="sq" style="background:${esc(t.color)}"></span>${esc(t.name)} <b>${s.score}</b></span>`; }).join("")}</div>`
    : `<div class="ov-wait">Scores appear once a team has 5 runners through</div>`;
  if ($("#ovBody").innerHTML !== html) $("#ovBody").innerHTML = html;
}

function onAirParticipant(){
  if (!meeting || !st?.live?.on_air) return null;
  const want = "camera-" + st.live.on_air;
  return meeting.participants.joined.toArray().find(p => p.customParticipantId === want) || null;
}
function attach(){
  const p = onAirParticipant();
  const v = $("#pv"), a = $("#pa");
  const vt = p && p.videoEnabled ? p.videoTrack : null, at = p && p.audioEnabled ? p.audioTrack : null;
  if (vt !== shownVideo){ shownVideo = vt; v.srcObject = vt ? new MediaStream([vt]) : null; if (vt) v.play().catch(() => {}); }
  if (at !== shownAudio){ shownAudio = at; a.srcObject = at ? new MediaStream([at]) : null; if (at) a.play().catch(() => {}); }
  v.hidden = !vt; $("#slate").hidden = !!vt;
  const spot = p ? String(p.name || "").split(" · ")[0] : "";
  $("#camLabel").hidden = !vt || !spot; $("#camLabel").textContent = spot;
  $("#slSub").textContent = st?.race?.start_at ? "Race under way" : "Starting soon";
}

function tick(){
  const start = st?.race?.start_at;
  const s = start && st.race.current ? fmt(Date.now() + offset - start) : "0:00";
  const el = $("#ovClock"); if (el.textContent !== s) el.textContent = s;
  requestAnimationFrame(tick);
}

async function join(){
  const authToken = new URLSearchParams(location.search).get("authToken");
  if (!authToken || !window.RealtimeKitClient) return;   // opened by hand: show the graphics only
  meeting = await RealtimeKitClient.init({ authToken, defaults: { audio: false, video: false } });
  const rec = new RecordingSDK.RealtimeKitRecording({ autoStop: true, waitTimeMs: 300000 });
  await rec.init(meeting);
  for (const ev of ["participantJoined", "participantLeft", "videoUpdate", "audioUpdate"]) meeting.participants.joined.on(ev, attach);
  await meeting.join();
  attach();
}

requestAnimationFrame(tick); poll(); join().catch(e => { $("#slSub").textContent = "Video connection problem"; console.error(e); });
})();

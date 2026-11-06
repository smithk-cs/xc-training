/* Race Day page: coach tap screen (#tap #scores #start #setup #overlay) and the family watch page.
   Taps are kept on this phone first (localStorage) and sent to the server whenever there's signal; every time is
   in the server's clock (see offset), so phones that disagree by seconds still give the right splits. */
(() => {
"use strict";
const cfg = JSON.parse(document.getElementById("rd-cfg").textContent);
const CSRF = JSON.parse(document.getElementById("rd-csrf").textContent);
const S = window.XCScore;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ls = {
  get(k, d){ try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch(e){ return d; } },
  set(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} }
};
const rid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
const OTHER = "_other";
const WATCH = cfg.mode === "watch";
const PAST = new URLSearchParams(location.search).get("race") || "";   // an earlier race, read only
function fmt(ms, tenths = true){
  if (ms == null || isNaN(ms)) return "–";
  const neg = ms < 0; ms = Math.abs(ms);
  const t = Math.floor(ms / 100), s = Math.floor(t / 10), m = Math.floor(s / 60);
  const base = (neg ? "-" : "") + m + ":" + String(s % 60).padStart(2, "0");
  return tenths ? base + "." + (t % 10) : base;
}
function textOn(hex){
  const h = (hex || "#888888").replace("#", "");
  const r = parseInt(h.slice(0,2),16), g = parseInt(h.slice(2,4),16), b = parseInt(h.slice(4,6),16);
  return (0.2126*r + 0.7152*g + 0.0722*b) / 255 > 0.6 ? "#111111" : "#ffffff";
}
function dist(m){ return m < 1000 ? Math.round(m) + " m" : (m / 1609.34).toFixed(2) + " mi"; }

// ---------- state ----------
let deviceId = ls.get("xc-device", null);
if (!deviceId){ deviceId = rid(); ls.set("xc-device", deviceId); }
let coachName = ls.get("xc-coach", "");
const cpKey = "xc-cp-" + cfg.api;
let myCp = ls.get(cpKey, null);
let race = null, remoteLogs = [], canManage = !!cfg.canManage, label = cfg.label;
let loaded = false, online = true, lastOk = 0;
let local = { raceId: null, logs: {} };
const dirty = new Set();                 // "raceId|cp" waiting to be sent
let syncing = false, syncErr = false;
let scorePick = {};
let armed = null;
const openDetails = new Set();
// Server clock: offset = server time − this phone's time, from the quickest round trip seen
let offset = ls.get("xc-offset", 0), bestRtt = Infinity;
const sNow = () => Date.now() + offset;

function loadLocal(raceId){ local = ls.get("xc-taps-" + raceId, null) || { raceId, logs: {} }; }
function saveLocal(){ if (local.raceId) ls.set("xc-taps-" + local.raceId, local); }

// ---------- server ----------
async function post(path, body){
  const r = await fetch(cfg.api + path, { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(body), credentials: "same-origin" });
  let data = {}; try { data = await r.json(); } catch(e){}
  if (!r.ok) throw new Error(data.error || data.detail || "Couldn't save (" + r.status + ").");
  return data;
}
let pollTimer = null;
async function poll(){
  clearTimeout(pollTimer);
  const t0 = Date.now();
  try {
    const r = await fetch(cfg.api + "/state" + (PAST ? "?race=" + encodeURIComponent(PAST) : ""), { credentials: "same-origin", cache: "no-store" });
    const t1 = Date.now();
    if (r.status === 404){ setSync("This link has changed", true); $("#main").innerHTML = `<div class="panel empty"><h2>Link changed</h2><p class="muted">Ask a coach for the new link.</p></div>`; return; }
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    const rtt = t1 - t0;
    if (rtt <= bestRtt * 1.5 || !loaded){
      const o = data.now - (t0 + t1) / 2;
      if (rtt < bestRtt || !loaded){ offset = Math.round(o); ls.set("xc-offset", offset); }
      bestRtt = Math.min(bestRtt, rtt);
    }
    online = true; lastOk = Date.now(); loaded = true;
    apply(data);
  } catch(e){
    online = false; updateSyncUi();
    if (!loaded){ loadedFallback(); }
  }
  const every = document.hidden ? 15000 : (WATCH ? 3000 : 2500);
  pollTimer = setTimeout(poll, every);
}
function loadedFallback(){
  // No signal on first load: keep working from what this phone saved last time
  const saved = ls.get("xc-race-" + cfg.api, null);
  if (saved && !race){ race = saved; loadLocal(race.race_id); loaded = true; render(); }
}
function apply(data){
  const next = data.race;
  if (typeof data.can_manage === "boolean") canManage = data.can_manage;
  label = data.label || label;
  const changed = (next.race_id || null) !== (race?.race_id || null);
  race = next.race_id ? next : null;
  if (race && race.current && !WATCH) ls.set("xc-race-" + cfg.api, race);
  remoteLogs = data.logs || [];
  if (changed && race){
    loadLocal(race.race_id);
    if (race.current) for (const cp of Object.keys(local.logs)) dirty.add(race.race_id + "|" + cp);
  }
  if (race && !WATCH){
    for (const d of remoteLogs){   // this phone's taps back from the server if the phone's storage was cleared
      if (d.device === deviceId && !(local.logs[d.cp]?.length) && d.taps?.length){ local.logs[d.cp] = d.taps.map(t => ({...t})); saveLocal(); }
    }
  }
  flush(); updateSyncUi(); render();
}
async function flush(){
  if (syncing || WATCH) return;
  const it = dirty.values().next(); if (it.done){ updateSyncUi(); return; }
  const key = it.value; dirty.delete(key); syncing = true; updateSyncUi();
  const [raceId, cpStr] = key.split("|"); const cp = Number(cpStr);
  const doc = raceId === local.raceId ? local : ls.get("xc-taps-" + raceId, { logs: {} });
  try {
    await post("/log", { race_id: raceId, cp, device: deviceId, coach: coachName || "Coach", taps: doc.logs[cp] || [] });
    syncErr = false;
  } catch(e){
    syncing = false;
    if (/isn't on this board/.test(e.message)){ flush(); return; }   // an old race that was deleted: drop it
    dirty.add(key); syncErr = true; updateSyncUi();
    setTimeout(flush, 4000 + Math.random() * 2000); return;
  }
  syncing = false; flush();
}
function setSync(text, bad){ const el = $("#sync"); el.textContent = text; el.classList.toggle("bad", !!bad); }
let toastUntil = 0;
function updateSyncUi(){
  if (Date.now() < toastUntil) return;
  if (!loaded) return setSync("Connecting…", false);
  const pending = dirty.size + (syncing ? 1 : 0);
  if (pending && (syncErr || !online)) return setSync("No signal: taps saved on phone", true);
  if (pending) return setSync("Sending…", false);
  if (!online) return setSync("No signal", true);
  setSync(PAST ? "Earlier race" : WATCH ? "Live · watching" : "Live", false);
}
function toast(msg, good){ setSync(msg, !good); toastUntil = Date.now() + 5000; setTimeout(updateSyncUi, 5100); }

// ---------- clock ----------
function tick(){
  const start = race?.start_at;
  const el = $("#clock"), sub = $("#clockSub");
  let html, subText;
  if (!start){ html = "0:00<small>.0</small>"; subText = race ? (race.current ? "Waiting for the start" : "This race had no start time") : (loaded ? "No race set up yet" : "Connecting…"); }
  else {
    const s = race.current ? fmt(sNow() - start) : "Final", dot = s.lastIndexOf(".");
    html = dot < 0 ? esc(s) : esc(s.slice(0, dot)) + "<small>" + esc(s.slice(dot)) + "</small>";
    subText = "Started " + new Date(start - offset).toLocaleTimeString([], {hour:"numeric", minute:"2-digit", second:"2-digit"});
  }
  if (el.innerHTML !== html) el.innerHTML = html;
  if (sub.textContent !== subText) sub.textContent = subText;
  const ov = document.getElementById("ovClock");
  if (ov){ const s = start ? fmt(sNow() - start, false) : "0:00"; if (ov.textContent !== s) ov.textContent = s; }
  if (recording){
    drawFrame();
    const cc = $("#camClock"), c = start ? fmt(sNow() - start) : "0:00.0"; if (cc.textContent !== c) cc.textContent = c;
    const rt = $("#camRecTime"), r = fmt(Date.now() - recStart, false); if (rt.textContent !== r) rt.textContent = r;
  }
  requestAnimationFrame(tick);
}

// ---------- views ----------
const VIEWS = WATCH ? ["watch"] : ["tap", "scores", "start", "setup", "overlay"];
const view = () => { const h = location.hash.slice(1); return VIEWS.includes(h) ? h : (PAST ? "scores" : VIEWS[0]); };
function teamById(id){ return id === OTHER ? { id: OTHER, name: "Other", color: "#9aa39e" } : (race?.teams || []).find(t => t.id === id) || { id, name: "?", color: "#888888" }; }
function swatch(id){ return `<span class="sw" style="background:${esc(teamById(id).color)}"></span>`; }
function signIn(what){ return `<div class="panel empty"><h2>Coaches only</h2><p class="muted">Only a coach signed in to the team app can ${what}.</p><a class="btn primary" href="/login">Sign in</a></div>`; }
function noRace(){
  if (WATCH) return `<div class="panel empty"><h2>No race yet</h2><p class="muted">The clock and scores appear here once the coaches set up the race.</p></div>`;
  return `<div class="panel empty"><h2>No race yet</h2><p class="muted">Add the teams' jersey colors and the spots on the course where coaches will stand.</p>
    ${canManage ? `<a class="btn primary" href="#setup">Set up a race</a>` : `<p class="muted">A coach signed in to the team app sets up the race. This page fills in on its own.</p>`}</div>`;
}
function pastBanner(){
  return PAST && race && !race.current ? `<div class="banner"><span><b>Earlier race.</b> ${esc(race.name)}. Read only.</span><a class="btn" href="${esc(location.pathname)}">Go to the current race</a></div>` : "";
}

function renderTap(){
  if (!race) return noRace();
  if (!race.current) return pastBanner();
  const cps = race.checkpoints || [];
  if (myCp == null || myCp >= cps.length){
    const near = nearestSpot();
    return `<div class="panel"><h2>Where are you standing?</h2>
      <label>Your name<input id="coachName" type="text" autocomplete="name" placeholder="Coach Smith" value="${esc(coachName)}"></label>
      <div class="pads">${cps.map((c, i) => { const d = spotDist(i);
        return `<button class="pad other${near && near.i === i && near.d < 200 ? " near" : ""}" data-cp="${i}"><span class="pn">${esc(c)}</span>${d != null ? `<span class="pd">${dist(d)} away${near && near.i === i ? " · nearest" : ""}</span>` : ""}</button>`; }).join("")}</div>
      ${pinned() ? `<p class="small muted">${gpsPos ? "Distances use this phone's location." : gpsErr ? esc(gpsErr) : "Finding your location…"}</p>` : ""}</div>`;
  }
  const mine = local.logs[myCp] || [];
  const counts = {}; for (const x of mine) counts[x.team] = (counts[x.team] || 0) + 1;
  const pads = (race.teams || []).map(t => `<button class="pad" data-team="${esc(t.id)}" style="background:${esc(t.color)};color:${textOn(t.color)}">
      <span class="pn">${esc(t.name)}</span><span class="pc">${counts[t.id] || 0} through</span></button>`).join("")
    + `<button class="pad other" data-team="${OTHER}"><span class="pn">Other</span><span class="pc">${counts[OTHER] || 0} · not scored</span></button>`;
  const sorted = [...mine].sort((a, b) => a.at - b.at);
  const recent = sorted.map((x, i) => ({ x, place: i + 1 })).reverse().slice(0, 8).map(({ x, place }) =>
    `<div class="tap"><span class="muted">#${place}</span>${swatch(x.team)}<span>${esc(teamById(x.team).name)}</span>
      <span class="tm">${race.start_at ? fmt(x.at - race.start_at) : "before start"}</span><button class="x" data-del="${esc(x.id)}" aria-label="Remove this tap">Remove</button></div>`).join("");
  return `${!race.start_at ? `<div class="banner"><span><b>Clock not started.</b> Tap anyway; times fill in once the starter presses Start.</span></div>` : ""}
    <div class="cpbar"><span class="cpname">${esc(cps[myCp])}</span><button id="changeCp">Change spot</button></div>
    <div id="gps"></div>
    <div class="pads">${pads}</div>
    <div class="panel"><div class="row" style="justify-content:space-between"><h3>Last taps at ${esc(cps[myCp])} · ${mine.length} total</h3>${mine.length ? `<button id="undo">Undo last</button>` : ""}</div>
      ${recent ? `<div class="recent">${recent}</div>` : `<p class="muted">Tap a jersey color each time a runner passes you. The time is taken at the moment you tap.</p>`}</div>
    ${camStream ? "" : `<div class="panel"><h3>Video</h3><p class="muted small">Record the race with the clock on the picture, then save it for YouTube. Keep this screen open while recording.</p><div><button id="camOn">Turn on camera</button></div></div>`}`;
}

function logsForCp(cp){
  const mineHere = !WATCH && race?.current ? local.logs[cp] : null;
  const out = remoteLogs.filter(d => d.cp === cp && (WATCH || !race?.current || d.device !== deviceId))
    .map(d => ({ id: d.device, coach: d.coach + (d.device === deviceId ? " (this phone)" : ""), taps: d.taps || [] }));
  if (mineHere?.length) out.unshift({ id: deviceId, coach: (coachName || "You") + " (this phone)", taps: mineHere });
  return out;
}
function pickLog(cp, logs){ return logs.find(l => l.id === scorePick[cp]) || logs.reduce((a, b) => b.taps.length > a.taps.length ? b : a); }
function scoreBlocks(){
  const teams = race.teams || [];
  return (race.checkpoints || []).map((name, cp) => {
    const logs = logsForCp(cp);
    if (!logs.length) return `<div class="panel"><h2>${esc(name)}</h2><p class="muted">No taps yet from this spot.</p></div>`;
    const pick = pickLog(cp, logs);
    const r = S.score(pick.taps, teams);
    const chips = logs.length > 1
      ? `<p class="small muted">${logs.length} phones logged this spot. Showing:</p><div class="chips">${logs.map(l => `<button class="chip" data-pick="${cp}|${esc(l.id)}" aria-pressed="${l === pick}">${esc(l.coach)} · ${l.taps.length}</button>`).join("")}</div>`
      : `<p class="muted small">From ${esc(pick.coach)} · ${r.total} runner${r.total === 1 ? "" : "s"} through</p>`;
    const rows = r.standings.map((s, i) => {
      const places = s.places.map((p, k) => k < 5 ? p.place : `<s>${p.place}</s>`).join(" · ");
      return `<tr><td class="big">${i + 1}</td><td><span class="teamcell">${swatch(s.id)}${esc(teamById(s.id).name)}</span></td><td class="big">${s.score}</td>
        <td class="pl"><span class="places">${places}</span></td><td>${fmt(s.spread, false)}</td></tr>`;
    }).join("");
    const waiting = r.waiting.length ? `<p class="muted small">Not scored yet (fewer than 5 through): ${r.waiting.map(w => `${esc(teamById(w.id).name)} ${w.n}`).join(", ")}</p>` : "";
    const key = "order" + cp;
    const order = race.start_at && r.order.length ? `<details data-key="${key}"${openDetails.has(key) ? " open" : ""}><summary class="muted">Running order with times</summary><div class="order" style="margin-top:8px">${r.order.map(o =>
      `<div class="${o.tp ? "" : "np"}"><span>${o.overall}.</span>${swatch(o.team)}<span>${fmt(o.at - race.start_at)}</span>${o.tp ? `<span class="muted">(${o.tp})</span>` : ""}</div>`).join("")}</div></details>` : "";
    return `<div class="panel"><h2>${esc(name)}</h2>${chips}
      ${rows ? `<div class="tablewrap"><table><thead><tr><th></th><th>Team</th><th>Score</th><th>Places (6th, 7th)</th><th>1–5 gap</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">No team has 5 runners through yet.</p>`}
      ${waiting}${order}</div>`;
  }).join("");
}
const RULES = `<details class="panel" data-key="rules"><summary><b>How scores are counted</b></summary><ul class="rules">
    <li>Only teams with at least 5 runners through are scored. Places skip runners from other teams and "Other".</li>
    <li>A team's top 5 places add up to its score. Low score wins.</li>
    <li>The 6th and 7th runners (in parentheses) push other teams' places back but don't add to the score. An 8th runner is skipped.</li>
    <li>Ties go to the team whose 6th runner placed higher.</li>
    <li>The 1–5 gap is the time between a team's 1st and 5th runners.</li></ul></details>`;
function renderScores(){
  if (!race) return noRace();
  return pastBanner() + scoreBlocks() + (race.current ? `<div class="panel"><h3>Put the clock and scores on the livestream</h3>
    <p class="muted small">Opens a full-screen scoreboard with the race clock and the latest team scores. In OBS, capture that window and key out the green.</p>
    <div><a class="btn primary" href="#overlay">Open livestream scoreboard</a></div></div>` : "") + RULES;
}

function renderStart(){
  if (!canManage) return signIn("start the race clock");
  if (!race) return noRace();
  if (!race.current) return pastBanner();
  if (!race.start_at){
    return `<div class="panel"><h2>Start the clock</h2><p class="muted">Press at the gun. Every coach's phone starts counting from this moment.</p>
      <button class="startbtn" id="go">START</button></div>`;
  }
  return `<div class="panel"><h2>Race is running</h2>
      <p class="muted">Pressed Start late or early? Shift the start time. Every split already tapped moves with it.</p>
      <div class="nudges"><button data-nudge="-5000">−5 s</button><button data-nudge="-1000">−1 s</button><button data-nudge="1000">+1 s</button><button data-nudge="5000">+5 s</button></div></div>
    <div class="panel"><h3>False start or test run?</h3><p class="muted">Clears the clock. Taps stay but have no time until Start is pressed again.</p>
      <button class="danger ${armed === "reset" ? "armed" : ""}" id="resetClock">${armed === "reset" ? "Tap again to clear the clock" : "Clear the clock"}</button></div>`;
}

function pinHtml(p){
  return p ? `<span class="ok">Pinned</span> <a href="https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}" target="_blank" rel="noopener">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</a> <button type="button" data-unpin>Clear pin</button>`
           : `<span class="muted">No GPS pin</span>`;
}
function spotRow(name, pin){
  return `<div class="spotrow" data-pin="${esc(pin ? JSON.stringify(pin) : "")}">
    <div class="top"><input type="text" class="spotname" placeholder="Such as 1 Mile" value="${esc(name)}" aria-label="Spot name"><button type="button" class="x danger" data-rmspot aria-label="Remove spot">✕</button></div>
    <div class="pinline"><span class="pinstate">${pinHtml(pin)}</span>
      <button type="button" data-pinhere>Pin this spot here</button>
      <input type="text" data-pinpaste placeholder="Or paste a Google Maps link or coordinates" aria-label="Google Maps link or coordinates"></div></div>`;
}
function renderSetup(){
  if (!loaded) return `<p class="muted">Loading…</p>`;
  if (!canManage) return signIn("set up races");
  const r = race && race.current ? race : { name: "", teams: [], checkpoints: ["1 Mile", "2 Mile", "Finish"], pins: [], stream_url: "", course: null };
  const teams = r.teams?.length ? r.teams : [{ id: "t" + rid(), name: "", color: "#a6192e" }, { id: "t" + rid(), name: "", color: "#1b4fa0" }];
  const courses = cfg.courses || [];
  const cid = r.course?.id || "";
  const mapUrl = (courses.find(c => c.id === cid) || {}).map_url || "";
  return `<form id="setupForm" class="panel" onsubmit="return false"><h2>${race ? "Race setup" : "New race"}</h2>
    <label>Race name<input id="fName" type="text" maxlength="160" placeholder="Such as Clovis Invitational – Varsity Boys" value="${esc(r.name)}"></label>
    <label>Course (optional)<select id="fCourse"><option value="">None</option>${courses.map(c => `<option value="${c.id}"${c.id === cid ? " selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
    <p class="small" id="courseMap">${mapUrl ? `<a href="${esc(mapUrl)}" target="_blank" rel="noopener">Open the course map</a> to find the mile markers.` : ""}</p>
    <label>Team YouTube livestream link (optional, shown to families)<input id="fStream" type="url" maxlength="300" placeholder="https://youtube.com/live/…" value="${esc(r.stream_url || "")}"></label></form>
  <div class="panel"><h2>Teams</h2><p class="muted">Pick each team's main jersey color. It becomes that team's tap button.</p>
    <div id="teamList" style="display:grid;gap:8px">${teams.map(t => `<div class="teamrow" data-id="${esc(t.id)}">
      <input type="color" value="${esc(t.color)}" aria-label="Jersey color"><input type="text" maxlength="40" placeholder="School name" value="${esc(t.name)}" aria-label="School name">
      <button type="button" class="x danger" data-rmteam aria-label="Remove team">✕</button></div>`).join("")}</div>
    <div><button id="addTeam">Add a team</button></div></div>
  <div class="panel"><h2>Coach spots</h2><p class="muted">In course order, finish last. Pin each spot so phones can check their coach is in the right place: stand at the marker on a course walk and tap <b>Pin this spot here</b>, or paste a Google Maps link (long-press the spot in Google Maps, then share or copy).</p>
    <div id="spotList">${(r.checkpoints || []).map((c, i) => spotRow(c, (r.pins || [])[i] || null)).join("")}</div>
    <div><button id="addSpot">Add a spot</button></div></div>
  <div class="row"><button class="primary" id="saveSetup">${race ? "Save changes" : "Create race"}</button>
    ${race ? `<button class="danger ${armed === "newrace" ? "armed" : ""}" id="newRace">${armed === "newrace" ? "Tap again: start the next race" : "New race, same teams"}</button>` : ""}</div>
  ${race ? `<p class="muted small">"New race, same teams" keeps this race's results (under Earlier races on the Race Day page) and starts the next one with an empty clock and no taps.</p>` : ""}`;
}

// ---------- family watch page ----------
function ytId(url){
  if (!url) return null;
  try {
    const u = new URL(url);
    if (u.hostname === "youtu.be") return u.pathname.slice(1).split("/")[0] || null;
    if (u.searchParams.get("v")) return u.searchParams.get("v");
    const m = u.pathname.match(/^\/(?:live|embed|shorts)\/([A-Za-z0-9_-]{6,})/);
    return m ? m[1] : null;
  } catch(e){ return null; }
}
let streamShown = null;
function showStream(){
  let box = document.getElementById("stream");
  const url = WATCH && race?.current ? race.stream_url : "";
  const key = url || "";
  if (key === streamShown) return;
  streamShown = key;
  if (!url){ if (box) box.remove(); return; }
  if (!box){ box = document.createElement("section"); box.id = "stream"; box.className = "wrap"; box.style.paddingTop = "12px"; $("#main").before(box); }
  const id = ytId(url);
  box.innerHTML = id && /^[A-Za-z0-9_-]+$/.test(id)
    ? `<div class="stream"><iframe src="https://www.youtube-nocookie.com/embed/${id}?autoplay=1&mute=1&playsinline=1" title="Team livestream" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>`
    : `<div class="panel"><a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener">Watch the team livestream on YouTube</a></div>`;
}
function renderWatch(){
  if (!race) return noRace();
  return (race.start_at ? "" : `<div class="banner"><span><b>Waiting for the start.</b> The clock starts at the gun and team scores show as runners pass the coaches.</span></div>`)
    + scoreBlocks() + RULES;
}

// ---------- livestream scoreboard ----------
let ovCp = ls.get("xc-ovcp", "auto"), ovBg = ls.get("xc-ovbg", "green");
let ovHidden = false, ovTimer = null;
function ovPoke(){
  if (view() !== "overlay") return;
  if (ovHidden){ ovHidden = false; $("#ovCtl")?.classList.remove("hide"); }
  clearTimeout(ovTimer);
  ovTimer = setTimeout(() => { ovHidden = true; $("#ovCtl")?.classList.add("hide"); }, 3000);
}
function renderOverlay(){
  const cps = race?.checkpoints || [], teams = race?.teams || [];
  const results = cps.map((name, cp) => { const logs = logsForCp(cp); return logs.length ? { name, r: S.score(pickLog(cp, logs).taps, teams) } : null; });
  let shown = null;
  if (ovCp === "auto"){ for (let i = results.length - 1; i >= 0; i--) if (results[i]?.r.standings.length){ shown = results[i]; break; } }
  else shown = results[Number(ovCp)] || { name: cps[Number(ovCp)] || "", r: { standings: [] } };
  const pills = (shown?.r.standings || []).map((s, i) => { const t = teamById(s.id);
    return `<span class="ov-pill"><span class="pl">${i + 1}</span><span class="sq" style="background:${esc(t.color)}"></span>${esc(t.name)} <b>${s.score}</b></span>`; }).join("");
  const bg = ovBg === "black" ? "#000000" : "#00b140";
  document.body.style.background = bg;
  return `<div class="ov-stage" style="background:${bg}">
    <div class="ov-ctl ${ovHidden ? "hide" : ""}" id="ovCtl">
      <button data-ovcp="auto" aria-pressed="${ovCp === "auto"}">Latest spot</button>
      ${cps.map((c, i) => `<button data-ovcp="${i}" aria-pressed="${String(ovCp) === String(i)}">${esc(c)}</button>`).join("")}
      <button id="ovBg">${ovBg === "black" ? "Black background" : "Green background"}</button>
      <a class="btn" href="#scores">Close</a>
      <span class="tip">These buttons hide after 3 seconds and come back when you move the mouse.</span>
    </div>
    <div class="ov-third">
      <div class="ov-clockbox"><div class="ov-race">${esc(race?.name || label)}</div><div class="ov-clock" id="ovClock">0:00</div></div>
      <div class="ov-scores"><div class="ov-cp">${shown ? esc(shown.name) + " · " : ""}Team scores</div>
        ${pills ? `<div class="ov-pills">${pills}</div>` : `<div class="ov-wait">Scores appear once a team has 5 runners through</div>`}</div>
    </div></div>`;
}

// ---------- render ----------
let lastHtml = "";
function render(force){
  const v = view();
  document.body.classList.toggle("ov", v === "overlay");
  if (v !== "overlay") document.body.style.background = "";
  document.querySelectorAll(".views a").forEach(a => a.dataset.v === v ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  $("#raceName").textContent = race?.name || label;
  $("#cam").hidden = !(camStream && v === "tap");
  if (WATCH) showStream();
  if (v === "setup" && !force && $("#setupForm")) return;   // never wipe a form being filled in
  const html = v === "overlay" ? renderOverlay()
    : ({ tap: renderTap, scores: renderScores, start: renderStart, setup: renderSetup, watch: renderWatch }[v])();
  if (html !== lastHtml || force){ $("#main").innerHTML = html; lastHtml = html; }
  if (v === "tap") updateGps();
  renderCamPads();
  gpsWanted(v === "tap" && race?.current && pinned());
}

// ---------- GPS spot check ----------
let gpsPos = null, gpsErr = "", gpsWatch = null;
const pinned = () => (race?.pins || []).some(Boolean);
function spotDist(i){ const p = (race?.pins || [])[i]; return p && gpsPos ? S.metres(gpsPos, p) : null; }
function nearestSpot(){
  let best = null;
  (race?.pins || []).forEach((p, i) => { if (p && gpsPos){ const d = S.metres(gpsPos, p); if (!best || d < best.d) best = { i, d }; } });
  return best;
}
function gpsWanted(on){
  if (!("geolocation" in navigator)) { gpsErr = "This phone can't share its location."; return; }
  if (on && gpsWatch == null){
    gpsWatch = navigator.geolocation.watchPosition(p => {
      gpsPos = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy };
      gpsErr = ""; if (myCp == null && view() === "tap") render(); else updateGps();
    }, e => { gpsErr = e.code === 1 ? "Location is off for this page. Allow it in the browser's settings to check your spot." : "Can't find this phone's location yet."; updateGps(); },
    { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 });
  } else if (!on && gpsWatch != null){ navigator.geolocation.clearWatch(gpsWatch); gpsWatch = null; }
}
function miniMap(myPin){
  const pts = (race.pins || []).map((p, i) => p && { ...p, i }).filter(Boolean);
  const all = gpsPos ? [...pts, gpsPos] : pts;
  if (!all.length) return "";
  const k = Math.cos((all[0].lat * Math.PI) / 180);
  const xs = all.map(p => p.lon * k), ys = all.map(p => p.lat);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const span = Math.max(maxX - minX, maxY - minY, 0.0005);
  const W = 120, H = 90, pad = 16, sc = Math.min((W - 2 * pad) / span, (H - 2 * pad) / span);
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const X = p => (W / 2 + (p.lon * k - cx) * sc).toFixed(1), Y = p => (H / 2 - (p.lat - cy) * sc).toFixed(1);
  const dots = pts.map(p => `<circle cx="${X(p)}" cy="${Y(p)}" r="${p.i === myCp ? 6 : 4}" fill="${p.i === myCp ? "var(--accent)" : "var(--muted)"}"/><text x="${X(p)}" y="${Y(p) < 22 ? +Y(p) + 15 : Y(p) - 7}" font-size="9" text-anchor="middle" fill="currentColor">${esc(String((race.checkpoints[p.i] || "").slice(0, 8)))}</text>`).join("");
  const me = gpsPos ? `<circle cx="${X(gpsPos)}" cy="${Y(gpsPos)}" r="5" fill="#2a7de1" stroke="#fff" stroke-width="2"/>` : "";
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Sketch of the pinned spots and this phone (blue)">${dots}${me}</svg>`;
}
function updateGps(){
  const box = document.getElementById("gps"); if (!box || !race || myCp == null) return;
  const pin = (race.pins || [])[myCp];
  const name = esc((race.checkpoints || [])[myCp] || "");
  if (!pin){
    box.className = ""; box.innerHTML = canManage ? `<p class="small muted">No GPS pin for this spot. <button id="pinMine" class="x">Pin this spot here</button></p>` : "";
    return;
  }
  if (!gpsPos){ box.className = "gps"; box.innerHTML = `<b>Checking your spot…</b><span class="muted small">${esc(gpsErr || "Finding this phone's location.")}</span>`; return; }
  const d = S.metres(gpsPos, pin), acc = gpsPos.acc || 0;
  const okR = Math.max(35, Math.min(acc, 60));
  const near = nearestSpot();
  let cls, head, more = "";
  if (d <= okR){ cls = "ok"; head = `On spot · ${dist(d)} from ${name}`; }
  else if (d <= 150){ cls = "near"; head = `Close · ${dist(d)} from ${name}`; more = "Your spot is the red dot on the sketch; you are blue."; }
  else { cls = "far"; head = `Not at ${name} · ${dist(d)} away`; more = "Your spot is the red dot on the sketch; you are blue."; }
  const other = near && near.i !== myCp && near.d <= Math.max(okR, 60) ? near : null;
  if (other) more = `You look to be at <b>${esc(race.checkpoints[other.i])}</b> (${dist(other.d)}).`;
  box.className = "gps " + cls;
  box.innerHTML = `<b>${head}</b>${miniMap(pin)}<span class="small muted">${more || "GPS within about " + Math.round(acc) + " m."}</span>
    <span class="acts">${other ? `<button data-switch="${other.i}">Switch to ${esc(race.checkpoints[other.i])}</button>` : ""}<a class="btn" style="min-height:36px;padding:4px 10px;font-size:14px" href="https://www.google.com/maps/dir/?api=1&destination=${pin.lat},${pin.lon}&travelmode=walking" target="_blank" rel="noopener">Directions</a></span>`;
}
function hereNow(){
  return new Promise((ok, bad) => {
    if (!("geolocation" in navigator)) return bad(new Error("This phone can't share its location."));
    navigator.geolocation.getCurrentPosition(p => ok(p.coords), e => bad(new Error(e.code === 1 ? "Location is off for this page. Allow it in the browser's settings." : "Couldn't get a GPS fix. Try again in the open.")),
      { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 });
  });
}

// ---------- camera ----------
let camStream = null, camFacing = "environment", recorder = null, recording = false, recChunks = [], recStart = 0, lastTapFlash = null;
const canvas = () => document.getElementById("camCanvas");
async function camStart(){
  if (!navigator.mediaDevices?.getUserMedia){ toast("This browser can't use the camera."); return; }
  camStop();
  const video = { facingMode: { ideal: camFacing }, width: { ideal: 1280 }, height: { ideal: 720 } };
  try { camStream = await navigator.mediaDevices.getUserMedia({ video, audio: true }); }
  catch(e){
    try { camStream = await navigator.mediaDevices.getUserMedia({ video, audio: false }); }
    catch(e2){ camStream = null; toast("Camera blocked. Allow it in the browser's settings."); render(true); return; }
  }
  const v = $("#camVideo"); v.srcObject = camStream; try { await v.play(); } catch(e){}
  wake(); render(true);
}
function camStop(){
  if (recording) recStop();
  if (camStream){ camStream.getTracks().forEach(t => t.stop()); camStream = null; }
  const v = $("#camVideo"); if (v) v.srcObject = null;
}
function pickMime(){
  if (typeof MediaRecorder === "undefined") return null;
  for (const m of ["video/mp4;codecs=avc1,mp4a", "video/mp4", "video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"])
    if (MediaRecorder.isTypeSupported(m)) return m;
  return "";
}
function drawFrame(){
  const v = $("#camVideo"), c = canvas();
  if (!v.videoWidth) return;
  if (c.width !== v.videoWidth){ c.width = v.videoWidth; c.height = v.videoHeight; }
  const g = c.getContext("2d"), W = c.width, H = c.height, u = H / 720;
  g.drawImage(v, 0, 0, W, H);
  // Race clock box, lower left
  const clock = race?.start_at ? fmt(sNow() - race.start_at) : "0:00.0";
  const top = (race?.name || label).slice(0, 48), spot = race && myCp != null ? (race.checkpoints[myCp] || "") : "";
  g.font = `700 ${Math.round(64 * u)}px "Barlow Semi Condensed", "Arial Narrow", sans-serif`;
  const cw = Math.max(g.measureText(clock).width, 200 * u);
  g.font = `600 ${Math.round(20 * u)}px Inter, sans-serif`;
  const tw = Math.max(g.measureText(top).width, g.measureText(spot).width);
  const bw = Math.max(cw, tw) + 40 * u, bh = 130 * u, bx = 24 * u, by = H - bh - 24 * u;
  g.fillStyle = "rgba(20,24,27,.82)"; g.fillRect(bx, by, bw, bh);
  g.fillStyle = "#a6192e"; g.fillRect(bx, by, 8 * u, bh);
  g.fillStyle = "#ffffff"; g.textBaseline = "top";
  g.fillText(top, bx + 22 * u, by + 12 * u);
  g.fillStyle = "#f3d58a"; g.fillText(spot.toUpperCase(), bx + 22 * u, by + 36 * u);
  g.fillStyle = "#ffffff"; g.font = `700 ${Math.round(64 * u)}px "Barlow Semi Condensed", "Arial Narrow", sans-serif`;
  g.fillText(clock, bx + 22 * u, by + 58 * u);
  // A tap shows for a moment: team color band, top
  if (lastTapFlash && Date.now() - lastTapFlash.t < 1500){
    const t = teamById(lastTapFlash.team);
    g.fillStyle = t.color; g.fillRect(0, 0, W, 54 * u);
    g.fillStyle = textOn(t.color); g.font = `700 ${Math.round(34 * u)}px "Barlow Semi Condensed", "Arial Narrow", sans-serif`;
    g.fillText(`${t.name.toUpperCase()} · #${lastTapFlash.n} · ${lastTapFlash.time}`, 24 * u, 10 * u);
  }
  const rt = $("#recTime"); const s = fmt(Date.now() - recStart, false); if (rt.textContent !== s) rt.textContent = s;
}
function recStart_(){
  if (!camStream) return;
  const mime = pickMime();
  if (mime === null || !canvas().captureStream){ toast("This browser can't record video. Use the phone's camera app."); return; }
  drawFrame();
  const out = canvas().captureStream(30);
  camStream.getAudioTracks().forEach(t => out.addTrack(t));
  try { recorder = new MediaRecorder(out, mime ? { mimeType: mime, videoBitsPerSecond: 2500000 } : { videoBitsPerSecond: 2500000 }); }
  catch(e){ toast("Couldn't start recording."); return; }
  recChunks = [];
  recorder.ondataavailable = e => { if (e.data && e.data.size) recChunks.push(e.data); };
  recorder.onstop = () => saveRecording(recorder.mimeType || mime || "video/webm");
  recorder.start(1000);
  recording = true; recStart = Date.now();
  document.body.classList.add("recfull"); camPadsHtml = ""; renderCamPads();
  $("#recDot").hidden = false; $("#recBtn").textContent = "Stop recording"; $("#camFlip").disabled = true;
  wake();
}
function recStop(){
  if (!recording) return;
  recording = false;
  try { recorder.stop(); } catch(e){}
  document.body.classList.remove("recfull");
  $("#recDot").hidden = true; $("#recBtn").textContent = "Start recording"; $("#camFlip").disabled = false;
}
function saveRecording(type){
  const blob = new Blob(recChunks, { type }); recChunks = [];
  const ext = type.includes("mp4") ? "mp4" : "webm";
  const spot = race && myCp != null ? race.checkpoints[myCp] : "race";
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
  const name = `${(race?.name || label)}-${spot}-${stamp}`.replace(/[^A-Za-z0-9-]+/g, "-").replace(/-+/g, "-") + "." + ext;
  const url = URL.createObjectURL(blob);
  const file = typeof File !== "undefined" ? new File([blob], name, { type }) : null;
  const canShare = file && navigator.canShare && navigator.canShare({ files: [file] });
  const mb = blob.size < 1048576 ? "under 1" : (blob.size / 1048576).toFixed(0);
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const el = document.createElement("div"); el.className = "saved panel";
  el.innerHTML = `<span><b>Video ready</b> · ${mb} MB${ext === "webm" ? " (WebM: YouTube takes it as is)" : ""}</span>`
    + (canShare ? `<button class="primary" data-share>${ios ? "Save to Photos" : "Share or save…"}</button><a class="btn" href="${url}" download="${esc(name)}">Save as a file</a>`
                : `<a class="btn primary" href="${url}" download="${esc(name)}">Save video</a>`)
    + (canShare && ios ? `<span class="savehint">Tap Save to Photos, then choose <b>Save Video</b>. It goes to your camera roll.</span>` : "");
  if (canShare) el.querySelector("[data-share]").addEventListener("click", () => navigator.share({ files: [file], title: name }).catch(() => {}));
  $("#camSaved").prepend(el);
  toast(canShare && ios ? "Video ready. Tap Save to Photos before closing this page." : "Video ready. Save it before closing this page.", true);
}

let camPadsHtml = "";
function renderCamPads(){
  if (!recording) return;
  const cps = race?.checkpoints || [];
  $("#camSpot").textContent = myCp != null && cps[myCp] ? cps[myCp] : "No spot";
  let html;
  if (!race?.current || myCp == null || myCp >= cps.length) html = `<div class="nospot">Pick your spot on the Tap screen to tap runners while recording.</div>`;
  else {
    const mine = local.logs[myCp] || [], counts = {};
    for (const x of mine) counts[x.team] = (counts[x.team] || 0) + 1;
    html = (race.teams || []).map(t => `<button class="pad" data-team="${esc(t.id)}" style="background:${esc(t.color)};color:${textOn(t.color)}"><span class="pn">${esc(t.name)}</span><span class="pc">${counts[t.id] || 0}</span></button>`).join("")
      + `<button class="pad other" data-team="${OTHER}"><span class="pn">Other</span><span class="pc">${counts[OTHER] || 0}</span></button>`;
  }
  if (html !== camPadsHtml){ $("#camPads").innerHTML = html; camPadsHtml = html; }
}

// ---------- actions ----------
let wakeLock = null;
async function wake(){ try { if (!wakeLock && navigator.wakeLock) { wakeLock = await navigator.wakeLock.request("screen"); wakeLock.addEventListener("release", () => { wakeLock = null; }); } } catch(e){} }
function addTap(team){
  if (!race?.current || myCp == null) return;
  const at = sNow();
  (local.logs[myCp] ||= []).push({ id: rid(), at, team });
  saveLocal(); dirty.add(local.raceId + "|" + myCp); flush(); render();
  if (recording){ const n = local.logs[myCp].length; lastTapFlash = { team, t: Date.now(), n, time: race.start_at ? fmt(at - race.start_at) : "" }; }
  document.querySelectorAll(`.pad[data-team="${CSS.escape(team)}"]`).forEach(el => { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 180); });
  try { navigator.vibrate?.(30); } catch(e){}
}
function delTap(id){
  const arr = local.logs[myCp] || []; const i = arr.findIndex(x => x.id === id);
  if (i >= 0){ arr.splice(i, 1); saveLocal(); dirty.add(local.raceId + "|" + myCp); flush(); render(); }
}
function arm(name){ armed = name; render(true); setTimeout(() => { if (armed === name){ armed = null; render(true); } }, 4000); }
async function clock(body){
  try { const d = await post("/clock", { ...body, csrf: CSRF }); race.start_at = d.start_at; render(); poll(); }
  catch(e){ toast(e.message); poll(); }
}
function setPin(row, pin){
  row.dataset.pin = pin ? JSON.stringify(pin) : "";
  row.querySelector(".pinstate").innerHTML = pinHtml(pin);
}
function readSetup(){
  const teams = [...document.querySelectorAll(".teamrow")].map(r => ({ id: r.dataset.id, color: r.querySelector("input[type=color]").value, name: r.querySelector("input[type=text]").value.trim() })).filter(x => x.name);
  const rows = [...document.querySelectorAll(".spotrow")].filter(r => r.querySelector(".spotname").value.trim());
  return { name: $("#fName").value.trim(), teams, checkpoints: rows.map(r => r.querySelector(".spotname").value.trim()),
           pins: rows.map(r => r.dataset.pin ? JSON.parse(r.dataset.pin) : null),
           course_id: Number($("#fCourse").value) || null, stream_url: $("#fStream").value.trim() };
}

document.addEventListener("pointerdown", e => {
  const pad = e.target.closest(".pad[data-team]");
  if (pad){ e.preventDefault(); addTap(pad.dataset.team); }
});
document.addEventListener("toggle", e => { const k = e.target.dataset?.key; if (k){ e.target.open ? openDetails.add(k) : openDetails.delete(k); lastHtml = ""; } }, true);
document.addEventListener("change", e => {
  if (e.target.id === "fCourse"){
    const c = (cfg.courses || []).find(x => x.id === Number(e.target.value));
    $("#courseMap").innerHTML = c?.map_url ? `<a href="${esc(c.map_url)}" target="_blank" rel="noopener">Open the course map</a> to find the mile markers.` : "";
  }
  if (e.target.matches("[data-pinpaste]")){
    const pin = S.pinFromText(e.target.value);
    if (pin){ setPin(e.target.closest(".spotrow"), pin); e.target.value = ""; }
    else if (e.target.value.trim()) toast("No map location in that. Long-press the spot in Google Maps, then copy the coordinates.");
  }
});
document.addEventListener("click", async e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.dataset.ovcp != null){ ovCp = t.dataset.ovcp; ls.set("xc-ovcp", ovCp); render(); ovPoke(); return; }
  if (t.id === "ovBg"){ ovBg = ovBg === "black" ? "green" : "black"; ls.set("xc-ovbg", ovBg); render(); ovPoke(); return; }
  if (t.dataset.cp != null){
    const n = $("#coachName"); if (n){ coachName = n.value.trim(); ls.set("xc-coach", coachName); }
    myCp = Number(t.dataset.cp); ls.set(cpKey, myCp);
    wake(); render(); return;
  }
  if (t.dataset.switch != null){ myCp = Number(t.dataset.switch); ls.set(cpKey, myCp); render(); return; }
  if (t.id === "changeCp"){ myCp = null; ls.set(cpKey, null); render(); return; }
  if (t.id === "undo"){ const arr = [...(local.logs[myCp] || [])].sort((a, b) => a.at - b.at); if (arr.length) delTap(arr[arr.length - 1].id); return; }
  if (t.dataset.del){ delTap(t.dataset.del); return; }
  if (t.dataset.pick){ const [cp, id] = t.dataset.pick.split("|"); scorePick[cp] = id; render(); return; }
  if (t.id === "camOn"){ camStart(); return; }
  if (t.id === "camOff"){ camStop(); render(true); return; }
  if (t.id === "camFlip"){ camFacing = camFacing === "environment" ? "user" : "environment"; camStart(); return; }
  if (t.id === "recStop"){ recStop(); return; }
  if (t.id === "camUndo"){ const arr = [...(local.logs[myCp] || [])].sort((a, b) => a.at - b.at); if (arr.length) delTap(arr[arr.length - 1].id); return; }
  if (t.id === "recBtn"){ recording ? recStop() : recStart_(); return; }
  if (t.id === "go"){ const at = sNow(); race.start_at = at; render(); clock({ action: "start", at }); return; }
  if (t.dataset.nudge){ race.start_at += Number(t.dataset.nudge); render(); clock({ action: "nudge", ms: Number(t.dataset.nudge) }); return; }
  if (t.id === "resetClock"){ if (armed !== "reset") return arm("reset"); armed = null; clock({ action: "clear" }); return; }
  if (t.id === "pinMine"){
    try { const c = await hereNow(); const pins = [...(race.pins || [])]; while (pins.length < race.checkpoints.length) pins.push(null);
      pins[myCp] = { lat: +c.latitude.toFixed(6), lon: +c.longitude.toFixed(6) };
      await post("/race", { csrf: CSRF, fresh: false, name: race.name, teams: race.teams, checkpoints: race.checkpoints, pins, course_id: race.course?.id || null, stream_url: race.stream_url });
      toast(`Pinned ${race.checkpoints[myCp]} (GPS within ${Math.round(c.accuracy)} m).`, true); poll();
    } catch(err){ toast(err.message); }
    return;
  }
  if (t.hasAttribute("data-pinhere")){
    const row = t.closest(".spotrow"); t.disabled = true; t.textContent = "Finding location…";
    try { const c = await hereNow(); setPin(row, { lat: +c.latitude.toFixed(6), lon: +c.longitude.toFixed(6) });
      toast(`Pinned (GPS within ${Math.round(c.accuracy)} m). Save changes to keep it.`, true); }
    catch(err){ toast(err.message); }
    t.disabled = false; t.textContent = "Pin this spot here"; return;
  }
  if (t.hasAttribute("data-unpin")){ setPin(t.closest(".spotrow"), null); return; }
  if (t.id === "addTeam"){
    const row = document.createElement("div"); row.className = "teamrow"; row.dataset.id = "t" + rid();
    row.innerHTML = `<input type="color" value="#555555" aria-label="Jersey color"><input type="text" maxlength="40" placeholder="School name" aria-label="School name"><button type="button" class="x danger" data-rmteam aria-label="Remove team">✕</button>`;
    $("#teamList").appendChild(row); row.querySelector("input[type=text]").focus(); return;
  }
  if (t.hasAttribute("data-rmteam")){ t.closest(".teamrow").remove(); return; }
  if (t.id === "addSpot"){ $("#spotList").insertAdjacentHTML("beforeend", spotRow("", null)); $("#spotList .spotrow:last-child .spotname").focus(); return; }
  if (t.hasAttribute("data-rmspot")){ t.closest(".spotrow").remove(); return; }
  if (t.id === "saveSetup" || t.id === "newRace"){
    if (t.id === "newRace" && armed !== "newrace"){ armed = "newrace"; t.classList.add("armed"); t.textContent = "Tap again: start the next race"; setTimeout(() => { if (armed === "newrace"){ armed = null; t.classList.remove("armed"); t.textContent = "New race, same teams"; } }, 4000); return; }
    armed = null;
    const data = readSetup();
    if (!data.teams.length || !data.checkpoints.length){ toast("Add at least one team and one coach spot."); return; }
    const fresh = t.id === "newRace";
    try {
      await post("/race", { ...data, fresh, csrf: CSRF });
      if (fresh || !race){ myCp = myCp != null && myCp < data.checkpoints.length ? myCp : null; }
      location.hash = fresh || !race ? "#start" : "#tap";
      await poll();
      toast(fresh ? "Next race ready. Press START at the gun." : "Saved.", true);
    } catch(err){ toast(err.message); }
  }
});
window.addEventListener("hashchange", () => { armed = null; ovHidden = false; render(true); ovPoke(); });
["mousemove", "pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, ovPoke, { passive: true }));
window.addEventListener("online", () => { flush(); poll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden){ poll(); if (myCp != null || camStream) wake(); } });
window.addEventListener("beforeunload", e => { if (recording || dirty.size){ e.preventDefault(); e.returnValue = ""; } });

render(true); updateSyncUi(); requestAnimationFrame(tick); poll();
})();

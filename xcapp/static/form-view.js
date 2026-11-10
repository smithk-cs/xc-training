/* The running-form results screen: a player on top (the athlete's own video with the skeleton drawn over it right
   after analysis, or one stride as an animated stick figure on a saved report), speed and scrub controls, then tabs:
   Analysis (total score and a card per check, ranked priorities, tap for why, cue and drill), Metrics, Graph.
   mount(root, view, player): view comes from form.py view(); player is {video, frames, width, height} or null. */
import { angle } from "/static/form-core.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NEAR_LEFT = new Set([11, 13, 15, 23, 25, 27, 29, 31]);
const col = (s) => (s == null ? "#777" : s >= 80 ? "#6cc04a" : s >= 40 ? "#f2c500" : "#ff5a36");

function ring(score, size = 64) {
  // A 270° gauge: grey track, colored arc to the score, a dot at its end
  const r = size / 2 - 6, c = size / 2, a0 = 135, sweep = 270, a1 = a0 + sweep * Math.max(0, Math.min(100, score ?? 0)) / 100;
  const pt = (a) => [c + r * Math.cos(a * Math.PI / 180), c + r * Math.sin(a * Math.PI / 180)];
  const arc = (from, to) => { const [x0, y0] = pt(from), [x1, y1] = pt(to); return `M${x0.toFixed(1)} ${y0.toFixed(1)} A${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`; };
  const [dx, dy] = pt(a1);
  return `<svg class="fv-ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">
    <path d="${arc(a0, a0 + sweep)}" stroke="#3a3a3a" stroke-width="5" fill="none" stroke-linecap="round"/>
    ${score ? `<path d="${arc(a0, Math.max(a0 + 1, a1))}" stroke="${col(score)}" stroke-width="5" fill="none" stroke-linecap="round"/>` : ""}
    <circle cx="${dx.toFixed(1)}" cy="${dy.toFixed(1)}" r="5" fill="${col(score)}" stroke="#1b1b1b" stroke-width="2"/>
    <circle cx="${c}" cy="${c}" r="${r - 9}" fill="${col(score)}" fill-opacity=".18"/></svg>`;
}

function valueText(c) {
  if (c.value == null) return "–";
  return `${Number(c.value).toFixed(c.unit === "°" ? 1 : 0).replace(/\.0$/, "")}${c.unit === "°" || !c.unit ? c.unit : " " + c.unit}`;
}

function analysisTab(v) {
  const cards = v.checks.map((c, i) => `
    <button type="button" class="fv-card${c.state ? " " + c.state : ""}" data-i="${i}" aria-expanded="false">
      <div class="fv-card-top">${ring(c.score)}<div><div class="fv-sub">score</div><div class="fv-big">${c.score == null ? "–" : c.score + "%"}</div></div></div>
      <div class="fv-name">${esc(c.short)}</div>
      <div class="fv-verdict">${esc(c.summary.replace(/\.$/, ""))}</div>
      <div class="fv-val">${valueText(c)} <span>good ${c.lo} to ${c.hi}</span></div>
      ${c.priority ? `<div class="fv-pri"><b>#${c.priority}</b><span>Priority to work on</span></div>` : ""}
      <div class="fv-more" hidden>
        <p>${esc(c.what)}</p>
        ${c.why ? `<p><b>Why it matters:</b> ${esc(c.why)}</p>` : ""}
        ${c.state === "low" || c.state === "high" ? `<p><b>Cue:</b> ${esc(c.cue)}</p><p><b>Drill:</b> ${esc(c.drill)}</p>` : ""}
      </div>
      <div class="fv-tap">Tap to see more</div>
    </button>`).join("");
  return `<div class="fv-total">${ring(v.score, 76)}<div class="fv-total-label">Total score</div><div class="fv-total-num" style="color:${col(v.score)}">${v.score == null ? "–" : Math.round(v.score) + "%"}</div></div>
    <div class="fv-grid">${cards}</div>`;
}

function metricsTab(v) {
  const t = v.timing.map((m) => `<div class="fv-metric"><div class="fv-sub">${esc(m.title)}</div><div class="fv-big">${m.value == null ? "–" : m.value + " " + esc(m.unit)}</div>
    <p>${m.value == null ? "Film at 60+ frames a second (slow motion) to measure this." : esc(m.what)}</p></div>`).join("");
  const step = v.checks.find((c) => c.key === "cadence");
  const s = v.stride ? `<div class="fv-metric"><div class="fv-sub">Step length</div><div class="fv-big">${v.stride.step_m} m</div><p>One foot strike to the next, at ${esc(v.stride.pace)} a mile.</p></div>
    <div class="fv-metric"><div class="fv-sub">Stride length</div><div class="fv-big">${v.stride.stride_m} m</div><p>One foot strike to the next of the same foot.</p></div>`
    : `<div class="fv-metric"><div class="fv-sub">Step and stride length</div><div class="fv-big">–</div><p>Enter the pace when analyzing to get these.</p></div>`;
  return `<div class="fv-metrics"><div class="fv-metric"><div class="fv-sub">Step rate</div><div class="fv-big">${step && step.value != null ? Math.round(step.value) : "–"} /min</div><p>Foot strikes a minute, both feet.</p></div>${t}${s}</div>`;
}

function graphTab(v) {
  const order = ["knee", "hip", "ankle", "elbow"];
  return `<p class="fv-note">From one foot strike to the next of the same foot. Left and right far apart can point to an imbalance.</p>` + order.map((k) => {
    const s = v.curves[k] || {}, vals = [...(s.left || []), ...(s.right || [])];
    if (!vals.length) return "";
    const lo = Math.floor((Math.min(...vals) - 5) / 10) * 10, hi = Math.ceil((Math.max(...vals) + 5) / 10) * 10, span = hi - lo || 1;
    const line = (arr, color) => arr && arr.length > 1 ? `<polyline fill="none" stroke="${color}" stroke-width="2.2" points="${arr.map((y, i) => `${(30 + 270 * i / (arr.length - 1)).toFixed(1)},${(110 - 100 * (y - lo) / span).toFixed(1)}`).join(" ")}"/>` : "";
    const grid = [0, 0.5, 1].map((f) => `<line x1="30" x2="300" y1="${110 - 100 * f}" y2="${110 - 100 * f}" stroke="#3a3a3a"/><text x="26" y="${113 - 100 * f}" text-anchor="end">${Math.round(lo + span * f)}°</text>`).join("");
    return `<figure class="fv-chart"><figcaption>${esc(v.curve_titles[k])}<span><i style="background:#4a90e2"></i>left <i style="background:#ff5a36"></i>right</span></figcaption>
      <svg viewBox="0 0 300 125" role="img" aria-label="${esc(v.curve_titles[k])}, left and right">${grid}${line(s.left, "#4a90e2")}${line(s.right, "#ff5a36")}
      <text x="30" y="124">0%</text><text x="300" y="124" text-anchor="end">100% of stride</text></svg></figure>`;
  }).join("");
}

/** Skeleton with angle tags (both knees, the near elbow, the forward lean), drawn on a canvas in pixel space. */
function drawSkeleton(ctx, pts, bones, scale, opts = {}) {
  ctx.lineCap = "round";
  ctx.lineWidth = opts.width || 3;
  for (const [a, b] of bones) {
    ctx.strokeStyle = NEAR_LEFT.has(a) && NEAR_LEFT.has(b) ? "#ff5a36" : "#5b6cff";
    ctx.beginPath(); ctx.moveTo(pts[a][0], pts[a][1]); ctx.lineTo(pts[b][0], pts[b][1]); ctx.stroke();
  }
  const ear = pts[7], nose = pts[0], hr = Math.max(6, Math.hypot(nose[0] - ear[0], nose[1] - ear[1]) * 0.9);
  ctx.strokeStyle = "#5b6cff"; ctx.beginPath(); ctx.arc((ear[0] + nose[0]) / 2, (ear[1] + nose[1]) / 2, hr, 0, 7); ctx.stroke();
  ctx.fillStyle = "#fff";
  for (const j of [0, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]) { ctx.beginPath(); ctx.arc(pts[j][0], pts[j][1], opts.dot || 3, 0, 7); ctx.fill(); }
  const tag = (x, y, text) => {
    ctx.font = `600 ${Math.round(11 * scale)}px Inter, system-ui, sans-serif`;
    const w = ctx.measureText(text).width + 8 * scale, h = 16 * scale;
    ctx.fillStyle = "rgba(255,90,54,.92)"; ctx.fillRect(x, y - h / 2, w, h);
    ctx.fillStyle = "#fff"; ctx.fillText(text, x + 4 * scale, y + 4 * scale);
  };
  const off = 8 * scale;
  tag(pts[25][0] + off, pts[25][1], Math.round(angle(pts[23], pts[25], pts[27])) + "°");
  tag(pts[26][0] + off, pts[26][1] + 18 * scale, Math.round(angle(pts[24], pts[26], pts[28])) + "°");
  tag(pts[13][0] + off, pts[13][1], Math.round(angle(pts[11], pts[13], pts[15])) + "°");
  const sh = [(pts[11][0] + pts[12][0]) / 2, (pts[11][1] + pts[12][1]) / 2], hp = [(pts[23][0] + pts[24][0]) / 2, (pts[23][1] + pts[24][1]) / 2];
  const lean = Math.abs(Math.atan2(sh[0] - hp[0], hp[1] - sh[1]) * 180 / Math.PI);
  tag(hp[0] + off, hp[1] - 14 * scale, Math.round(lean) + "°");
}

export function mount(root, v, player) {
  root.innerHTML = `<div class="fv">
    <div class="fv-stage" id="fvStage"></div>
    <div class="fv-controls">
      <button type="button" class="fv-play" id="fvPlay" aria-label="Play">▶</button>
      <input type="range" id="fvScrub" min="0" max="1000" value="0" aria-label="Position in the clip">
      <button type="button" class="fv-speed" id="fvSpeed" aria-label="Playback speed">×0.25</button>
    </div>
    <div class="fv-tabs" role="tablist">
      <button type="button" role="tab" aria-selected="true" data-tab="a">Analysis</button>
      <button type="button" role="tab" aria-selected="false" data-tab="m">Metrics</button>
      <button type="button" role="tab" aria-selected="false" data-tab="g">Graph</button>
    </div>
    <div class="fv-panel" data-panel="a">${analysisTab(v)}</div>
    <div class="fv-panel" data-panel="m" hidden>${metricsTab(v)}</div>
    <div class="fv-panel" data-panel="g" hidden>${graphTab(v)}</div></div>`;
  const $ = (s) => root.querySelector(s);
  root.querySelectorAll(".fv-tabs [role=tab]").forEach((t) => t.addEventListener("click", () => {
    root.querySelectorAll(".fv-tabs [role=tab]").forEach((x) => x.setAttribute("aria-selected", x === t ? "true" : "false"));
    root.querySelectorAll(".fv-panel").forEach((p) => { p.hidden = p.dataset.panel !== t.dataset.tab; });
  }));
  root.querySelectorAll(".fv-card").forEach((c) => c.addEventListener("click", () => {
    const open = c.getAttribute("aria-expanded") !== "true";
    c.setAttribute("aria-expanded", open ? "true" : "false");
    c.querySelector(".fv-more").hidden = !open;
    c.querySelector(".fv-tap").textContent = open ? "Tap to close" : "Tap to see more";
  }));

  const speeds = [0.25, 0.5, 1];
  let speed = 0.25, playing = false;
  const stage = $("#fvStage"), canvas = document.createElement("canvas");
  canvas.className = "fv-canvas";
  const ctx = canvas.getContext("2d");
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  let draw, length, position, setPosition, play, pause;

  if (player && player.video) {
    // The athlete's video, with the skeleton found in each analyzed frame drawn over the runner
    const vid = player.video;
    vid.removeAttribute("controls"); vid.muted = true; vid.playsInline = true;
    stage.append(vid, canvas);
    const fr = player.frames, t0 = fr[0].vt, t1 = fr[fr.length - 1].vt;
    length = t1 - t0;
    const nearest = (t) => { let lo = 0, hi = fr.length - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (fr[m].vt < t) lo = m; else hi = m; } return Math.abs(fr[lo].vt - t) < Math.abs(fr[hi].vt - t) ? fr[lo] : fr[hi]; };
    draw = () => {
      const r = vid.getBoundingClientRect(); canvas.width = r.width * dpr; canvas.height = r.height * dpr;
      canvas.style.width = r.width + "px"; canvas.style.height = r.height + "px";
      const k = Math.min(r.width / player.width, r.height / player.height), ox = (r.width - player.width * k) / 2, oy = (r.height - player.height * k) / 2;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, r.width, r.height);
      if (vid.currentTime < t0 - 0.05 || vid.currentTime > t1 + 0.05) return;
      const f = nearest(vid.currentTime);
      drawSkeleton(ctx, f.p.map((q) => [ox + q[0] * player.width * k, oy + q[1] * player.height * k]), v.bones, Math.max(0.7, k * player.width / 900), { width: 2.5 });
    };
    position = () => (vid.currentTime - t0) / (length || 1);
    setPosition = (x) => { vid.currentTime = t0 + x * length; };
    play = () => { try { vid.playbackRate = speed; } catch (e) {} if (vid.currentTime >= t1 - 0.02) vid.currentTime = t0; vid.play().catch(() => {}); };
    pause = () => vid.pause();
    vid.addEventListener("timeupdate", () => { if (vid.currentTime > t1) vid.currentTime = t0; });
    vid.currentTime = t0;
  } else if (v.clip) {
    // A saved report: one stride of the runner as an animated stick figure
    stage.classList.add("fv-anim");
    stage.append(canvas);
    const c = v.clip;
    length = c.t[c.t.length - 1] || 1;
    const used = [...new Set([0, 7, ...v.bones.flat()])];   // the joints drawn (hands and face detail aren't)
    const allX = c.f.flatMap((f) => used.map((j) => f[j][0])), allY = c.f.flatMap((f) => used.map((j) => f[j][1]));
    const bx = [Math.min(...allX), Math.max(...allX)], by = [Math.min(...allY), Math.max(...allY)];
    let at = 0, last = 0;
    draw = () => {
      const r = stage.getBoundingClientRect(); canvas.width = r.width * dpr; canvas.height = r.height * dpr;
      canvas.style.width = r.width + "px"; canvas.style.height = r.height + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, r.width, r.height);
      let i = c.t.findIndex((t) => t >= at); if (i < 0) i = c.t.length - 1;
      const size = Math.min(r.width * 0.9 / (bx[1] - bx[0] || 1), r.height * 0.86 / (by[1] - by[0] || 1));
      const ox = r.width / 2 - size * (bx[0] + bx[1]) / 2, oy = r.height * 0.07 - size * by[0];
      const ground = oy + size * by[1] + 3;
      ctx.strokeStyle = "#444"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(0, ground); ctx.lineTo(r.width, ground); ctx.stroke();
      drawSkeleton(ctx, c.f[i].map((q) => [ox + q[0] * size, oy + q[1] * size]), v.bones, Math.max(0.8, size / 320), { width: 4, dot: 4 });
    };
    position = () => at / length;
    setPosition = (x) => { at = x * length; draw(); };
    const tick = (now) => { if (!playing) return; at += (now - last) / 1000 * speed; last = now; if (at > length) at = 0; draw(); scrub.value = Math.round(position() * 1000); requestAnimationFrame(tick); };
    play = () => { last = performance.now(); requestAnimationFrame(tick); };
    pause = () => {};
  } else {
    stage.innerHTML = `<p class="fv-note" style="padding:20px">No stride saved for this analysis.</p>`;
    $("#fvPlay").disabled = true; $("#fvScrub").disabled = true;
    return;
  }
  const scrub = $("#fvScrub"), btn = $("#fvPlay"), spd = $("#fvSpeed");
  const loop = () => { draw(); if (player && player.video) scrub.value = Math.round(position() * 1000); if (playing && player && player.video) requestAnimationFrame(loop); };
  btn.addEventListener("click", () => {
    playing = !playing; btn.textContent = playing ? "❚❚" : "▶"; btn.setAttribute("aria-label", playing ? "Pause" : "Play");
    if (playing) { play(); if (player && player.video) requestAnimationFrame(loop); } else pause();
  });
  scrub.addEventListener("input", () => { setPosition(scrub.value / 1000); draw(); });
  spd.addEventListener("click", () => {
    speed = speeds[(speeds.indexOf(speed) + 1) % speeds.length]; spd.textContent = "×" + speed;
    if (player && player.video) { try { player.video.playbackRate = speed; } catch (e) {} }
  });
  if (player && player.video) player.video.addEventListener("seeked", draw);
  window.addEventListener("resize", draw);
  setTimeout(draw, 50);
}

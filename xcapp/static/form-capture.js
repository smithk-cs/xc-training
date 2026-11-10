/* Running-form capture: pick or film a side-view clip, find the runner's joints in each frame with MediaPipe Pose
   (on this phone or computer; the video is never uploaded), measure the stride (form-core.js), and save only the
   measurements to the athlete's record. The server writes the feedback and the PDF. */
import { analyze } from "/static/form-core.js";

const cfg = JSON.parse(document.getElementById("form-cfg").textContent);
const $ = (s) => document.querySelector(s);
const video = $("#clip"), status = $("#status"), bar = $("#progress");
let file = null, landmarker = null;

function say(text, bad) { status.textContent = text; status.classList.toggle("bad", !!bad); }

$("#file").addEventListener("change", (e) => {
  file = e.target.files[0] || null;
  if (!file) return;
  video.src = URL.createObjectURL(file);
  video.hidden = false;
  $("#go").disabled = false;
  say("Ready. Check the runner is side-on with the whole body in frame, then tap Analyze.");
});

async function loadModel() {
  if (landmarker) return landmarker;
  say("Loading the pose model (about 24 MB the first time; it's saved on this device after that)…");
  const { FilesetResolver, PoseLandmarker } = await import("/static/vendor/pose/vision_bundle.mjs");
  const vision = await FilesetResolver.forVisionTasks("/static/vendor/pose");
  const opts = (delegate) => ({ baseOptions: { modelAssetPath: "/static/vendor/pose/pose_landmarker_full.task", delegate },
                                runningMode: "VIDEO", numPoses: 1, minPoseDetectionConfidence: 0.5, minTrackingConfidence: 0.5 });
  try { landmarker = await PoseLandmarker.createFromOptions(vision, opts("GPU")); }
  catch (e) { landmarker = await PoseLandmarker.createFromOptions(vision, opts("CPU")); }
  return landmarker;
}

const seek = (t) => new Promise((ok) => { const done = () => { video.removeEventListener("seeked", done); ok(); }; video.addEventListener("seeked", done); video.currentTime = t; });

async function frameRate() {
  // How many frames a second the clip has: play a moment and count the frames the browser shows
  if (!("requestVideoFrameCallback" in HTMLVideoElement.prototype)) return 30;
  const times = [];
  video.muted = true;
  await seek(0);
  await new Promise((ok) => {
    const step = (_now, meta) => { times.push(meta.mediaTime); if (times.length < 40 && !video.ended) video.requestVideoFrameCallback(step); else ok(); };
    video.requestVideoFrameCallback(step);
    video.play().catch(ok);
    setTimeout(ok, 2500);
  });
  video.pause();
  const gaps = times.slice(1).map((t, i) => t - times[i]).filter((g) => g > 0.001).sort((a, b) => a - b);
  const g = gaps[Math.floor(gaps.length / 2)];
  return g ? Math.min(240, Math.max(15, Math.round(1 / g))) : 30;
}

$("#go").addEventListener("click", async () => {
  if (!file) return;
  $("#go").disabled = true;
  try {
    const lm = await loadModel();
    await new Promise((ok) => (video.readyState >= 1 ? ok() : video.addEventListener("loadedmetadata", ok, { once: true })));
    const slow = Number($("#slowmo").value) || 1;          // a slow-motion clip saved at normal speed: 2, 4 or 8 × slower
    const fps = await frameRate();
    // About 4 seconds of running (a dozen steps) is plenty; fewer for very high frame rates, so the phone isn't busy for long
    const realSeconds = Math.min(4, 480 / (fps * slow));
    const end = Math.min(video.duration, Number($("#start").value || 0) + realSeconds * slow);
    const start = Math.min(Number($("#start").value || 0), Math.max(0, end - 1));
    const step = 1 / fps, frames = [];
    const total = Math.floor((end - start) / step);
    for (let i = 0; i <= total; i++) {
      const t = start + i * step;
      await seek(t);
      const r = lm.detectForVideo(video, Math.round(t * 1000) + 1);
      const p = r.landmarks && r.landmarks[0];
      if (p) frames.push({ t: (t - start) / slow, p: p.map((q) => [q.x, q.y, q.visibility ?? 1]) });
      if (i % 5 === 0) { bar.value = i / total; say(`Finding the runner in each frame… ${Math.round(100 * i / total)}%`); }
    }
    bar.value = 1;
    if (frames.length < 0.6 * total) throw new Error("The runner was found in too few frames. Film side-on, the whole body in frame, in good light.");
    const result = analyze(frames, video.videoWidth, video.videoHeight);
    if (result.metrics.cadence && result.metrics.cadence < 120 && slow === 1)
      throw new Error(`That's ${result.metrics.cadence} steps a minute, too slow for running: if this is a slow-motion clip, choose how slow and analyze again.`);
    say("Saving…");
    const body = { ...result, pace: $("#pace").value.trim(), note: $("#note").value.trim(), csrf: cfg.csrf };
    const r = await fetch(cfg.save, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "same-origin" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || d.detail || "Couldn't save the analysis.");
    location.href = d.url;
  } catch (e) {
    say(e.message || "Something went wrong analyzing the clip.", true);
    $("#go").disabled = false;
  }
});

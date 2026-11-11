/* Running-form analysis from pose landmarks (MediaPipe Pose: 33 points per frame, x and y from 0 to 1 across the image).
   Pure functions, so the tests run them on made-up strides. The page (form-capture.js) runs the pose model on the
   video and passes the frames here; the server rates each measure and writes the feedback (xcapp/form.py).

   Side view, the whole body in frame. Each frame: {t: seconds, p: [[x, y, visibility] × 33]}. The page runs tidy()
   on the frames first (filling gaps and fixing legs the model swapped); analyze() measures them. */

export const L = { nose: 0, lEye: 2, rEye: 5, lEar: 7, rEar: 8, lSh: 11, rSh: 12, lEl: 13, rEl: 14, lWr: 15, rWr: 16,
  lHip: 23, rHip: 24, lKnee: 25, rKnee: 26, lAnk: 27, rAnk: 28, lHeel: 29, rHeel: 30, lToe: 31, rToe: 32 };
const SIDES = { left: { sh: L.lSh, el: L.lEl, wr: L.lWr, hip: L.lHip, knee: L.lKnee, ank: L.lAnk, heel: L.lHeel, toe: L.lToe, ear: L.lEar },
                right: { sh: L.rSh, el: L.rEl, wr: L.rWr, hip: L.rHip, knee: L.rKnee, ank: L.rAnk, heel: L.rHeel, toe: L.rToe, ear: L.rEar } };
const DEG = 180 / Math.PI;
const median = (a) => { const s = [...a].sort((x, y) => x - y); const n = s.length; return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : NaN; };
const mean = (a) => a.length ? a.reduce((s, x) => s + x, 0) / a.length : NaN;
const pct = (a, q) => { const s = [...a].sort((x, y) => x - y); return s.length ? s[Math.min(s.length - 1, Math.max(0, Math.round(q * (s.length - 1))))] : NaN; };

/** Interior angle at b (degrees) between b→a and b→c. */
export function angle(a, b, c) {
  const v1 = [a[0] - b[0], a[1] - b[1]], v2 = [c[0] - b[0], c[1] - b[1]];
  const d = Math.hypot(...v1) * Math.hypot(...v2);
  if (!d) return NaN;
  return Math.acos(Math.max(-1, Math.min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / d))) * DEG;
}

/** Frames in pixels (x × width, y × height), lightly smoothed (3-frame average) to take the jitter out. */
export function toPixels(frames, w, h) {
  const px = frames.map(f => ({ t: f.t, p: f.p.map(q => [q[0] * w, q[1] * h, q[2] ?? 1]) }));
  return px.map((f, i) => {
    const near = px.slice(Math.max(0, i - 1), i + 2);
    return { t: f.t, p: f.p.map((q, j) => [mean(near.map(g => g.p[j][0])), mean(near.map(g => g.p[j][1])), q[2]]) };
  });
}

const mid = (p, a, b) => [(p[a][0] + p[b][0]) / 2, (p[a][1] + p[b][1]) / 2];

// Left/right pairs: the legs (with the hips) and the arms (with the shoulders), each swapped as a group
const LEG_PAIRS = [[L.lHip, L.rHip], [L.lKnee, L.rKnee], [L.lAnk, L.rAnk], [L.lHeel, L.rHeel], [L.lToe, L.rToe]];
const ARM_PAIRS = [[L.lSh, L.rSh], [L.lEl, L.rEl], [L.lWr, L.rWr]];

/** Clean up what the pose model found, before measuring or drawing. Seen side-on, a runner's legs cross every step
    and the model sometimes labels the left leg as the right for a frame or two; it loses the runner in a blurred
    frame; and a half-hidden joint (the far arm, a foot behind the other leg) can jump away for a frame. So:
    - a frame where the runner wasn't found (p null) is filled in from the frames either side, if the gap is short;
    - legs (and arms) are kept on the same side from frame to frame: when swapping a pair matches where those joints
      were heading clearly better than keeping it, they're swapped;
    - a joint the model wasn't sure of, or that jumps far from its neighbours for a frame, is filled in from the
      frames either side.
    frames: [{t, vt?, p: [[x, y, visibility] × 33] | null}], x and y from 0 to 1. Returns the frames kept, same shape. */
export function tidy(frames, w = 1, h = 1) {
  const n = frames.length;
  if (!n) return [];
  const dt = n > 1 ? (frames[n - 1].t - frames[0].t) / (n - 1) : 1 / 30;
  const fps = 1 / Math.max(1e-3, dt);
  const P = (f, j) => [f.p[j][0] * w, f.p[j][1] * h];
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const found = frames.filter(f => f.p).map(f => ({ ...f, p: f.p.map(q => [...q]) }));
  // 1. Keep each leg (and arm) on its own side: compare with where the joints were heading (from the two frames
  //    found before, allowing for frames in between where the runner wasn't found)
  for (let i = 1; i < found.length; i++) {
    const f = found[i], a = found[i - 1], b = found[i - 2];
    for (const pairs of [LEG_PAIRS, ARM_PAIRS]) {
      let keep = 0, swap = 0;
      for (const [l, r] of pairs) {
        const pred = (j) => { const pa = P(a, j); if (!b) return pa; const pb = P(b, j), u = (f.t - a.t) / Math.max(1e-6, a.t - b.t);
                              return [pa[0] + (pa[0] - pb[0]) * u, pa[1] + (pa[1] - pb[1]) * u]; };
        const cl = P(f, l), cr = P(f, r), pl = pred(l), pr = pred(r);
        keep += dist(cl, pl) + dist(cr, pr);
        swap += dist(cl, pr) + dist(cr, pl);
      }
      if (swap < 0.6 * keep) for (const [l, r] of pairs) [f.p[l], f.p[r]] = [f.p[r], f.p[l]];
    }
  }
  // 2. Short gaps (up to about a tenth of a second) filled in; longer ones dropped
  const maxGap = Math.max(2, Math.round(0.1 * fps));
  const at = new Map(found.map(f => [f.t, f]));
  const filled = frames.map(f => at.get(f.t) || { ...f, p: null });
  for (let i = 0; i < n; i++) {
    if (filled[i].p) continue;
    let j = i; while (j < n && !filled[j].p) j++;
    const a = filled[i - 1], b = filled[j];
    if (a && b && j - i <= maxGap) {
      for (let k = i; k < j; k++) {
        const u = (k - (i - 1)) / (j - (i - 1));
        filled[k].p = a.p.map((q, m) => [q[0] + (b.p[m][0] - q[0]) * u, q[1] + (b.p[m][1] - q[1]) * u, Math.min(q[2], b.p[m][2])]);
      }
    }
    i = j;
  }
  const out = filled.filter(f => f.p);
  const m = out.length;
  if (m < 3) return out;
  // 3. Unsure or jumpy joints filled in from the frames either side (the 5-frame median keeps real fast movement)
  const legLen = median(out.map(f => (dist(P(f, L.lHip), P(f, L.lAnk)) + dist(P(f, L.rHip), P(f, L.rAnk))) / 2)) || 1;
  const reach = 4;
  for (let j = 0; j < 33; j++) {
    const bad = out.map((f, i) => {
      if (f.p[j][2] < 0.3) return true;
      const near = out.slice(Math.max(0, i - 2), Math.min(m, i + 3));
      const med = [median(near.map(g => g.p[j][0] * w)), median(near.map(g => g.p[j][1] * h))];
      return dist(P(f, j), med) > 0.4 * legLen;
    });
    if (bad.every(Boolean)) continue;
    const fixed = out.map(f => f.p[j]);
    for (let i = 0; i < m; i++) {
      if (!bad[i]) continue;
      let a = i - 1; while (a >= 0 && bad[a]) a--;
      let b = i + 1; while (b < m && bad[b]) b++;
      if (a >= 0 && b < m && b - a <= 2 * reach) {
        const u = (i - a) / (b - a), qa = out[a].p[j], qb = out[b].p[j];
        fixed[i] = [qa[0] + (qb[0] - qa[0]) * u, qa[1] + (qb[1] - qa[1]) * u, Math.min(qa[2], qb[2])];
      } else if (a >= 0 && i - a <= reach && b >= m) fixed[i] = [...out[a].p[j]];
      else if (b < m && b - i <= reach && a < 0) fixed[i] = [...out[b].p[j]];
    }
    out.forEach((f, i) => { f.p[j] = fixed[i]; });
  }
  return out;
}

/** Contacts for one foot: the frames it's on the ground. A foot is down while it's near its lowest point and moving
    backward relative to the hips (the body passing over it). That holds whether the phone is held still or follows
    the runner, and it catches the very frame the foot lands. */
export function stances(px, side, legLen, dir) {
  const s = SIDES[side];
  const n = px.length;
  const footY = px.map(f => Math.max(f.p[s.heel][1], f.p[s.toe][1]));
  const rel = px.map(f => (f.p[s.heel][0] + f.p[s.toe][0]) / 2 - mid(f.p, L.lHip, L.rHip)[0]);
  const win = Math.max(3, Math.round(0.4 / Math.max(1e-3, (px[n - 1].t - px[0].t) / Math.max(1, n - 1))));   // about 0.4 s
  const down = new Array(n).fill(false);
  for (let i = 0; i < n - 1; i++) {
    const low = Math.max(...footY.slice(Math.max(0, i - win), Math.min(n, i + win + 1)));
    const back = dir * (rel[i + 1] - rel[i]) < 0;      // moving backward relative to the hips
    down[i] = footY[i] >= low - 0.05 * legLen && back;
  }
  const out = [];
  for (let i = 0; i < n; i++) {
    if (down[i] && (i === 0 || !down[i - 1])) {
      let j = i; while (j + 1 < n && down[j + 1]) j++;
      if (i > 0 && j < n - 2) out.push({ side, on: i, off: j, t0: px[i].t, t1: px[j].t });   // whole contacts only
      i = j;
    }
  }
  return out;
}

/** Everything the report needs from the frames. Throws an Error with a coach-readable message when the clip won't do. */
export function analyze(frames, w, h) {
  if (frames.length < 20) throw new Error("The clip is too short. Film at least 4 steps.");
  const px = toPixels(frames, w, h);
  const vis = mean(px.map(f => mean([L.lHip, L.rHip, L.lKnee, L.rKnee, L.lAnk, L.rAnk, L.lSh, L.rSh].map(j => f.p[j][2]))));
  if (!(vis >= 0.5)) throw new Error("The runner wasn't clear enough. Film from the side with the whole body in frame, in good light.");
  const legLen = median(px.map(f => (Math.hypot(f.p[L.lHip][0] - f.p[L.lAnk][0], f.p[L.lHip][1] - f.p[L.lAnk][1]) +
                                     Math.hypot(f.p[L.rHip][0] - f.p[L.rAnk][0], f.p[L.rHip][1] - f.p[L.rAnk][1])) / 2));
  const hipX = px.map(f => mid(f.p, L.lHip, L.rHip)[0]);
  const dur = px[px.length - 1].t - px[0].t;
  const hipSpeed = (hipX[hipX.length - 1] - hipX[0]) / dur;             // pixels a second across the image
  const still = Math.abs(hipSpeed) > 0.5 * legLen ? Math.abs(hipSpeed) : 0;
  // Which way the runner faces: the way they cross the image, or (camera following) where the nose points
  const dir = still ? Math.sign(hipSpeed) : Math.sign(median(px.map(f => f.p[L.nose][0] - mid(f.p, L.lEar, L.rEar)[0]))) || 1;
  // Running: each foot swings from behind the hips to in front of them (about a leg length); a pose held still or a
  // walk-on doesn't, and its little jitters would otherwise look like steps
  for (const side of ["left", "right"]) {
    const s = SIDES[side];
    const rel = px.map(f => (f.p[s.heel][0] + f.p[s.toe][0]) / 2 - mid(f.p, L.lHip, L.rHip)[0]);
    if (pct(rel, 0.95) - pct(rel, 0.05) < 0.5 * legLen)
      throw new Error("The runner doesn't seem to be running in this clip. Film a few steps at running pace, side-on.");
  }
  const contacts = [...stances(px, "left", legLen, dir), ...stances(px, "right", legLen, dir)].sort((a, b) => a.on - b.on);
  if (contacts.length < 3) throw new Error("Couldn't find enough foot strikes. Film at least 4 steps from the side, with the feet in frame.");
  const fps = (px.length - 1) / dur;
  // Near side: the one the model sees better
  const near = mean(px.map(f => f.p[L.lHip][2] + f.p[L.lKnee][2] + f.p[L.lAnk][2])) >= mean(px.map(f => f.p[L.rHip][2] + f.p[L.rKnee][2] + f.p[L.rAnk][2])) ? "left" : "right";

  const atContact = contacts.map(c => {
    const p = px[c.on].p, s = SIDES[c.side], o = SIDES[c.side === "left" ? "right" : "left"];
    const hip = mid(p, L.lHip, L.rHip);
    return {
      overstride: Math.atan2(dir * (p[s.ank][0] - hip[0]), p[s.ank][1] - hip[1]) * DEG,      // + foot lands ahead of the hips
      frontKnee: angle(p[s.hip], p[s.knee], p[s.ank]),
      backKnee: angle(p[o.hip], p[o.knee], p[o.ank]),
      shin: Math.atan2(dir * (p[s.ank][0] - p[s.knee][0]), p[s.ank][1] - p[s.knee][1]) * DEG,  // + ankle ahead of the knee
    };
  });
  const trunk = px.map(f => { const sh = mid(f.p, L.lSh, L.rSh), hp = mid(f.p, L.lHip, L.rHip); return Math.atan2(dir * (sh[0] - hp[0]), hp[1] - sh[1]) * DEG; });
  const ear = (f) => f.p[SIDES[near].ear];
  const head = px.map(f => Math.atan2(ear(f)[1] - f.p[L.nose][1], dir * (f.p[L.nose][0] - ear(f)[0])) * DEG);   // + chin up
  const elbow = px.map(f => angle(f.p[SIDES[near].sh], f.p[SIDES[near].el], f.p[SIDES[near].wr]));
  const hipY = px.map(f => mid(f.p, L.lHip, L.rHip)[1]);
  const steps = contacts.slice(1).map((c, i) => c.t0 - contacts[i].t0).filter(x => x > 0.15 && x < 0.6);
  const cadence = steps.length ? 60 / median(steps) : NaN;
  const contactS = median(contacts.map(c => c.t1 - c.t0 + 1 / fps));
  const stepS = median(steps);
  const okTiming = fps >= 50;

  const metrics = {
    cadence: round(cadence, 0),
    overstride_deg: round(median(atContact.map(c => c.overstride)), 1),
    front_knee_deg: round(median(atContact.map(c => c.frontKnee)), 0),
    back_knee_deg: round(median(atContact.map(c => c.backKnee)), 0),
    shin_deg: round(median(atContact.map(c => c.shin)), 1),
    trunk_deg: round(median(trunk), 1),
    head_deg: round(median(head), 1),
    elbow_deg: round(median(elbow), 0),
    oscillation_pct: round((pct(hipY, 0.95) - pct(hipY, 0.05)) / legLen * 100, 1),
    contact_ms: okTiming ? round(contactS * 1000, 0) : null,
    flight_ms: okTiming ? round(Math.max(0, stepS - contactS) * 1000, 0) : null,
    duty_pct: okTiming ? round(contactS / (2 * stepS) * 100, 1) : null,
  };
  return { metrics, clip: strideClip(px, contacts, near, dir, legLen), curves: curves(px, contacts, dir), figure: figure(px[contacts.find(c => c.side === near)?.on ?? contacts[0].on].p),
           fps: round(fps, 1), seconds: round(dur, 2), frames: px.length, contacts: contacts.length, near, direction: dir,
           camera: still ? "still" : "following", visibility: round(vis, 2) };
}

/** One stride (a foot strike to the next of the same foot), for the animated stick figure on the saved report: the
    runner kept centered on the hips (like a treadmill), facing right, at most 60 frames. */
export function strideClip(px, contacts, near, dir, legLen) {
  const mine = contacts.filter(c => c.side === near);
  const [a, b] = mine.length >= 2 ? [mine[0].on, mine[1].on] : [0, Math.min(px.length - 1, contacts[contacts.length - 1].on)];
  const idx = [];
  const n = Math.min(60, b - a + 1);
  for (let k = 0; k < n; k++) idx.push(Math.round(a + (b - a) * k / Math.max(1, n - 1)));
  const s = 2.6 * legLen;
  const top = Math.min(...idx.map(i => Math.min(...px[i].p.map(q => q[1])))) - 0.1 * legLen;
  return {
    t: idx.map(i => round(px[i].t - px[a].t, 3)),
    f: idx.map(i => { const hx = mid(px[i].p, L.lHip, L.rHip)[0]; return px[i].p.map(q => [round(0.5 + dir * (q[0] - hx) / s, 3), round((q[1] - top) / s, 3)]); }),
  };
}

function round(x, d) { return Number.isFinite(x) ? Math.round(x * 10 ** d) / 10 ** d : null; }

/** Joint angles through the stride (0-100%, from one foot strike to the next of the same foot), averaged over the
    strides seen: knee bend, hip swing, ankle, elbow, for each side. */
export function curves(px, contacts, dir) {
  const out = { knee: {}, hip: {}, ankle: {}, elbow: {} };
  for (const side of ["left", "right"]) {
    const s = SIDES[side];
    const mine = contacts.filter(c => c.side === side);
    const series = {
      knee: px.map(f => 180 - angle(f.p[s.hip], f.p[s.knee], f.p[s.ank])),
      hip: px.map(f => { const sh = mid(f.p, L.lSh, L.rSh), hp = mid(f.p, L.lHip, L.rHip);
        const trunk = Math.atan2(dir * (sh[0] - hp[0]), hp[1] - sh[1]); const thigh = Math.atan2(dir * (f.p[s.knee][0] - f.p[s.hip][0]), f.p[s.knee][1] - f.p[s.hip][1]);
        return (thigh + trunk) * DEG; }),   // + knee in front of the hips
      ankle: px.map(f => angle(f.p[s.knee], f.p[s.ank], f.p[s.toe]) - 90),
      elbow: px.map(f => angle(f.p[s.sh], f.p[s.el], f.p[s.wr])),
    };
    for (const k of Object.keys(series)) {
      const cycles = [];
      for (let i = 0; i + 1 < mine.length; i++) {
        const a = mine[i].on, b = mine[i + 1].on;
        if (b - a < 6) continue;
        cycles.push(Array.from({ length: 51 }, (_, j) => {
          const x = a + (b - a) * j / 50, lo = Math.floor(x), hi = Math.min(b, lo + 1), f = x - lo;
          return series[k][lo] * (1 - f) + series[k][hi] * f;
        }));
      }
      out[k][side] = cycles.length ? cycles[0].map((_, j) => round(mean(cycles.map(c => c[j])), 1)) : [];
    }
  }
  return out;
}

/** The runner at a foot strike, as points scaled into a 0-1 box (facing right), for the stick figure in the report. */
export function figure(p) {
  const xs = p.map(q => q[0]), ys = p.map(q => q[1]);
  const x0 = Math.min(...xs), y0 = Math.min(...ys), s = Math.max(Math.max(...xs) - x0, Math.max(...ys) - y0) || 1;
  const facing = Math.sign(p[L.nose][0] - mid(p, L.lEar, L.rEar)[0]) || 1;
  return p.map(q => [round(facing > 0 ? (q[0] - x0) / s : 1 - (q[0] - x0) / s, 3), round((q[1] - y0) / s, 3)]);
}

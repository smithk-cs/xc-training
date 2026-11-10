// Runs xcapp/static/form-core.js on a made-up runner whose stride we know, and prints what it measured as JSON
// (tests/test_form.py checks the numbers). Usage: node tests/form_core_check.mjs [still|following] [fps] [left|right]
import { analyze } from "../xcapp/static/form-core.js";

const [camera = "still", fpsArg = "60", facing = "right"] = process.argv.slice(2);
const fps = Number(fpsArg), W = 1920, H = 1080;
const CAD = 180, CONTACT = 0.24, LEAN = 7 * Math.PI / 180, LAND_AHEAD = 0.13;   // steps/min, s, rad, × leg length
const leg = 400, thigh = 205, shin = 205, speed = 1200;   // pixels; pixels a second across the image (still camera)
const step = 60 / CAD, stride = 2 * step, groundY = 900;
const dir = facing === "right" ? 1 : -1;

function ik(hip, ank, bendDir) {   // knee position: two segments, bending forward (dir) at the knee
  const dx = ank[0] - hip[0], dy = ank[1] - hip[1], d = Math.min(Math.hypot(dx, dy), thigh + shin - 1e-6);
  const a = Math.acos((thigh * thigh + d * d - shin * shin) / (2 * thigh * d));
  const base = Math.atan2(dy, dx);
  const ang = base - bendDir * a;
  return [hip[0] + thigh * Math.cos(ang), hip[1] + thigh * Math.sin(ang)];
}

const frames = [];
for (let i = 0; i < Math.round(3.2 * fps); i++) {
  const t = i / fps;
  const hipX = camera === "still" ? 200 + dir * speed * t + (dir < 0 ? 1500 : 0) : 960;
  const travel = camera === "still" ? speed : speed;   // ground moves past at running speed either way
  const hipY = groundY - leg * 0.93 + 18 * Math.cos(2 * Math.PI * t / step);   // bob: lowest mid-stance
  const p = new Array(33).fill(null).map(() => [0, 0, 0.95]);
  const feet = {};
  for (const [side, phase0] of [["left", 0], ["right", step]]) {
    let ph = ((t - phase0) % stride + stride) % stride;   // time since this foot's strike
    let ax, ay;
    if (ph < CONTACT) {   // on the ground: lands ahead of the hips, then the body passes over it
      ax = hipX + dir * (LAND_AHEAD * leg - travel * ph);
      ay = groundY;
    } else {              // swinging through: from behind to ahead, lifting the heel
      const u = (ph - CONTACT) / (stride - CONTACT);
      const back = LAND_AHEAD * leg - travel * CONTACT;
      ax = hipX + dir * (back + (LAND_AHEAD * leg - back) * u);
      ay = groundY - 170 * Math.sin(Math.PI * u) ** 1.5;
    }
    if (camera !== "still") ax += 0;   // same picture relative to the hips; the stance is found from the feet's height
    const hip = [hipX + dir * (side === "left" ? 0 : 0), hipY];
    const knee = ik(hip, [ax, ay], -dir);
    feet[side] = { hip, knee, ank: [ax, ay] };
  }
  const L = { lSh: 11, rSh: 12, lEl: 13, rEl: 14, lWr: 15, rWr: 16, lHip: 23, rHip: 24, lKnee: 25, rKnee: 26, lAnk: 27, rAnk: 28, lHeel: 29, rHeel: 30, lToe: 31, rToe: 32 };
  const sh = [hipX + dir * Math.sin(LEAN) * 330, hipY - Math.cos(LEAN) * 330];
  for (const [s, k] of [["left", "l"], ["right", "r"]]) {
    const f = feet[s];
    p[L[k + "Hip"]] = [...f.hip, 0.95]; p[L[k + "Knee"]] = [...f.knee, 0.95]; p[L[k + "Ank"]] = [...f.ank, 0.95];
    p[L[k + "Heel"]] = [f.ank[0] - dir * 25, f.ank[1] + 8, 0.9]; p[L[k + "Toe"]] = [f.ank[0] + dir * 45, f.ank[1] + 8, 0.9];
    p[L[k + "Sh"]] = [...sh, 0.95];
    const swing = Math.sin(2 * Math.PI * t / stride + (s === "left" ? Math.PI : 0)) * 0.6;
    const el = [sh[0] + 120 * Math.sin(swing) * dir, sh[1] + 120 * Math.cos(swing)];
    p[L[k + "El"]] = [...el, 0.9];
    p[L[k + "Wr"]] = [el[0] + dir * 110 * Math.cos(swing), el[1] - 110 * Math.sin(Math.max(0, swing)) + 0, 0.9];   // about 90° at the elbow
  }
  const head = [sh[0] + dir * 30, sh[1] - 90];
  p[0] = [head[0] + dir * 40, head[1] + 8, 0.95];          // nose a little below the ears
  p[7] = [head[0] - dir * 10, head[1], 0.9]; p[8] = [head[0] - dir * 10, head[1], 0.9];
  p[2] = [head[0] + dir * 30, head[1] - 4, 0.9]; p[5] = [head[0] + dir * 30, head[1] - 4, 0.9];
  frames.push({ t, p: p.map(q => [q[0] / W, q[1] / H, q[2]]) });
}
const r = analyze(frames, W, H);
console.log(JSON.stringify({ ...r, curves: Object.fromEntries(Object.entries(r.curves).map(([k, v]) => [k, { left: v.left.length, right: v.right.length, kneeMax: k === "knee" ? Math.max(...v.left) : null }])) }));

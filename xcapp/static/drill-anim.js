/* Animated stick-figure demonstrations of the form drills (drills.py). Each <div class="drill-anim" data-drill="key">
   gets a looping figure drawn on a canvas, with Play/Pause and Slow buttons. The figure faces right; the near side
   (left arm and leg) is orange like the form report's figure, the far side blue.

   A pose is a set of joint angles in degrees: lean (trunk from vertical, + forward), hL/hR (thigh from vertical,
   + forward), kL/kR (knee bend), aL/aR (ankle, + toes up), sL/sR (upper arm from vertical, + forward), eL/eR (elbow
   bend), lift (height off the ground, in leg lengths), rot (whole body, for lying down), v (how fast the ground
   moves by, for drills that travel). The lowest point of the body is put on the ground, then raised by lift. */
(function () {
  "use strict";
  var DEF = { lean: 0, hL: 0, kL: 5, aL: 0, hR: 0, kR: 5, aR: 0, sL: 0, eL: 80, sR: 0, eR: 80, lift: 0, rot: 0, v: 0 };
  var KEYS = Object.keys(DEF);
  var L = { torso: 0.52, neck: 0.07, head: 0.105, thigh: 0.46, shank: 0.44, foot: 0.15, heel: 0.045, upper: 0.29, fore: 0.27 };

  function P(o) { var p = {}; KEYS.forEach(function (k) { p[k] = o[k] === undefined ? DEF[k] : o[k]; }); return p; }
  function swap(p) {   // the same pose with left and right exchanged
    var q = Object.assign({}, p);
    [["hL", "hR"], ["kL", "kR"], ["aL", "aR"], ["sL", "sR"], ["eL", "eR"]].forEach(function (s) { q[s[0]] = p[s[1]]; q[s[1]] = p[s[0]]; });
    return q;
  }
  // An alternating drill: frames for the first half (left leg working, t from 0 to <0.5); the second half mirrors it.
  function alt(frames) {
    var out = frames.map(function (f) { return Object.assign(P(f), { t: f.t }); });
    return out.concat(frames.map(function (f) { return Object.assign(swap(P(f)), { t: f.t + 0.5 }); }));
  }
  function seq(frames) { return frames.map(function (f) { return Object.assign(P(f), { t: f.t }); }); }

  // A running stride (left foot landing at t = 0), scaled for bounce and stride length
  function gait(o) {
    o = o || {};
    var s = o.stride || 1, b = o.bounce === undefined ? 1 : o.bounce, lean = o.lean || 6, v = o.v || 2.2, arms = o.arms || 1;
    return alt([
      { t: 0, lean: lean, hL: 24 * s, kL: 14, aL: 6, hR: -24 * s, kR: 38, aR: -28, sL: -32 * arms, eL: 85, sR: 34 * arms, eR: 80, lift: 0, v: v },
      { t: 0.14, lean: lean, hL: 4, kL: 32, aL: 2, hR: -2 * s, kR: 112, aR: -8, sL: -10, eL: 85, sR: 12, eR: 85, lift: 0, v: v },
      { t: 0.3, lean: lean, hL: -22 * s, kL: 22, aL: -26, hR: 36 * s, kR: 92, aR: 4, sL: 24 * arms, eL: 80, sR: -28 * arms, eR: 85, lift: 0.035 * b, v: v },
      { t: 0.41, lean: lean, hL: -20 * s, kL: 62, aL: -18, hR: 33 * s, kR: 48, aR: 6, sL: 30 * arms, eL: 80, sR: -32 * arms, eR: 85, lift: 0.07 * b, v: v },
    ]);
  }

  var DRILLS = {
    a_skip: { cycle: 1.0, frames: alt([
      { t: 0, lean: 2, hL: 82, kL: 104, aL: 16, hR: -4, kR: 4, aR: -16, sL: -38, eL: 88, sR: 52, eR: 88, lift: 0.05, v: 0.9 },
      { t: 0.18, lean: 2, hL: 34, kL: 44, aL: 12, hR: -4, kR: 6, aR: -12, sL: -20, sR: 30, lift: 0.025, v: 0.9 },
      { t: 0.3, lean: 2, hL: 0, kL: 6, aL: 0, hR: 2, kR: 22, aR: -6, sL: 0, sR: 0, lift: 0, v: 0.9 },
      { t: 0.42, lean: 2, hL: -4, kL: 8, aL: -16, hR: 30, kR: 60, aR: 10, sL: 30, eL: 88, sR: -24, eR: 88, lift: 0.035, v: 0.9 },
    ]) },
    b_skip: { cycle: 1.1, frames: alt([
      { t: 0, lean: 2, hL: 80, kL: 104, aL: 16, hR: -4, kR: 4, aR: -16, sL: -38, eL: 88, sR: 52, eR: 88, lift: 0.05, v: 0.9 },
      { t: 0.13, lean: 2, hL: 72, kL: 14, aL: 12, hR: -4, kR: 6, aR: -14, sL: -32, sR: 44, lift: 0.04, v: 0.9 },
      { t: 0.25, lean: 2, hL: 24, kL: 4, aL: 4, hR: -2, kR: 10, aR: -10, sL: -12, sR: 18, lift: 0.015, v: 0.9 },
      { t: 0.34, lean: 2, hL: -6, kL: 6, aL: -4, hR: 4, kR: 26, aR: -6, sL: 4, sR: -4, lift: 0, v: 0.9 },
      { t: 0.43, lean: 2, hL: -6, kL: 8, aL: -16, hR: 30, kR: 62, aR: 10, sL: 30, sR: -24, lift: 0.035, v: 0.9 },
    ]) },
    high_knees: { cycle: 0.55, frames: alt([
      { t: 0, lean: 0, hL: 86, kL: 112, aL: 12, hR: -4, kR: 8, aR: -18, sL: -42, eL: 85, sR: 56, eR: 85, lift: 0.03, v: 1.2 },
      { t: 0.25, lean: 0, hL: 30, kL: 64, aL: 4, hR: 22, kR: 50, aR: -4, sL: 0, sR: 0, lift: 0, v: 1.2 },
    ]) },
    butt_kicks: { cycle: 0.55, frames: alt([
      { t: 0, lean: 4, hL: 6, kL: 148, aL: -30, hR: -2, kR: 10, aR: -18, sL: -30, eL: 85, sR: 32, eR: 85, lift: 0.02, v: 1.2 },
      { t: 0.25, lean: 4, hL: 2, kL: 70, aL: -14, hR: 2, kR: 60, aR: -12, sL: 0, sR: 0, lift: 0, v: 1.2 },
    ]) },
    quick_feet: { cycle: 0.34, frames: alt([
      { t: 0, lean: 4, hL: 16, kL: 42, aL: 2, hR: -6, kR: 16, aR: -12, sL: -22, eL: 88, sR: 24, eR: 88, lift: 0.01, v: 0.5 },
      { t: 0.25, lean: 4, hL: 4, kL: 22, aL: -4, hR: 4, kR: 22, aR: -4, sL: 0, sR: 0, lift: 0, v: 0.5 },
    ]) },
    straight_leg_bounds: { cycle: 0.8, frames: alt([
      { t: 0, lean: 2, hL: 46, kL: 6, aL: 12, hR: -24, kR: 6, aR: -20, sL: -44, eL: 70, sR: 50, eR: 70, lift: 0.03, v: 1.8 },
      { t: 0.22, lean: 2, hL: 6, kL: 4, aL: 2, hR: -6, kR: 6, aR: -10, sL: -10, sR: 12, lift: 0, v: 1.8 },
      { t: 0.38, lean: 2, hL: -22, kL: 6, aL: -18, hR: 30, kR: 8, aR: 6, sL: 30, sR: -34, lift: 0.07, v: 1.8 },
    ]) },
    marching: { cycle: 1.8, frames: alt([
      { t: 0, lean: 0, hL: 82, kL: 100, aL: 16, hR: 0, kR: 2, aR: -10, sL: -36, eL: 88, sR: 50, eR: 88, lift: 0, v: 0.4 },
      { t: 0.18, lean: 0, hL: 82, kL: 100, aL: 16, hR: 0, kR: 2, aR: -10, sL: -36, eL: 88, sR: 50, eR: 88, lift: 0, v: 0.4 },
      { t: 0.36, lean: 0, hL: 4, kL: 4, aL: 0, hR: -6, kR: 8, aR: -6, sL: 0, sR: 0, lift: 0, v: 0.4 },
    ]) },
    wall_drill: { cycle: 1.4, wall: true, frames: alt([
      { t: 0, lean: 40, hL: 52, kL: 104, aL: 14, hR: -40, kR: 0, aR: -14, sL: 128, eL: 6, sR: 128, eR: 6 },
      { t: 0.2, lean: 40, hL: 52, kL: 104, aL: 14, hR: -40, kR: 0, aR: -14, sL: 128, eL: 6, sR: 128, eR: 6 },
      { t: 0.36, lean: 40, hL: -38, kL: 2, aL: -14, hR: -40, kR: 0, aR: -14, sL: 128, eL: 6, sR: 128, eR: 6 },
    ]) },
    falling_start: { cycle: 3.6, frames: seq([
      { t: 0, lean: 0, hL: 0, kL: 2, hR: 0, kR: 2, sL: 0, eL: 20, sR: 0, eR: 20 },
      { t: 0.12, lean: 0, hL: 0, kL: 2, hR: 0, kR: 2, sL: 0, eL: 20, sR: 0, eR: 20 },
      { t: 0.32, lean: 18, hL: -18, kL: 2, aL: -8, hR: -18, kR: 2, aR: -8, sL: -10, eL: 40, sR: 10, eR: 40 },
      { t: 0.42, lean: 18, hL: 44, kL: 76, aL: 10, hR: -24, kR: 10, aR: -22, sL: -34, eL: 85, sR: 40, eR: 85, v: 0.8 },
      { t: 0.5, lean: 16, hL: 20, kL: 18, aL: 4, hR: -30, kR: 40, aR: -28, sL: -30, eL: 85, sR: 32, eR: 85, v: 1.6 },
      { t: 0.6, lean: 14, hL: -24, kL: 24, aL: -26, hR: 40, kR: 90, aR: 4, sL: 28, eL: 85, sR: -30, eR: 85, lift: 0.04, v: 2 },
      { t: 0.7, lean: 12, hL: -26, kL: 40, aL: -26, hR: 24, kR: 16, aR: 6, sL: 32, eL: 85, sR: -32, eR: 85, v: 2.2 },
      { t: 0.8, lean: 10, hL: 38, kL: 90, aL: 4, hR: -24, kR: 26, aR: -26, sL: -30, eL: 85, sR: 30, eR: 85, lift: 0.05, v: 2.2 },
      { t: 0.9, lean: 8, hL: 24, kL: 16, aL: 6, hR: -26, kR: 40, aR: -28, sL: -32, eL: 85, sR: 32, eR: 85, v: 1.2 },
      { t: 0.99, lean: 0, hL: 0, kL: 2, hR: 0, kR: 2, sL: 0, eL: 20, sR: 0, eR: 20 },
    ]) },
    strides: { cycle: 0.62, frames: gait({ stride: 1.15, v: 3 }) },
    low_strides: { cycle: 0.62, frames: gait({ stride: 1.1, bounce: 0.35, v: 3 }), ceiling: true },
    tall_strides: { cycle: 0.62, frames: gait({ stride: 1.1, lean: 5, v: 3 }) },
    hill_strides: { cycle: 0.6, slope: 8, frames: gait({ stride: 1.05, lean: 14, v: 2.4, arms: 1.25 }) },
    bounding: { cycle: 0.95, frames: gait({ stride: 1.5, bounce: 2.6, v: 3.4, arms: 1.3 }) },
    metronome: { cycle: 0.67, metronome: true, frames: gait({ v: 2.4 }) },
    bridges: { cycle: 2.6, frames: seq([
      { t: 0, rot: -90, lean: 0, hL: 45, kL: 117, aL: -18, hR: 45, kR: 117, aR: -18, sL: 0, eL: 0, sR: 0, eR: 0 },
      { t: 0.15, rot: -90, lean: 0, hL: 45, kL: 117, aL: -18, hR: 45, kR: 117, aR: -18, sL: 0, eL: 0, sR: 0, eR: 0 },
      { t: 0.45, rot: -90, lean: -24, hL: 25, kL: 106, aL: -9, hR: 25, kR: 106, aR: -9, sL: -24, eL: 0, sR: -24, eR: 0, lift: 0 },
      { t: 0.65, rot: -90, lean: -24, hL: 25, kL: 106, aL: -9, hR: 25, kR: 106, aR: -9, sL: -24, eL: 0, sR: -24, eR: 0, lift: 0 },
      { t: 0.95, rot: -90, lean: 0, hL: 45, kL: 117, aL: -18, hR: 45, kR: 117, aR: -18, sL: 0, eL: 0, sR: 0, eR: 0 },
    ]) },
    single_leg_squat: { cycle: 3, frames: seq([
      { t: 0, lean: 0, hR: 30, kR: 24, aR: 0, hL: 0, kL: 4, aL: 0, sL: 40, eL: 10, sR: 40, eR: 10 },
      { t: 0.12, lean: 0, hR: 30, kR: 24, aR: 0, hL: 0, kL: 4, aL: 0, sL: 40, eL: 10, sR: 40, eR: 10 },
      { t: 0.45, lean: 32, hR: 62, kR: 30, aR: 0, hL: 62, kL: 92, aL: 22, sL: 92, eL: 6, sR: 92, eR: 6 },
      { t: 0.6, lean: 32, hR: 62, kR: 30, aR: 0, hL: 62, kL: 92, aL: 22, sL: 92, eL: 6, sR: 92, eR: 6 },
      { t: 0.95, lean: 0, hR: 30, kR: 24, aR: 0, hL: 0, kL: 4, aL: 0, sL: 40, eL: 10, sR: 40, eR: 10 },
    ]) },
    plank: { cycle: 3, frames: seq([
      { t: 0, rot: 90, lean: 0, hL: 0, kL: 0, aL: 0, hR: 0, kR: 0, aR: 0, sL: 90, eL: 90, sR: 90, eR: 90 },
      { t: 0.5, rot: 89, lean: 0, hL: 0, kL: 0, aL: 0, hR: 0, kR: 0, aR: 0, sL: 90, eL: 90, sR: 90, eR: 90 },
    ]), hold: "Hold 30-45 s" },
    dead_bug: { cycle: 3.2, frames: alt([
      { t: 0, rot: -90, hL: 90, kL: 90, aL: 0, hR: 90, kR: 90, aR: 0, sL: 90, eL: 0, sR: 90, eR: 0 },
      { t: 0.06, rot: -90, hL: 90, kL: 90, aL: 0, hR: 90, kR: 90, aR: 0, sL: 90, eL: 0, sR: 90, eR: 0 },
      { t: 0.24, rot: -90, hL: 12, kL: 4, aL: -10, hR: 90, kR: 90, aR: 0, sL: 90, eL: 0, sR: 172, eR: 0 },
      { t: 0.32, rot: -90, hL: 12, kL: 4, aL: -10, hR: 90, kR: 90, aR: 0, sL: 90, eL: 0, sR: 172, eR: 0 },
    ]) },
    arm_swing: { cycle: 0.75, frames: alt([
      { t: 0, lean: 2, hL: 10, kL: 6, hR: -10, kR: 6, aR: -6, sL: -38, eL: 90, sR: 44, eR: 90 },
      { t: 0.25, lean: 2, hL: 10, kL: 6, hR: -10, kR: 6, aR: -6, sL: 3, eL: 90, sR: 3, eR: 90 },
    ]).map(function (f) { return Object.assign(f, f.t >= 0.5 ? { hL: 10, kL: 6, aL: 0, hR: -10, kR: 6, aR: -6 } : {}); }) },
  };

  function ease(x) { return x * x * (3 - 2 * x); }
  function poseAt(frames, t) {
    var n = frames.length, i = 0;
    while (i < n - 1 && frames[i + 1].t <= t) i++;
    var a = frames[i], b = frames[(i + 1) % n], t0 = a.t, t1 = i + 1 < n ? b.t : 1 + frames[0].t;
    var tt = t < t0 ? t + 1 : t, f = t1 > t0 ? ease(Math.min(1, Math.max(0, (tt - t0) / (t1 - t0)))) : 0, p = {};
    KEYS.forEach(function (k) { p[k] = a[k] + (b[k] - a[k]) * f; });
    return p;
  }

  var R = Math.PI / 180;
  function dir(deg) { return [Math.sin(deg * R), Math.cos(deg * R)]; }        // down = 0°, forward = +
  function add(p, d, len) { return [p[0] + d[0] * len, p[1] + d[1] * len]; }
  // Joint positions in leg lengths, the hip at (0, 0), y down
  function skeleton(p) {
    var hip = [0, 0], up = [Math.sin(p.lean * R), -Math.cos(p.lean * R)];
    var sh = add(hip, up, L.torso), neck = add(sh, up, L.neck), head = add(neck, up, L.head);
    function leg(h, k, a) {
      var knee = add(hip, dir(h), L.thigh), sa = h - k, ankle = add(knee, dir(sa), L.shank);
      var fd = dir(sa + 90 + a);
      return { knee: knee, ankle: ankle, toe: add(ankle, fd, L.foot), heel: add(ankle, fd, -L.heel) };
    }
    function arm(s, e) {
      var elbow = add(sh, dir(s), L.upper);
      return { elbow: elbow, hand: add(elbow, dir(s + e), L.fore) };
    }
    var out = { hip: hip, sh: sh, head: head, L: leg(p.hL, p.kL, p.aL), R: leg(p.hR, p.kR, p.aR), aL: arm(p.sL, p.eL), aR: arm(p.sR, p.eR) };
    if (p.rot) {
      var c = Math.cos(p.rot * R), s = Math.sin(p.rot * R);
      var rot = function (q) { return [q[0] * c - q[1] * s, q[0] * s + q[1] * c]; };
      out = JSON.parse(JSON.stringify(out), function (k, v) { return Array.isArray(v) && typeof v[0] === "number" ? rot(v) : v; });
    }
    return out;
  }
  function points(sk) {
    return [sk.hip, sk.sh, sk.L.knee, sk.L.ankle, sk.L.toe, sk.L.heel, sk.R.knee, sk.R.ankle, sk.R.toe, sk.R.heel,
            sk.aL.elbow, sk.aL.hand, sk.aR.elbow, sk.aR.hand, [sk.head[0], sk.head[1] + L.head]];
  }

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function Player(el) {
    var spec = DRILLS[el.dataset.drill];
    if (!spec) return;
    var canvas = document.createElement("canvas"), bar = document.createElement("div");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "Animated demonstration: " + (el.dataset.name || el.dataset.drill));
    bar.className = "drill-bar";
    var play = button(""), slow = button("Slow motion");
    bar.appendChild(play); bar.appendChild(slow);
    var bpmBox = null, bpm = Math.round(Number(el.dataset.bpm) || 172);
    if (spec.metronome) {
      bpmBox = document.createElement("label");
      bpmBox.className = "drill-bpm";
      bpmBox.innerHTML = 'Steps a minute <input type="number" min="140" max="210" step="1" inputmode="numeric">';
      var input = bpmBox.querySelector("input");
      input.value = bpm;
      input.addEventListener("change", function () { bpm = Math.min(210, Math.max(140, Number(input.value) || bpm)); input.value = bpm; });
      bar.appendChild(bpmBox);
      var sound = button("Sound on");
      bar.appendChild(sound);
    }
    el.appendChild(canvas); el.appendChild(bar);
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var running = !reduce, speed = 1, phase = 0, ground = 0, last = 0, audio = null, soundOn = false, lastStep = -1;
    if (el.dataset.phase) { phase = Number(el.dataset.phase) % 1; running = false; }   // a still picture at that moment
    function label() { play.textContent = running ? "Pause" : "Play"; slow.setAttribute("aria-pressed", String(speed < 1)); }
    play.addEventListener("click", function () { running = !running; label(); if (running) tick(); });
    slow.addEventListener("click", function () { speed = speed < 1 ? 1 : 0.35; label(); });
    if (spec.metronome) {
      sound.addEventListener("click", function () {
        soundOn = !soundOn;
        sound.textContent = soundOn ? "Sound off" : "Sound on";
        if (soundOn && !audio) { var AC = window.AudioContext || window.webkitAudioContext; if (AC) audio = new AC(); }
        if (audio && audio.state === "suspended") audio.resume();
        if (soundOn && !running) { running = true; label(); tick(); }
      });
    }
    function click() {
      if (!audio || !soundOn) return;
      var o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = 1500; g.gain.setValueAtTime(0.25, audio.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.05);
      o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + 0.06);
    }
    function cycle() { return spec.metronome ? 120 / bpm : spec.cycle; }   // a stride is two steps
    var fit = (function () {   // the drill's whole movement: widest and tallest, and its middle, so the zoom stays put
      var w = 0, hgt = 0, mid = 0, n = 0;
      for (var t = 0; t < 1; t += 0.02) {
        var q = poseAt(spec.frames, t), pts = points(skeleton(q));
        var xs = pts.map(function (a) { return a[0]; }), ys = pts.map(function (a) { return a[1]; });
        var lo = Math.min.apply(null, xs) - L.head, hi = Math.max.apply(null, xs) + L.head;
        w = Math.max(w, hi - lo); hgt = Math.max(hgt, Math.max.apply(null, ys) - Math.min.apply(null, ys) + L.head + q.lift);
        mid += (lo + hi) / 2; n++;
      }
      return { w: w, h: hgt, mid: mid / n };
    })();

    function draw() {
      var w = canvas.clientWidth, h = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
      var ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      var p = poseAt(spec.frames, phase), sk = skeleton(p), pts = points(sk);
      var lowest = Math.max.apply(null, pts.map(function (q) { return q[1]; }));
      var scale = Math.min(h * 0.72 / fit.h, w * 0.86 / fit.w, h / 2.2), gy = h * 0.86;
      var cx = w / 2 - fit.mid * scale - (spec.wall ? 7 : 0);
      function X(q) { return cx + q[0] * scale; }
      function Y(q) { return gy - (lowest - q[1]) * scale - p.lift * scale; }
      var ink = css("--color-fg-muted", "#6b645c"), line = css("--color-border-line", "#e4ded6");
      ctx.save();
      if (spec.slope) { ctx.translate(w / 2, gy); ctx.rotate(-spec.slope * R); ctx.translate(-w / 2, -gy); }
      // ground, with marks that move by for drills that travel
      ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(-w, gy + 1); ctx.lineTo(2 * w, gy + 1); ctx.stroke();
      ctx.strokeStyle = line; ctx.lineWidth = 3;
      var gap = 0.5 * scale, off = (ground * scale) % gap;
      for (var x = -w - off; x < 2 * w; x += gap) { ctx.beginPath(); ctx.moveTo(x, gy + 8); ctx.lineTo(x + gap * 0.4, gy + 8); ctx.stroke(); }
      if (spec.ceiling) {   // low and smooth: the head stays under a line
        ctx.setLineDash([6, 6]); ctx.strokeStyle = ink; ctx.lineWidth = 1;
        var top = gy - (lowest - sk.head[1] + L.head + 0.06) * scale - 0.035 * scale;
        ctx.beginPath(); ctx.moveTo(0, top); ctx.lineTo(w, top); ctx.stroke(); ctx.setLineDash([]);
      }
      if (spec.wall) {
        var wx = Math.max(X(sk.aL.hand), X(sk.aR.hand)) + 4;
        ctx.fillStyle = line; ctx.fillRect(wx, gy - 2.3 * scale, 14, 2.3 * scale);
        ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.strokeRect(wx, gy - 2.3 * scale, 14, 2.3 * scale);
      }
      var near = "#ff5a36", far = "#5b6cff", lw = Math.max(3, scale * 0.035);
      ctx.lineCap = "round"; ctx.lineJoin = "round";
      function limb(color, pts2, width) {
        ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); ctx.moveTo(X(pts2[0]), Y(pts2[0]));
        for (var i = 1; i < pts2.length; i++) ctx.lineTo(X(pts2[i]), Y(pts2[i]));
        ctx.stroke();
      }
      ctx.globalAlpha = 0.55;   // far side first, lighter
      limb(far, [sk.sh, sk.aR.elbow, sk.aR.hand], lw);
      limb(far, [sk.hip, sk.R.knee, sk.R.ankle, sk.R.toe], lw); limb(far, [sk.R.ankle, sk.R.heel], lw);
      ctx.globalAlpha = 1;
      limb(far, [sk.hip, sk.sh], lw * 1.25);
      ctx.strokeStyle = far; ctx.lineWidth = lw; ctx.beginPath();
      ctx.arc(X(sk.head), Y(sk.head), L.head * scale, 0, 7); ctx.stroke();
      limb(near, [sk.hip, sk.L.knee, sk.L.ankle, sk.L.toe], lw); limb(near, [sk.L.ankle, sk.L.heel], lw);
      limb(near, [sk.sh, sk.aL.elbow, sk.aL.hand], lw);
      ctx.restore();
      if (spec.hold) {
        ctx.fillStyle = ink; ctx.font = "600 " + Math.round(Math.max(12, scale * 0.07)) + "px Inter, system-ui, sans-serif";
        ctx.fillText(spec.hold, 10, 20);
      }
    }
    function tick(now) {
      if (!running) return;
      if (now !== undefined) {
        var dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
        last = now;
        var p = poseAt(spec.frames, phase);
        phase = (phase + dt * speed / cycle()) % 1;
        ground += dt * speed * p.v;
        if (spec.metronome) {   // a click as each foot lands (t = 0 and 0.5)
          var step = Math.floor(phase * 2);
          if (step !== lastStep) { lastStep = step; click(); }
        }
        draw();
      } else { last = 0; }
      if (visible) requestAnimationFrame(tick);
    }
    var visible = true;
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (es) {
        var was = visible; visible = es[0].isIntersecting;
        if (visible && !was && running) requestAnimationFrame(tick);
      }).observe(canvas);
    }
    label(); draw();
    if (running) requestAnimationFrame(tick);
    window.addEventListener("resize", draw);
  }
  function button(text) { var b = document.createElement("button"); b.type = "button"; b.className = "secondary small-btn"; b.textContent = text; return b; }

  window.DrillAnim = { DRILLS: DRILLS, poseAt: poseAt, skeleton: skeleton };
  function start() { document.querySelectorAll(".drill-anim[data-drill]").forEach(Player); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();

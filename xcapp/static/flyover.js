// Course flyover: a runner moving along the route, with the elevation profile to scrub (see flyover.py for the data).
(function () {
  'use strict';
  var C = JSON.parse(document.getElementById('fly-data').textContent);
  var $ = function (id) { return document.getElementById(id); };
  var NS = 'http://www.w3.org/2000/svg';
  var map = $('fly-map'), prof = $('fly-prof');
  var mile = 0, playing = false, speed = 1, follow = true, view = null, lastTs = 0, dragging = false, ovOn = false;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var SEGC = ['var(--fly-c1)', 'var(--fly-c2)', 'var(--fly-c3)'];
  var GHOST = ['var(--fly-ghost1)', 'var(--fly-ghost2)', 'var(--fly-ghost3)'];
  var FILL = ['var(--fly-fill1)', 'var(--fly-fill2)', 'var(--fly-fill3)'];
  var noHeights = C.heights === 'none';
  var els = {};

  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function at(m) {
    var P = C.P; m = clamp(m, 0, C.total);
    for (var i = 1; i < P.length; i++) { if (m <= P[i][2]) { var a = P[i - 1], b = P[i], p = (m - a[2]) / (b[2] - a[2] || 1); return [a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p]; } }
    var z = P[P.length - 1]; return [z[0], z[1]];
  }
  function elev(m) {
    var E = C.E; m = clamp(m, 0, C.total);
    for (var i = 1; i < E.length; i++) { if (m <= E[i][0]) { var a = E[i - 1], b = E[i], p = (m - a[0]) / (b[0] - a[0] || 1); return a[1] + (b[1] - a[1]) * p; } }
    return E[E.length - 1][1];
  }
  function grade(m) { var h = 0.012, a = Math.max(0, m - h), b = Math.min(C.total, m + h); return (elev(b) - elev(a)) / ((b - a) * 5280 || 1) * 100; }
  function pathD(m0, m1) {
    var a = at(m0), d = 'M' + a[0].toFixed(1) + ' ' + a[1].toFixed(1);
    C.P.forEach(function (p) { if (p[2] > m0 && p[2] < m1) d += 'L' + p[0] + ' ' + p[1]; });
    var b = at(m1); return d + 'L' + b[0].toFixed(1) + ' ' + b[1].toFixed(1);
  }
  // One stretch per mile; the last runs to the finish (a 5K's 3.1 is 2 miles to the finish, not a stretch of 0.1)
  var SEGS = (function () { var s = [], m = 0; while (m + 1 < C.total - 0.25) { s.push([m, m + 1]); m += 1; } s.push([m, C.total]); return s; })();
  function segOf(m) { for (var i = SEGS.length - 1; i >= 0; i--) if (m >= SEGS[i][0]) return i; return 0; }
  function col(list, i) { return list[i % list.length]; }
  if (!C.sections || !C.sections.length) {
    C.sections = SEGS.map(function (s, i) { return [s[0], s[1], i === SEGS.length - 1 && SEGS.length > 1 ? 'Mile ' + (i + 1) + ' to the finish' : 'Mile ' + (i + 1), '']; });
  }

  function buildMap() {
    var h = '<rect class="fly-land" x="-2000" y="-2000" width="5000" height="5000"/>' + (C.base || '');
    if (C.overlay) { var o = C.overlay; h += '<image id="fly-ovimg" href="' + esc(o.href) + '" x="' + o.x + '" y="' + o.y + '" width="' + o.w + '" height="' + o.h + '" preserveAspectRatio="none" style="display:none"/>'; }
    SEGS.forEach(function (s, i) { h += '<path class="fly-ghost" style="stroke:' + col(GHOST, i) + '" d="' + pathD(s[0], s[1]) + '"/>'; });
    SEGS.forEach(function () { h += '<path class="fly-case" d="M0 0"/>'; });
    SEGS.forEach(function (s, i) { h += '<path class="fly-route" style="stroke:' + col(SEGC, i) + '" d="M0 0"/>'; });
    h += '<g id="fly-sym"></g>';
    map.innerHTML = h;
    var s = '';
    (C.LAB || []).forEach(function (l) { s += '<g class="fly-pin" data-x="' + +l[1] + '" data-y="' + +l[2] + '"><text class="t-lab" text-anchor="middle" dy="4" font-size="13">' + esc(l[0]) + '</text></g>'; });
    (C.CP || []).forEach(function (c) { s += '<g class="fly-pin" data-x="' + +c[1] + '" data-y="' + +c[2] + '"><text class="t-cp" text-anchor="middle" dy="5" font-size="16">' + esc(c[0]) + '</text></g>'; });
    C.MK.forEach(function (k) {
      var p = at(k[0]), anchor = 'middle', dx = 0, dy = -12;
      if (k[2] === 'd') dy = 20; if (k[2] === 'l') { anchor = 'end'; dx = -11; dy = 5; } if (k[2] === 'r') { anchor = 'start'; dx = 11; dy = 5; }
      s += '<g class="fly-pin" data-x="' + p[0] + '" data-y="' + p[1] + '"><circle class="fly-mk" r="6" stroke-width="2.5"/><text class="t-mk" text-anchor="' + anchor + '" x="' + dx + '" y="' + dy + '" font-size="15">' + esc(k[1]) + '</text></g>';
    });
    s += '<g class="fly-pin" data-x="' + C.start[0] + '" data-y="' + C.start[1] + '"><circle class="fly-start" r="5" stroke-width="2"/><text class="t-flag" text-anchor="end" x="-9" y="-7" font-size="15">Start</text></g>';
    s += '<g class="fly-pin" data-x="' + C.finish[0] + '" data-y="' + C.finish[1] + '"><rect class="fly-finish" x="-5" y="-5" width="10" height="10" stroke-width="2"/><text class="t-flag" text-anchor="start" x="9" y="-7" font-size="15">Finish</text></g>';
    s += '<g class="fly-pin" id="fly-runner"><circle id="fly-ring" r="10" fill="none" stroke-width="2"/><circle class="fly-dot" r="8" stroke-width="3.5"/></g>';
    map.querySelector('#fly-sym').innerHTML = s;
    els.cases = map.querySelectorAll('.fly-case'); els.routes = map.querySelectorAll('.fly-route');
    els.pins = map.querySelectorAll('.fly-pin'); els.mks = map.querySelectorAll('.fly-mk');
    els.runner = map.querySelector('#fly-runner'); els.ring = map.querySelector('#fly-ring');
  }

  function buildProfile() {
    var T = C.total, span = (C.fmax - C.fmin) || 1, H = function (e) { return 10 + (C.fmax - e) / span * 175; };
    function line(a, b) { var d = '', n = Math.max(2, Math.ceil((b - a) * 200)); for (var i = 0; i <= n; i++) { var m = a + (b - a) * i / n; d += (i ? 'L' : 'M') + (m / T * 1000).toFixed(1) + ' ' + H(elev(m)).toFixed(1); } return d; }
    function area(a, b) { return line(a, b) + 'L' + (b / T * 1000).toFixed(1) + ' 200L' + (a / T * 1000).toFixed(1) + ' 200Z'; }
    var h = '<defs><clipPath id="fly-doneclip"><rect id="fly-donerect" x="0" y="-20" width="0" height="240"/></clipPath></defs>';
    h += '<path d="' + area(0, T) + '" style="fill:var(--fly-area)"/>';
    SEGS.forEach(function (s, i) { h += '<path clip-path="url(#fly-doneclip)" d="' + area(s[0], s[1]) + '" style="fill:' + col(FILL, i) + '"/>'; });
    C.MK.forEach(function (k) { var x = (k[0] / T * 1000).toFixed(1); h += '<line x1="' + x + '" x2="' + x + '" y1="0" y2="200" style="stroke:var(--fly-line)" stroke-width="1.5" stroke-dasharray="4 5" vector-effect="non-scaling-stroke"/>'; });
    SEGS.forEach(function (s, i) { h += '<path d="' + line(s[0], s[1]) + '" fill="none" style="stroke:' + col(SEGC, i) + '" stroke-width="3.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'; });
    prof.innerHTML = h;
    var g = '';
    if (!noHeights) (C.grid || []).forEach(function (f) { g += '<div class="fly-grid" style="top:' + (H(f) / 2) + '%"><span>' + Number(f).toLocaleString() + ' ft</span></div>'; });
    $('fly-grids').innerHTML = g;
    var t = '<span class="first" style="left:0">Start</span>';
    C.MK.forEach(function (k) { if (k[0] < T - 0.25) t += '<span' + (k[0] % 1 ? ' class="half"' : '') + ' style="left:' + (k[0] / T * 100) + '%">' + esc(k[1]) + '</span>'; });
    t += '<span class="last" style="left:100%">Finish</span>';
    $('fly-ticks').innerHTML = t;
    els.H = H; els.donerect = $('fly-donerect');
    var lg = '';
    SEGS.forEach(function (s, i) {
      var n = SEGS.length === 1 ? 'Start to finish' : i === 0 ? 'Start to 1 mile'
        : i === SEGS.length - 1 ? s[0] + (s[0] === 1 ? ' mile' : ' miles') + ' to finish' : s[0] + ' to ' + s[1] + ' miles';
      lg += '<div><i style="background:' + col(SEGC, i) + '"></i>' + n + '</div>';
    });
    $('fly-legend').innerHTML = lg;
    els.legend = document.querySelectorAll('#fly-legend div');
  }

  function fullView() { return { x: C.vb[0] + C.vb[2] / 2, y: C.vb[1] + C.vb[3] / 2, w: C.vb[2] }; }
  function targetView() {
    if (!follow) return fullView();
    var a = at(mile - 0.04), b = at(mile + 0.04);
    return { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2, w: C.vb[2] * 0.52 };
  }
  function applyView() {
    var w = view.w;
    map.setAttribute('viewBox', (view.x - w / 2).toFixed(2) + ' ' + (view.y - w / 2).toFixed(2) + ' ' + w.toFixed(2) + ' ' + w.toFixed(2));
    var k = w / (map.clientWidth || 600);
    els.pins.forEach(function (g) {
      var x, y;
      if (g === els.runner) { var p = at(mile); x = p[0]; y = p[1]; } else { x = +g.getAttribute('data-x'); y = +g.getAttribute('data-y'); }
      g.setAttribute('transform', 'translate(' + x.toFixed(2) + ' ' + y.toFixed(2) + ') scale(' + k.toFixed(4) + ')');
    });
  }

  function fmtMi(v) { return (Math.round(v * 100) / 100).toFixed(2).replace(/\.?0+$/, '') || '0'; }
  function render() {
    SEGS.forEach(function (s, i) { var d = mile > s[0] + 1e-4 ? pathD(s[0], Math.min(mile, s[1])) : 'M0 0'; els.routes[i].setAttribute('d', d); els.cases[i].setAttribute('d', d); });
    C.MK.forEach(function (k, i) { els.mks[i].classList.toggle('on', mile >= k[0] - 1e-6); });
    var e = elev(mile), g = Math.round(grade(mile)), si = segOf(mile);
    $('fly-mi').textContent = mile.toFixed(2);
    $('fly-ft').textContent = noHeights ? '–' : Math.round(e).toLocaleString();
    $('fly-gr').textContent = noHeights ? '–' : (g > 0 ? '+' : g < 0 ? '−' : '') + Math.abs(g) + '%';
    var sec = C.sections[0]; C.sections.forEach(function (x) { if (mile >= x[0]) sec = x; });
    $('fly-sec-name').textContent = sec[2];
    $('fly-sec-cp').textContent = 'Miles ' + fmtMi(sec[0]) + '–' + (sec[1] >= C.total - 0.01 ? 'finish' : fmtMi(sec[1])) + (sec[3] ? ' · ' + sec[3] : '');
    $('fly-sec').style.setProperty('--seg', col(SEGC, si));
    var note = C.notes[0] ? C.notes[0][1] : ''; C.notes.forEach(function (n) { if (mile >= n[0]) note = n[1]; });
    if (mile >= C.total - 0.002) note = 'Finish';
    var nn = $('fly-note').querySelector('span'); if (nn.textContent !== note) nn.textContent = note;
    els.legend.forEach(function (d, i) { d.classList.toggle('on', i === si); });
    var pct = mile / C.total * 100;
    els.donerect.setAttribute('width', (pct * 10).toFixed(1));
    $('fly-cursor').style.left = pct + '%';
    var kn = $('fly-knob'); kn.style.left = pct + '%'; kn.style.top = (els.H(e) / 2) + '%'; kn.style.background = col(SEGC, si);
    var sc = $('fly-scrub'); sc.setAttribute('aria-valuenow', mile.toFixed(2));
    sc.setAttribute('aria-valuetext', mile.toFixed(2) + ' miles' + (noHeights ? '' : ', ' + Math.round(e) + ' feet'));
  }

  function frame(ts) {
    var dt = lastTs ? Math.min(0.05, (ts - lastTs) / 1000) : 0; lastTs = ts;
    if (playing && !dragging) {
      mile += dt * speed * C.total / 40;
      if (mile >= C.total) { mile = C.total; setPlaying(false); }
      render();
    }
    var t = targetView();
    if (!view) view = t;
    var a = reduce ? 1 : 1 - Math.pow(0.0015, dt);
    view = { x: view.x + (t.x - view.x) * a, y: view.y + (t.y - view.y) * a, w: view.w + (t.w - view.w) * a };
    applyView();
    var r = ((ts / 1000) % 1.4) / 1.4;
    els.ring.setAttribute('r', (9 + r * (reduce ? 0 : 12)).toFixed(2));
    els.ring.style.opacity = reduce ? 0.6 : (1 - r) * 0.7;
    requestAnimationFrame(frame);
  }

  function setPlaying(p) {
    playing = p;
    if (p && mile >= C.total - 0.001) mile = 0;
    $('fly-play').textContent = p ? 'Pause' : (mile >= C.total - 0.001 ? 'Replay' : 'Play');
  }
  $('fly-play').addEventListener('click', function () { setPlaying(!playing); render(); });
  document.querySelectorAll('.flyover [data-speed]').forEach(function (b) {
    b.addEventListener('click', function () {
      speed = +b.getAttribute('data-speed');
      document.querySelectorAll('.flyover [data-speed]').forEach(function (o) { o.setAttribute('aria-pressed', o === b ? 'true' : 'false'); });
    });
  });
  function applyOverlay() {
    $('fly-ov').setAttribute('aria-pressed', ovOn ? 'true' : 'false');
    var img = map.querySelector('#fly-ovimg'); if (img) img.style.display = ovOn ? '' : 'none';
    map.querySelectorAll('.t-cp').forEach(function (el) { el.style.display = ovOn && C.overlay ? 'none' : ''; });
    $('fly-offmap').hidden = !(ovOn && C.official);
  }
  $('fly-ov').addEventListener('click', function () { ovOn = !ovOn; applyOverlay(); });
  $('fly-follow').addEventListener('click', function () { follow = !follow; this.setAttribute('aria-pressed', follow ? 'true' : 'false'); });

  var scrub = $('fly-scrub'), plot = $('fly-plot');
  function mileFromEvent(ev) { var r = plot.getBoundingClientRect(); return clamp((ev.clientX - r.left) / r.width, 0, 1) * C.total; }
  scrub.addEventListener('pointerdown', function (ev) { dragging = true; scrub.setPointerCapture(ev.pointerId); if (playing) setPlaying(false); mile = mileFromEvent(ev); render(); ev.preventDefault(); });
  scrub.addEventListener('pointermove', function (ev) { if (!dragging) return; mile = mileFromEvent(ev); render(); });
  function endDrag() { dragging = false; if (!playing) $('fly-play').textContent = mile >= C.total - 0.001 ? 'Replay' : 'Play'; }
  scrub.addEventListener('pointerup', endDrag); scrub.addEventListener('pointercancel', endDrag);
  scrub.addEventListener('keydown', function (ev) {
    var step = ev.shiftKey ? 0.1 : 0.01, used = true;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowUp') mile += step; else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowDown') mile -= step;
    else if (ev.key === 'Home') mile = 0; else if (ev.key === 'End') mile = C.total; else if (ev.key === ' ') { setPlaying(!playing); } else used = false;
    if (used) { mile = clamp(mile, 0, C.total); if (ev.key !== ' ' && playing) setPlaying(false); render(); ev.preventDefault(); }
  });

  buildMap(); buildProfile(); render();
  if (C.overlay || C.official) {
    var ob = $('fly-ov'); ob.hidden = false; ob.textContent = (C.overlay || C.official).label;
    if (C.official) $('fly-offmap').src = C.official.href;
  }
  applyOverlay();
  requestAnimationFrame(frame);
})();

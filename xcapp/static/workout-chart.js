/* A run over its workout: pace (top) and heart rate (bottom) on one time axis, never one chart with two y-axes.
   Each rep's target band is shaded behind the line; pauses (watch stopped) are hatched. One hover line moves across
   both charts with a tooltip. Data: JSON in the element's <script type="application/json">. */
(function () {
  'use strict';
  const mmss = (s) => { if (s == null) return '–'; const t = Math.round(s); return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0'); };
  const clock = (s) => { const t = Math.round(s); const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), x = t % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };

  function init(root) {
    const D = JSON.parse(root.querySelector('script[type="application/json"]').textContent);
    const box = root.querySelector('.wc-charts');
    if (!D.t.length) { box.textContent = 'No pace or heart-rate detail for this run.'; return; }
    // drawn at the box's real width, so text stays readable on a phone
    const W = Math.max(320, Math.round(box.clientWidth || 900)), small = W < 600;
    const ML = small ? 42 : 54, MR = small ? 8 : 14, H1 = small ? 170 : 210, H2 = small ? 140 : 170, GAP = 34, MT = 22, MB = 30;
    const H = MT + H1 + GAP + H2 + MB;
    const svg = d3.select(box).append('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('width', '100%')
      .attr('role', 'img').attr('aria-label', 'Pace and heart rate over the run, with each rep’s target zone');
    const defs = svg.append('defs');
    defs.append('pattern').attr('id', 'wc-hatch').attr('width', 6).attr('height', 6).attr('patternUnits', 'userSpaceOnUse')
      .attr('patternTransform', 'rotate(45)').append('line').attr('x1', 0).attr('y1', 0).attr('x2', 0).attr('y2', 6)
      .attr('stroke', 'var(--cds-chart-muted)').attr('stroke-width', 2);

    const x = d3.scaleLinear([D.t[0], D.t[D.t.length - 1]], [ML, W - MR]);
    const pts = D.t.map((t, i) => ({ t, pace: D.pace[i], hr: D.hr[i] }));
    const paceVals = pts.map((p) => p.pace).filter((p) => p != null && p < 15 * 60);
    const bandPaces = D.reps.flatMap((r) => r.pace || []);
    const pLo = Math.min(d3.quantile(paceVals.slice().sort(d3.ascending), 0.02) || 300, ...bandPaces) - 10;
    const pHi = Math.min(Math.max(d3.quantile(paceVals.slice().sort(d3.ascending), 0.98) || 600, ...bandPaces) + 15, 15 * 60);
    const yP = d3.scaleLinear([pLo, pHi], [MT, MT + H1]);           // faster (lower seconds) at the top
    const hrVals = pts.map((p) => p.hr).filter((h) => h);
    const bandHr = D.reps.flatMap((r) => r.hr || []);
    const top2 = MT + H1 + GAP;
    const yH = d3.scaleLinear([Math.min(d3.min(hrVals) || 100, ...bandHr) - 5, Math.max(d3.max(hrVals) || 180, ...bandHr) + 5], [top2 + H2, top2]).nice();

    const panels = [
      { y: yP, top: MT, h: H1, key: 'pace', band: 'pace', title: 'Pace (min/mi), faster at the top', fmt: mmss, color: 'var(--dash-c1)' },
      { y: yH, top: top2, h: H2, key: 'hr', band: 'hr', title: 'Heart rate (beats/min)', fmt: (v) => Math.round(v), color: 'var(--dash-c2)' },
    ];
    const ticksX = x.ticks(small ? 4 : 8);
    panels.forEach((pn) => {
      const g = svg.append('g');
      g.append('text').attr('x', ML).attr('y', pn.top - 8).attr('class', 'wc-title').text(pn.title);
      pn.y.ticks(5).forEach((v) => {
        g.append('line').attr('x1', ML).attr('x2', W - MR).attr('y1', pn.y(v)).attr('y2', pn.y(v)).attr('stroke', 'var(--cds-chart-grid)');
        g.append('text').attr('x', ML - 6).attr('y', pn.y(v) + 4).attr('text-anchor', 'end').attr('class', 'wc-axis').text(pn.fmt(v));
      });
      // target bands behind the line
      D.reps.forEach((r) => {
        const b = r[pn.band];
        if (!b) return;
        const [a, c] = pn.key === 'pace' ? [pn.y(b[0]), pn.y(b[1])] : [pn.y(b[1]), pn.y(b[0])];
        g.append('rect').attr('x', x(r.t0)).attr('width', Math.max(2, x(r.t1) - x(r.t0))).attr('y', Math.max(pn.top, a))
          .attr('height', Math.max(2, Math.min(pn.top + pn.h, c) - Math.max(pn.top, a)))
          .attr('class', r.easy ? 'wc-band easy' : 'wc-band');
      });
      D.pauses.forEach(([a, b]) => g.append('rect').attr('x', x(a)).attr('width', Math.max(2, x(b) - x(a)))
        .attr('y', pn.top).attr('height', pn.h).attr('fill', 'url(#wc-hatch)').attr('opacity', 0.55));
      const line = d3.line().defined((p) => p[pn.key] != null && (pn.key !== 'pace' || p.pace < 15 * 60))
        .x((p) => x(p.t)).y((p) => pn.y(Math.max(pn.y.domain()[0], Math.min(pn.y.domain()[1], p[pn.key]))));
      // break the line across pauses
      const segs = []; let cur = [];
      pts.forEach((p, i) => { if (i && p.t - pts[i - 1].t > 10) { segs.push(cur); cur = []; } cur.push(p); });
      segs.push(cur);
      segs.forEach((s) => g.append('path').attr('d', line(s)).attr('fill', 'none').attr('stroke', pn.color)
        .attr('stroke-width', 2).attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round'));
      g.append('line').attr('x1', ML).attr('x2', W - MR).attr('y1', pn.top + pn.h).attr('y2', pn.top + pn.h).attr('stroke', 'var(--cds-chart-reference)');
    });
    // rep labels above the pace chart, pause labels on the heart-rate chart
    D.reps.filter((r) => !r.easy).forEach((r, i) => svg.append('text').attr('x', (x(r.t0) + x(r.t1)) / 2).attr('y', MT + 12)
      .attr('text-anchor', 'middle').attr('class', 'wc-rep').text(i + 1));
    D.pauses.filter(([a, b]) => b - a >= 30 && x(b) - x(a) > 26).forEach(([a, b]) => svg.append('text')
      .attr('x', (x(a) + x(b)) / 2).attr('y', top2 + H2 - 6).attr('text-anchor', 'middle').attr('class', 'wc-axis').text('⏸ ' + mmss(b - a)));
    ticksX.forEach((v) => svg.append('text').attr('x', x(v)).attr('y', H - 8).attr('text-anchor', 'middle').attr('class', 'wc-axis').text(clock(v)));

    // hover: one line across both charts, and a tooltip
    const cross = svg.append('line').attr('y1', MT).attr('y2', top2 + H2).attr('stroke', 'var(--cds-chart-reference)').attr('stroke-width', 1).attr('visibility', 'hidden');
    const dots = panels.map((pn) => svg.append('circle').attr('r', 4.5).attr('fill', pn.color).attr('stroke', 'var(--color-panel)').attr('stroke-width', 2).attr('visibility', 'hidden'));
    let tip = box.querySelector('.ac-tip');
    if (!tip) { tip = document.createElement('div'); tip.className = 'ac-tip'; tip.hidden = true; box.append(tip); }
    const ts = D.t;
    const hit = svg.append('rect').attr('x', ML).attr('y', MT).attr('width', W - ML - MR).attr('height', top2 + H2 - MT).attr('fill', 'transparent');
    const show = (event) => {
      const [px] = d3.pointer(event, svg.node());
      const t = x.invert(px);
      let i = d3.bisectLeft(ts, t); if (i > 0 && (i >= ts.length || t - ts[i - 1] < ts[i] - t)) i -= 1;
      const p = pts[i]; if (!p) return;
      cross.attr('x1', x(p.t)).attr('x2', x(p.t)).attr('visibility', 'visible');
      panels.forEach((pn, k) => { const v = p[pn.key]; const ok = v != null && (pn.key !== 'pace' || v < 15 * 60);
        dots[k].attr('cx', x(p.t)).attr('cy', ok ? pn.y(Math.max(pn.y.domain()[0], Math.min(pn.y.domain()[1], v))) : 0).attr('visibility', ok ? 'visible' : 'hidden'); });
      const rep = D.reps.find((r) => p.t >= r.t0 && p.t <= r.t1);
      const pause = D.pauses.find(([a, b]) => t >= a && t <= b);
      const lines = [clock(p.t) + (rep ? ' · ' + rep.label : '')];
      if (pause) lines.push('Watch paused for ' + mmss(pause[1] - pause[0]));
      else {
        lines.push('Pace ' + (p.pace != null && p.pace < 15 * 60 ? mmss(p.pace) + '/mi' : '–') + (rep && rep.pace ? ' (target ' + mmss(rep.pace[0]) + '–' + mmss(rep.pace[1]) + ')' : ''));
        lines.push('Heart rate ' + (p.hr ? Math.round(p.hr) : '–') + (rep && rep.hr ? ' (target ' + Math.round(rep.hr[0]) + '–' + Math.round(rep.hr[1]) + ')' : ''));
      }
      tip.replaceChildren(...lines.map((s, k) => { const d = document.createElement('div'); d.textContent = s; if (!k) d.style.fontWeight = '600'; return d; }));
      tip.hidden = false;
      const bw = box.getBoundingClientRect().width, scale = bw / W;
      const left = x(p.t) * scale;
      tip.style.left = Math.min(Math.max(0, left + 12), bw - tip.offsetWidth) + 'px';
      tip.style.top = '8px';
    };
    hit.on('pointermove', show).on('pointerdown', show)
      .on('pointerleave', () => { cross.attr('visibility', 'hidden'); dots.forEach((d) => d.attr('visibility', 'hidden')); tip.hidden = true; });
  }

  const start = () => document.querySelectorAll('[data-workout-chart]').forEach(init);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();

/* Athlete training charts: one chart, two dropdowns (what to show, and over what time range).
   Used on the athlete's own page and on the coach's athlete page. Data: the element's data-src JSON. */
(function () {
  'use strict';
  const DAY = 864e5;
  const parse = (s) => new Date(s + 'T00:00:00Z');
  const iso = (d) => d.toISOString().slice(0, 10);
  const addDays = (d, n) => new Date(+d + n * DAY);
  const fmtPace = (s) => { const t = Math.round(s); const m = Math.floor(t / 60); return m + ':' + String(t - m * 60).padStart(2, '0'); };
  const fmtTime = (s) => { const t = Math.round(s * 10) / 10; const m = Math.floor(t / 60); return m + ':' + (t - m * 60).toFixed(1).padStart(4, '0'); };
  const C = { main: 'var(--dash-c1)', alt: 'var(--dash-c2)', flag: 'var(--cds-chart-status-critical)', grid: 'var(--cds-chart-grid)', ref: 'var(--cds-chart-reference)' };
  const EVENTS = ['1 Mile', '1.5 Mile', '2 Mile', '5K'];
  const EVENT_COLORS = { '1 Mile': 'var(--dash-c1)', '1.5 Mile': 'var(--dash-c2)', '2 Mile': 'var(--dash-c3)', '5K': 'var(--dash-c4)' };
  const NAMES = { miles: 'Miles', load: 'Load ratio', effort: 'Effort', hr: 'Heart rate', pace: 'Pace', results: 'Benchmark times' };

  const start = () => document.querySelectorAll('[data-athlete-charts]').forEach(init);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();

  function init(root) {
    const typeSel = root.querySelector('.ac-type');
    const rangeSel = root.querySelector('.ac-range');
    const box = root.querySelector('.ac-chart');
    const summary = root.querySelector('.ac-summary');
    const legend = root.querySelector('.ac-legend');
    let D = null;

    const empty = (text) => { const p = document.createElement('div'); p.className = 'ac-empty'; p.textContent = text; box.replaceChildren(p); };
    fetch(root.dataset.src, { credentials: 'same-origin' })
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then((d) => { D = prep(d); draw(); root.dataset.ready = '1'; })
      .catch(() => { empty('Charts could not be loaded. Reload the page to try again.'); summary.textContent = ''; root.dataset.ready = 'error'; });
    typeSel.addEventListener('change', draw);
    rangeSel.addEventListener('change', draw);
    let rt = null;
    window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(draw, 150); });

    function prep(d) {
      const today = parse(d.today);
      const runs = d.runs.map((r) => ({ ...r, d: parse(r.date) }));
      const daily = new Map();
      runs.forEach((r) => daily.set(r.date, (daily.get(r.date) || 0) + r.miles));
      return {
        ...d, today, runs, daily, first: d.first_run ? parse(d.first_run) : null,
        results: d.results.map((r) => ({ ...r, d: parse(r.date) })),
        pain: d.pain.map((p) => ({ ...p, d: parse(p.date) })),
        injuries: d.injuries.map((i) => ({ ...i, a: parse(i.onset), b: i.return ? parse(i.return) : addDays(today, 1) })),
        weeks: d.weeks.map((w) => ({ ...w, d: parse(w.week_start) })),
        seasonStart: parse(d.season.start),
      };
    }

    function range() {
      const r = rangeSel.value, t = D.today;
      if (r === 'week') return { start: addDays(t, -6), end: t, bin: 'day', label: 'past week', phrase: 'the past week' };
      if (r === 'month') return { start: addDays(t, -29), end: t, bin: 'day', label: 'past month', phrase: 'the past month' };
      if (r === 'season') return { start: D.seasonStart <= t ? D.seasonStart : addDays(t, -29), end: t, bin: 'week', label: D.season.name + ' season', phrase: 'the ' + D.season.name + ' season' };
      // All years: training charts start at the first run; benchmark times also reach back to older results.
      const dates = D.runs.map((x) => +x.d).concat(typeSel.value === 'results' ? D.results.map((x) => +x.d) : []);
      const s = dates.length ? new Date(Math.min(...dates)) : addDays(t, -365);
      return { start: s, end: t, bin: 'month', label: 'all years', phrase: 'all years on record' };
    }

    function bins(rg) {
      const out = [];
      if (rg.bin === 'day') {
        for (let d = rg.start; d <= rg.end; d = addDays(d, 1)) out.push({ a: d, b: d });
      } else if (rg.bin === 'week') {
        for (let e = rg.end; addDays(e, -6) >= addDays(rg.start, -6); e = addDays(e, -7)) out.unshift({ a: addDays(e, -6), b: e });
      } else {
        let y = rg.start.getUTCFullYear(), m = rg.start.getUTCMonth();
        while (Date.UTC(y, m, 1) <= +rg.end) {
          out.push({ a: new Date(Date.UTC(y, m, 1)), b: addDays(new Date(Date.UTC(y, m + 1, 1)), -1) });
          m += 1; if (m === 12) { m = 0; y += 1; }
        }
      }
      return out;
    }

    function ratioOn(t) {
      if (!D.first || t < addDays(D.first, 34)) return null;
      let acute = 0, chronic = 0;
      for (let i = 0; i < 7; i++) acute += D.daily.get(iso(addDays(t, -i))) || 0;
      for (let i = 7; i < 35; i++) chronic += D.daily.get(iso(addDays(t, -i))) || 0;
      chronic /= 4;
      return chronic > 0 ? acute / chronic : null;
    }

    // ---------- shared drawing pieces ----------
    function frame(H) {
      box.replaceChildren();
      const W = Math.max(280, box.clientWidth || 360);
      const svg = d3.select(box).append('svg').attr('width', '100%').attr('viewBox', `0 0 ${W} ${H}`);
      return { svg, W, H };
    }
    const textW = (svg, s) => { const t = svg.append('text').attr('class', 'ac-tick').text(s); const w = t.node().getComputedTextLength(); t.remove(); return w; };
    function yGrid(svg, y, ticks, fmt, x0, x1) {
      svg.append('g').selectAll('line').data(ticks).join('line').attr('x1', x0).attr('x2', x1).attr('y1', y).attr('y2', y).attr('stroke', C.grid);
      svg.append('g').selectAll('text').data(ticks).join('text').attr('class', 'ac-tick').attr('x', x0 - 6).attr('y', y).attr('dy', '0.32em').attr('text-anchor', 'end').text(fmt);
    }
    function xAxis(svg, x, rg, y0) {
      const span = (rg.end - rg.start) / DAY;
      const fmt = rg.bin === 'month' ? d3.utcFormat("%b '%y") : span <= 7 ? d3.utcFormat('%a') : d3.utcFormat('%b %-d');
      const W = x.range()[1] - x.range()[0];
      let ticks;
      if (span <= 7) ticks = d3.utcDays(rg.start, addDays(rg.end, 1));
      else ticks = x.ticks(Math.max(2, Math.floor(W / 80)));
      const half = span <= 7 ? (x(addDays(rg.start, 1)) - x(rg.start)) / 2 : 0;
      svg.append('g').selectAll('text').data(ticks).join('text').attr('class', 'ac-tick')
        .attr('x', (t) => x(t) + half).attr('y', y0 + 16).attr('text-anchor', 'middle').text(fmt);
    }
    function overlays(svg, x, rg, top, bottom, points) {
      const endX = addDays(rg.end, 1);
      const inj = D.injuries.filter((i) => i.b > rg.start && i.a <= rg.end);
      svg.append('g').selectAll('rect').data(inj).join('rect')
        .attr('x', (i) => x(i.a < rg.start ? rg.start : i.a))
        .attr('width', (i) => Math.max(2, x(i.b > endX ? endX : i.b) - x(i.a < rg.start ? rg.start : i.a)))
        .attr('y', top).attr('height', bottom - top).attr('fill', C.flag).attr('fill-opacity', 0.1);
      const pains = D.pain.filter((p) => p.d >= rg.start && p.d <= rg.end);
      const tri = d3.symbol(d3.symbolTriangle, 46);
      svg.append('g').selectAll('path').data(pains).join('path').attr('d', tri)
        .attr('transform', (p) => `translate(${x(p.d) + Math.max(3, (x(addDays(p.d, 1)) - x(p.d)) / 2)},${bottom - 7})`).attr('fill', C.flag);
      pains.forEach((p) => points.push({ x: x(p.d) + 3, y: bottom - 7, lines: [d3.utcFormat('%a, %b %-d')(p.d), `Pain ${p.severity}/10: ${p.area}`] }));
      inj.forEach((i) => points.push({ x: x(i.a < rg.start ? rg.start : i.a) + 4, y: top + 8, lines: [i.injury, 'From ' + d3.utcFormat('%b %-d')(i.a) + (i.return ? ' to ' + d3.utcFormat('%b %-d')(i.b) : ', no return date yet')] }));
      return { inj: inj.length, pain: pains.length };
    }
    function hover(svg, W, H, points) {
      let tip = box.querySelector(':scope > .ac-tip');
      if (!tip) { tip = document.createElement('div'); tip.className = 'ac-tip'; tip.hidden = true; box.append(tip); }
      svg.append('rect').attr('x', 0).attr('y', 0).attr('width', W).attr('height', H).attr('fill', 'transparent')
        .on('pointermove', (event) => {
          if (!points.length) return;
          const [px, py] = d3.pointer(event, svg.node());
          let best = null, bd = Infinity;
          for (const p of points) { const dd = Math.abs(p.x - px) + (p.y != null ? Math.abs(p.y - py) * 0.25 : 0); if (dd < bd) { bd = dd; best = p; } }
          if (!best || bd > 60) { tip.hidden = true; return; }
          tip.replaceChildren(...best.lines.map((l, i) => { const e = document.createElement('div'); e.textContent = l; if (i === 0) e.style.fontWeight = '600'; return e; }));
          tip.hidden = false;
          const scale = box.clientWidth ? box.clientWidth / W : 1;
          const left = best.x * scale + 12, w = tip.offsetWidth;
          tip.style.left = (left + w > (box.clientWidth || W) ? Math.max(0, best.x * scale - w - 12) : left) + 'px';
          tip.style.top = Math.max(0, (best.y != null ? best.y : py) * scale - 24) + 'px';
        })
        .on('pointerleave', () => { tip.hidden = true; });
    }
    function setLegend(items) {
      legend.replaceChildren(...items.map(([color, text, shape]) => {
        const s = document.createElement('span'); const i = document.createElement('i');
        i.style.background = color; if (shape === 'soft') i.style.opacity = '0.25'; if (shape === 'line') { i.style.height = '3px'; i.style.verticalAlign = '3px'; }
        s.append(i, document.createTextNode(text)); return s;
      }));
    }
    const overlayLegend = (o) => [...(o.inj ? [[C.flag, 'Injured', 'soft']] : []), ...(o.pain ? [[C.flag, 'Pain report']] : [])];
    function layout(svg, W, H, yTicks, fmt) {
      const ml = Math.ceil(Math.max(20, ...yTicks.map((v) => textW(svg, fmt(v))))) + 12;
      return { ml, mr: 10, top: 12, bottom: H - 28 };
    }
    const runsIn = (rg) => D.runs.filter((r) => r.d >= rg.start && r.d <= rg.end);

    // ---------- the six charts ----------
    function drawMiles(rg) {
      const bs = bins(rg).map((b) => {
        const rs = D.runs.filter((r) => r.d >= b.a && r.d <= b.b);
        const wk = rg.bin === 'week' ? D.weeks.find((w) => w.week_start === iso(b.a)) : null;
        return { ...b, miles: d3.sum(rs, (r) => r.miles), n: rs.length, ratio: wk ? wk.ratio : null };
      });
      const max = d3.max(bs, (b) => b.miles) || 0;
      const { svg, W, H } = frame(250);
      const y0 = d3.scaleLinear([0, Math.max(1, max) * 1.15], [0, 1]).nice();
      const ticks = y0.ticks(4);
      const L = layout(svg, W, H, ticks, String);
      const y = d3.scaleLinear(y0.domain(), [L.bottom, L.top]);
      const x = d3.scaleUtc([bs[0].a, addDays(bs[bs.length - 1].b, 1)], [L.ml, W - L.mr]);
      yGrid(svg, y, ticks, String, L.ml, W - L.mr);
      const points = [];
      const o = overlays(svg, x, rg, L.top, L.bottom, points);
      const flagged = (b) => b.ratio != null && b.ratio > D.load_ratio_flag;
      svg.append('g').selectAll('rect').data(bs.filter((b) => b.miles > 0)).join('rect')
        .attr('x', (b) => x(b.a) + 1).attr('width', (b) => Math.max(1, x(addDays(b.b, 1)) - x(b.a) - 2))
        .attr('y', (b) => y(b.miles)).attr('height', (b) => Math.max(0, y(0) - y(b.miles))).attr('rx', 3)
        .attr('fill', (b) => (flagged(b) ? C.flag : C.main));
      const label = rg.bin === 'day' ? d3.utcFormat('%a, %b %-d') : rg.bin === 'week' ? (b) => 'Week of ' + d3.utcFormat('%b %-d')(b) : d3.utcFormat('%B %Y');
      bs.forEach((b) => points.push({ x: (x(b.a) + x(addDays(b.b, 1))) / 2, y: y(b.miles), lines: [label(b.a), b.miles.toFixed(1) + ' miles, ' + b.n + ' run' + (b.n === 1 ? '' : 's'), ...(b.ratio != null ? ['Load ratio ' + b.ratio.toFixed(2)] : [])] }));
      xAxis(svg, x, rg, L.bottom);
      hover(svg, W, H, points);
      setLegend([[C.main, rg.bin === 'day' ? 'Miles per day' : rg.bin === 'week' ? 'Miles per week' : 'Miles per month'],
        ...(rg.bin === 'week' && bs.some(flagged) ? [[C.flag, 'Week above ' + D.load_ratio_flag + ' load ratio']] : []), ...overlayLegend(o)]);
      const total = d3.sum(bs, (b) => b.miles), n = d3.sum(bs, (b) => b.n);
      const days = (rg.end - rg.start) / DAY + 1;
      return `${n} run${n === 1 ? '' : 's'}, ${total.toFixed(1)} miles in ${rg.phrase}` + (days > 8 ? `, about ${(total / (days / 7)).toFixed(1)} miles a week.` : '.');
    }

    function drawLoad(rg) {
      const pts = [];
      for (let t = rg.start; t <= rg.end; t = addDays(t, 1)) { const r = ratioOn(t); pts.push({ d: t, r }); }
      const have = pts.filter((p) => p.r != null);
      if (!have.length) { empty('The load ratio needs four weeks of runs first.'); setLegend([]); return 'No load ratio yet for ' + rg.phrase + '.'; }
      const { svg, W, H } = frame(250);
      const top = Math.max(1.6, (d3.max(have, (p) => p.r) || 0) * 1.1);
      const y0 = d3.scaleLinear([0, top], [0, 1]).nice();
      const ticks = y0.ticks(4);
      const L = layout(svg, W, H, ticks, (v) => v.toFixed(1));
      const y = d3.scaleLinear(y0.domain(), [L.bottom, L.top]);
      const x = d3.scaleUtc([rg.start, addDays(rg.end, 1)], [L.ml, W - L.mr]);
      yGrid(svg, y, ticks, (v) => v.toFixed(1), L.ml, W - L.mr);
      svg.append('rect').attr('x', L.ml).attr('width', W - L.mr - L.ml).attr('y', y(D.load_ratio_flag)).attr('height', y(0.8) - y(D.load_ratio_flag))
        .attr('fill', C.main).attr('fill-opacity', 0.06);
      const points = [];
      const o = overlays(svg, x, rg, L.top, L.bottom, points);
      svg.append('line').attr('x1', L.ml).attr('x2', W - L.mr).attr('y1', y(D.load_ratio_flag)).attr('y2', y(D.load_ratio_flag))
        .attr('stroke', C.flag).attr('stroke-width', 1.5).attr('stroke-dasharray', '5 4');
      const cx = (p) => x(p.d) + (x(addDays(p.d, 1)) - x(p.d)) / 2;
      svg.append('path').datum(pts).attr('d', d3.line().defined((p) => p.r != null).x(cx).y((p) => y(p.r)))
        .attr('fill', 'none').attr('stroke', C.main).attr('stroke-width', 2).attr('stroke-linejoin', 'round');
      const over = have.filter((p) => p.r > D.load_ratio_flag);
      if (pts.length <= 120) svg.append('g').selectAll('circle').data(over).join('circle').attr('cx', cx).attr('cy', (p) => y(p.r)).attr('r', 3.5).attr('fill', C.flag);
      have.forEach((p) => points.push({ x: cx(p), y: y(p.r), lines: [d3.utcFormat('%a, %b %-d')(p.d), 'Load ratio ' + p.r.toFixed(2)] }));
      xAxis(svg, x, rg, L.bottom);
      hover(svg, W, H, points);
      setLegend([[C.main, 'Last 7 days ÷ previous 4-week average', 'line'], [C.flag, 'Above ' + D.load_ratio_flag + ' (big jump)', 'line'], [C.main, 'Steady range 0.8 to 1.3', 'soft'], ...overlayLegend(o)]);
      const now = ratioOn(D.today);
      return (now != null ? `Load ratio today: ${now.toFixed(2)}. ` : '') + `Above ${D.load_ratio_flag} on ${over.length} of ${have.length} days in ${rg.phrase}.`;
    }

    function drawDots(rg, key, opts) {
      const rs = runsIn(rg);
      const have = rs.filter((r) => r[key] != null);
      if (!have.length) { empty(opts.none); setLegend([]); return opts.noneSummary(rs); }
      const { svg, W, H } = frame(250);
      const vals = have.map((r) => r[key]);
      const y0 = d3.scaleLinear(opts.domain(vals), [0, 1]);
      if (!opts.fixed) y0.nice();
      const ticks = y0.ticks(5);
      const L = layout(svg, W, H, ticks, opts.fmt);
      const y = d3.scaleLinear(y0.domain(), [L.bottom, L.top]);
      if (opts.invert) y.range([L.top, L.bottom]);
      const x = d3.scaleUtc([rg.start, addDays(rg.end, 1)], [L.ml, W - L.mr]);
      yGrid(svg, y, ticks, opts.fmt, L.ml, W - L.mr);
      const points = [];
      const o = overlays(svg, x, rg, L.top, L.bottom, points);
      const cx = (r) => x(r.d) + (x(addDays(r.d, 1)) - x(r.d)) / 2;
      svg.append('g').selectAll('circle').data(have).join('circle').attr('cx', cx).attr('cy', (r) => y(r[key]))
        .attr('r', have.length > 150 ? 2.5 : 4.5).attr('fill', (r) => (r.workout ? C.alt : C.main)).attr('fill-opacity', 0.9);
      have.forEach((r) => points.push({ x: cx(r), y: y(r[key]), lines: [d3.utcFormat('%a, %b %-d')(r.d) + (r.workout ? ': ' + r.workout : ''), opts.tip(r), r.miles.toFixed(1) + ' miles'] }));
      xAxis(svg, x, rg, L.bottom);
      hover(svg, W, H, points);
      setLegend([[C.main, 'Easy runs'], [C.alt, 'Workouts, races and long runs'], ...overlayLegend(o)]);
      return opts.summary(have, rs);
    }

    function drawResults(rg) {
      const rs = D.results.filter((r) => r.d >= rg.start && r.d <= rg.end);
      if (!rs.length) { empty('No benchmark results in this range. Try “All years”.'); setLegend([]); return 'No benchmark results in ' + rg.phrase + '.'; }
      const { svg, W, H } = frame(260);
      const y0 = d3.scaleLinear([d3.max(rs, (r) => r.pace) + 8, d3.min(rs, (r) => r.pace) - 8], [0, 1]);
      const ticks = y0.ticks(5);
      const L = layout(svg, W, H, ticks, fmtPace);
      const y = d3.scaleLinear(y0.domain(), [L.bottom, L.top]);
      const x = d3.scaleUtc([rg.start, addDays(rg.end, 1)], [L.ml, W - L.mr]);
      yGrid(svg, y, ticks, fmtPace, L.ml, W - L.mr);
      const points = [];
      const o = overlays(svg, x, rg, L.top, L.bottom, points);
      const cx = (r) => x(r.d) + (x(addDays(r.d, 1)) - x(r.d)) / 2;
      const shown = EVENTS.filter((e) => rs.some((r) => r.event === e));
      shown.forEach((e) => {
        const er = rs.filter((r) => r.event === e).sort((a, b) => a.d - b.d);
        svg.append('path').datum(er).attr('d', d3.line().x(cx).y((r) => y(r.pace))).attr('fill', 'none').attr('stroke', EVENT_COLORS[e]).attr('stroke-width', 2);
        svg.append('g').selectAll('circle').data(er).join('circle').attr('cx', cx).attr('cy', (r) => y(r.pace)).attr('r', 4.5).attr('fill', EVENT_COLORS[e]);
        er.forEach((r) => points.push({ x: cx(r), y: y(r.pace), lines: [d3.utcFormat('%b %-d, %Y')(r.d), `${r.event}: ${fmtTime(r.time)} (${fmtPace(r.pace)} per mile)`, r.course || r.season] }));
      });
      xAxis(svg, x, rg, L.bottom);
      hover(svg, W, H, points);
      setLegend([...shown.map((e) => [EVENT_COLORS[e], e]), ...overlayLegend(o)]);
      return 'Race pace per mile, higher is faster. Bests: ' + shown.map((e) => `${e} ${fmtTime(d3.min(rs.filter((r) => r.event === e), (r) => r.time))}`).join(', ') + '.';
    }

    function draw() {
      if (!D) return;
      const rg = range();
      const type = typeSel.value;
      box.setAttribute('aria-label', `${NAMES[type]} chart, ${rg.label}`);
      let text;
      if (type === 'miles') text = drawMiles(rg);
      else if (type === 'load') text = drawLoad(rg);
      else if (type === 'effort') {
        text = drawDots(rg, 'rpe', {
          domain: () => [0, 10], fixed: true, fmt: String, tip: (r) => 'Effort ' + r.rpe + '/10' + (r.feel ? ', legs ' + r.feel.toLowerCase() : ''),
          none: 'No effort ratings in this range yet. They’re added from My runs after each run.',
          noneSummary: (rs) => `${rs.length} run${rs.length === 1 ? '' : 's'} in ${rg.phrase}, none with an effort rating yet.`,
          summary: (have, rs) => `Average effort ${d3.mean(have, (r) => r.rpe).toFixed(1)} out of 10 across ${have.length} run${have.length === 1 ? '' : 's'}` + (rs.length > have.length ? `; ${rs.length - have.length} without a rating.` : '.'),
        });
      } else if (type === 'hr') {
        text = drawDots(rg, 'avg_hr', {
          domain: (v) => [d3.min(v) - 5, d3.max(v) + 5], fmt: String, tip: (r) => 'Average heart rate ' + r.avg_hr + ' bpm',
          none: 'No heart rate recorded in this range.', noneSummary: () => 'No heart rate in ' + rg.phrase + '.',
          summary: (have) => `Average heart rate ${Math.round(d3.mean(have, (r) => r.avg_hr))} bpm across ${have.length} run${have.length === 1 ? '' : 's'}.`,
        });
      } else if (type === 'pace') {
        text = drawDots(rg, 'pace', {
          domain: (v) => [d3.max(v) + 15, d3.min(v) - 15], invert: false, fmt: fmtPace, tip: (r) => fmtPace(r.pace) + ' per mile',
          none: 'No runs in this range yet.', noneSummary: () => 'No runs in ' + rg.phrase + '.',
          summary: (have) => { const mi = d3.sum(have, (r) => r.miles), mins = d3.sum(have, (r) => r.pace * r.miles); return `Average pace ${fmtPace(mins / mi)} per mile across ${have.length} run${have.length === 1 ? '' : 's'}; higher on the chart is faster.`; },
        });
      } else text = drawResults(rg);
      summary.textContent = text;
    }
  }
})();

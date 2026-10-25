// Render a page in jsdom, wait for the data, exercise the controls, and report what it shows.
// Usage: node harness.mjs dashboard <url> <cookie>     (self-hosted app)
//        node harness.mjs hosted <body.html> <csvdir>   (the published prototype's page and files)
//        node harness.mjs charts <url> <cookie>
import { JSDOM, ResourceLoader, VirtualConsole } from 'jsdom';
import fs from 'node:fs';
import path from 'node:path';

const [mode, a, b] = process.argv.slice(2);
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...x) => errors.push('console.error: ' + x.join(' ')));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const d3src = fs.readFileSync(path.resolve(process.cwd(), '../xcapp/static/d3.v7.9.0.min.js'), 'utf8');
const shim = fs.readFileSync(path.resolve(process.cwd(), '../xcapp/static/dash-shim.js'), 'utf8');

function polyfill(window, fetchImpl) {
  window.SVGElement.prototype.getComputedTextLength = function () { return (this.textContent || '').length * 6.3; };
  window.fetch = fetchImpl;
  window.addEventListener('error', (e) => errors.push('window.error: ' + e.message));
}

class CookieLoader extends ResourceLoader {
  constructor(cookie) { super(); this.cookie = cookie; }
  fetch(url, options) { options.cookieJar = undefined; return super.fetch(url, { ...options, headers: { cookie: this.cookie } }); }
}

let dom;
if (mode === 'hosted') {
  const body = fs.readFileSync(a, 'utf8');
  const css = fs.readFileSync(path.resolve(process.cwd(), '../xcapp/static/app.css'), 'utf8');
  const html = `<!doctype html><html><head><style>${css}</style><script>window.DASH_PARAMS={"athlete":"A59","event":"5K","workout":"W4"};</script>
  <script>${d3src}</script><script>${shim}</script></head><body><div id="dash-root">${body}</div></body></html>`;
  dom = new JSDOM(html, { url: 'https://claude.ai/artifact/x', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { polyfill(w, async (u) => { const id = String(u).match(/datasets\/([^.]+)\.csv/)[1]; const t = fs.readFileSync(path.join(b, id + '.csv'), 'utf8'); return { ok: true, status: 200, text: async () => t }; }); } });
} else {
  const res = await fetch(a, { headers: { cookie: b } });
  const html = await res.text();
  dom = new JSDOM(html, { url: a, runScripts: 'dangerously', resources: new CookieLoader(b), pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { polyfill(w, (u, o) => fetch(new URL(String(u), a), { ...(o || {}), headers: { cookie: b } })); } });
}
const w = dom.window, doc = w.document;
const txt = (sel) => [...doc.querySelectorAll(sel)].map((e) => e.textContent.trim());
const out = { mode, errors };

async function waitFor(fn, ms = 20000) { const t0 = Date.now(); while (Date.now() - t0 < ms) { try { if (fn()) return true; } catch (e) { /* not yet */ } await sleep(100); } return false; }
function badValues() {
  const bad = [];
  if (/\bNaN\b|undefined|\[object Object\]/.test(doc.body.textContent)) bad.push('text: ' + (doc.body.textContent.match(/.{0,40}(NaN|undefined|\[object Object\]).{0,40}/) || [''])[0]);
  for (const el of doc.querySelectorAll('svg *')) for (const at of el.attributes) if (/NaN|undefined/.test(at.value)) { bad.push(`${el.tagName} ${at.name}=${at.value}`); break; }
  return bad.slice(0, 5);
}

if (mode === 'athlete') {
  out.ready = await waitFor(() => { const r = doc.querySelector('[data-athlete-charts]'); return r && r.dataset.ready; });
  const root = doc.querySelector('[data-athlete-charts]');
  const t = root.querySelector('.ac-type'), r = root.querySelector('.ac-range');
  out.data_state = root.dataset.ready;
  out.combos = [];
  for (const type of [...t.options].map((o) => o.value)) for (const range of [...r.options].map((o) => o.value)) {
    t.value = type; t.dispatchEvent(new w.Event('change'));
    r.value = range; r.dispatchEvent(new w.Event('change'));
    const chart = root.querySelector('.ac-chart');
    out.combos.push({ type, range, svg: !!chart.querySelector('svg'), marks: chart.querySelectorAll('rect,circle,path').length,
      empty: (chart.querySelector('.ac-empty') || {}).textContent || '', summary: root.querySelector('.ac-summary').textContent,
      legend: [...root.querySelectorAll('.ac-legend span')].map((s) => s.textContent), label: chart.getAttribute('aria-label') });
  }
  out.bad_values = badValues();
} else if (mode === 'charts') {
  out.ready = await waitFor(() => w.__chartsReady);
  out.career_panels = doc.querySelectorAll('#c-chart .xcc-panel').length;
  out.career_svgs = doc.querySelectorAll('#c-chart svg').length;
  out.compare_lines = doc.querySelectorAll('#m-chart path').length;
  out.compare_picks = doc.querySelectorAll('#m-picks input').length;
  out.improve_bars = doc.querySelectorAll('#i-chart rect').length;
  out.top7_points = doc.querySelectorAll('#t-chart circle').length;
  const g = doc.getElementById('i-gender'); g.value = 'Girls'; g.dispatchEvent(new w.Event('change'));
  out.improve_bars_girls = doc.querySelectorAll('#i-chart rect').length;
  const picks = [...doc.querySelectorAll('#m-picks input')];
  picks.slice(3, 7).forEach((cb) => { if (!cb.disabled) { cb.checked = true; cb.dispatchEvent(new w.Event('change')); } });
  out.compare_after_more = doc.querySelectorAll('#m-legend span > span').length;
  out.compare_disabled_at_6 = doc.querySelectorAll('#m-picks input:disabled').length > 0;
  const s = doc.getElementById('c-athlete'); s.selectedIndex = 5; s.dispatchEvent(new w.Event('change'));
  out.career_after_switch = doc.querySelectorAll('#c-chart svg').length;
  out.bad_values = badValues();
} else {
  out.ready = await waitFor(() => doc.querySelector('.xc-take-n') && !doc.querySelector('.xc-take-n').classList.contains('dash-skeleton')
    && doc.querySelectorAll('.xc-ins').length > 0 && doc.querySelectorAll('.xc-watch tbody tr').length > 0);
  out.sub = doc.querySelector('.xc-sub').textContent.replace(/\s+/g, ' ').trim();
  out.take = doc.querySelector('.xc-take').textContent.trim();
  out.kpis = Object.fromEntries([...doc.querySelectorAll('.xc-kpi [data-field]')].map((e) => [e.dataset.field, e.textContent.trim()]));
  out.insights = doc.querySelectorAll('.xc-ins').length;
  out.first_insight_scope = (doc.querySelector('.xc-ins .xc-scope') || {}).textContent;
  out.watch = [...doc.querySelectorAll('.xc-watch tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent));
  out.ai_note = doc.querySelector('.xc-ai-note').textContent.slice(0, 60);
  const tab = async (id) => { doc.getElementById('tab-' + id).click(); await sleep(400); };
  await tab('performance');
  out.bench_panels = doc.querySelectorAll('.xc-bench .xc-bp').length;
  out.bench_points = doc.querySelectorAll('.xc-bench circle').length;
  out.season_headers = txt('.xc-progress thead th button');
  out.progress_rows = doc.querySelectorAll('.xc-progress tbody tr').length;
  out.progress_first = [...doc.querySelector('.xc-progress tbody tr').children].map((td) => td.textContent);
  doc.querySelector('.xc-evseg button[data-event="1 Mile"]').click(); await sleep(300);
  out.ev_label = doc.querySelector('.xc-ev-label').textContent;
  const search = doc.querySelector('.xc-search'); search.value = 'rowan'; search.dispatchEvent(new w.Event('input')); await sleep(200);
  out.search_rows = txt('.xc-progress tbody tr td:first-child');
  search.value = ''; search.dispatchEvent(new w.Event('input'));
  await tab('heart-rate');
  out.workout_buttons = txt('.xc-wseg button');
  out.rep_svg_circles = doc.querySelectorAll('.xc-reps-plot circle').length;
  out.rep_legend = txt('.xc-reps-legend > span');
  out.wsum_first = [...doc.querySelector('.xc-wsum tbody tr').children].map((td) => td.textContent);
  out.wsum_rows = doc.querySelectorAll('.xc-wsum tbody tr').length;
  await tab('injuries');
  out.load_bars = doc.querySelectorAll('.xc-load rect').length;
  out.bands = txt('.xc-bands .xc-band-top').map((s) => s.replace(/\s+/g, ' '));
  out.team_svg = doc.querySelectorAll('.xc-team svg').length;
  out.injury_rows = doc.querySelectorAll('.xc-injlog tbody tr').length;
  doc.querySelector('.xc-injlog tbody tr').click(); await sleep(300);
  out.after_row_click_athlete = doc.querySelector('#athlete-load .xc-aname').textContent;
  doc.querySelector('.xc-injlog thead th[data-field="days_missed"] button').click(); await sleep(200);
  out.sorted_days = txt('.xc-injlog tbody tr td:nth-child(5)').slice(0, 3);
  out.bad_values = badValues();
}
out.errors = errors;
console.log(JSON.stringify(out));
process.exit(0);

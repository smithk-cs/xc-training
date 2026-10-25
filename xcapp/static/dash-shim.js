/* Minimal dashboard runtime for the self-hosted app.
   Implements the parts of the `dash` API that dashboard_body.html uses: data, onData, calc, params,
   setParams, resetParams, setLink, colors. Datasets load as CSV from /api/datasets/<id>.csv. */
(function () {
  'use strict';
  const defaults = Object.assign({}, window.DASH_PARAMS || {});
  let params = Object.assign({}, defaults);
  const sources = {};
  const calcs = [];
  const calcOut = {};
  const listeners = [];
  let pending = false;

  const isCalc = (id) => calcs.some((c) => c.id === id);

  function emit() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      for (const fn of listeners) {
        try { fn(); } catch (e) { console.error(e); window.__dashErrors = (window.__dashErrors || []).concat(String(e && e.stack || e)); }
      }
    });
  }

  function load(id) {
    if (sources[id]) return;
    sources[id] = { status: 'loading', data: [], columns: [] };
    fetch('/api/datasets/' + encodeURIComponent(id) + '.csv', { credentials: 'same-origin' })
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then((text) => {
        const rows = d3.csvParse(text);
        sources[id] = { status: 'ok', data: rows, columns: rows.columns || [] };
        recompute();
        emit();
      })
      .catch((e) => {
        sources[id] = { status: 'error', data: [], columns: [], message: String(e) };
        recompute();
        emit();
      });
  }

  function recompute() {
    for (const c of calcs) {
      const ins = c.inputs.map((i) => (isCalc(i) ? (calcOut[i] || { status: 'loading', data: [] }) : (sources[i] || { status: 'loading', data: [] })));
      if (ins.some((x) => x.status === 'error')) { calcOut[c.id] = { status: 'error', data: [] }; continue; }
      if (!ins.every((x) => x.status === 'ok')) { calcOut[c.id] = { status: 'loading', data: [] }; continue; }
      try {
        calcOut[c.id] = { status: 'ok', data: c.fn(...ins.map((x) => x.data)) };
      } catch (e) {
        console.error('calc ' + c.id, e);
        window.__dashErrors = (window.__dashErrors || []).concat('calc ' + c.id + ': ' + String(e && e.stack || e));
        calcOut[c.id] = { status: 'error', data: [], message: String(e) };
      }
    }
  }

  function data(id) {
    if (isCalc(id)) return Object.assign({ refreshing: false, meta: [] }, calcOut[id] || { status: 'loading', data: [] });
    load(id);
    return Object.assign({ refreshing: false, meta: [] }, sources[id]);
  }

  const colors = ['--dash-c1', '--dash-c2', '--dash-c3', '--dash-c4', '--dash-c5', '--dash-c6', '--dash-c7', '--dash-c8']
    .map((v) => 'var(' + v + ')');

  window.dash = {
    data,
    onData(fn) { listeners.push(fn); try { fn(); } catch (e) { console.error(e); window.__dashErrors = (window.__dashErrors || []).concat(String(e && e.stack || e)); } },
    calc(id, def) {
      calcs.push({ id, inputs: def.inputs || [], fn: def.fn });
      (def.inputs || []).forEach((i) => { if (!isCalc(i)) load(i); });
      recompute();
    },
    loader() {},
    params() { return Object.assign({}, params); },
    setParams(p) {
      for (const k of Object.keys(p || {})) { if (p[k] == null) delete params[k]; else params[k] = p[k]; }
      emit();
    },
    resetParams() { params = Object.assign({}, defaults); emit(); },
    setLink(name) { try { history.replaceState(null, '', '#' + name); } catch (e) { /* ignore */ } },
    refresh() {},
    colors,
  };
  let t = null;
  window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(emit, 120); });
})();

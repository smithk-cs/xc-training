// Tables with class "sortable": click (or Enter/Space on) a header to sort by that column; again to reverse.
// A cell's data-sort value is used when present; numbers sort as numbers; empty cells always go last.
(function () {
  function key(row, i) {
    var c = row.cells[i];
    if (!c) return '';
    var v = c.hasAttribute('data-sort') ? c.getAttribute('data-sort') : c.textContent;
    return v.trim();
  }
  function compare(a, b, dir) {
    if (a === '' && b === '') return 0;
    if (a === '') return 1;
    if (b === '') return -1;
    var na = Number(a), nb = Number(b);
    var r = (!isNaN(na) && !isNaN(nb)) ? na - nb : a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true });
    return r * dir;
  }
  document.querySelectorAll('table.sortable').forEach(function (table) {
    var heads = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(heads, function (th, i) {
      if (th.hasAttribute('data-nosort')) return;
      th.tabIndex = 0;
      th.classList.add('sort-head');
      function go() {
        var dir = th.getAttribute('aria-sort') === 'ascending' ? -1 : 1;
        Array.prototype.forEach.call(heads, function (h) { h.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
        var body = table.tBodies[0];
        var rows = Array.prototype.filter.call(body.rows, function (r) { return r.cells.length === heads.length; });
        rows.sort(function (a, b) { return compare(key(a, i), key(b, i), dir); });
        rows.forEach(function (r) { body.appendChild(r); });
      }
      th.addEventListener('click', go);
      th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
  });
})();

/* Time boxes (<input data-time>): phone number pads have no colon, so the colons go in by themselves as digits are
   typed, filling from the right like a stopwatch: 512 -> 5:12, 1745 -> 17:45, 10203 -> 1:02:03. A decimal point keeps
   tenths (5124.3 -> 51:24.3; 512.4 -> 5:12.4). Typing the colons yourself gives the same result. */
(function () {
  "use strict";
  function format(raw) {
    var s = String(raw || "");
    var dot = s.indexOf(".");
    var whole = (dot >= 0 ? s.slice(0, dot) : s).replace(/\D/g, "").replace(/^0+(?=\d)/, "");
    var frac = dot >= 0 ? "." + s.slice(dot + 1).replace(/\D/g, "").slice(0, 2) : "";
    if (whole.length > 6) whole = whole.slice(0, 6);
    var out = whole;
    if (whole.length > 4) out = whole.slice(0, -4) + ":" + whole.slice(-4, -2) + ":" + whole.slice(-2);
    else if (whole.length > 2) out = whole.slice(0, -2) + ":" + whole.slice(-2);
    return out + frac;
  }
  function attach(el) {
    if (el.dataset.timeReady) return;
    el.dataset.timeReady = "1";
    el.addEventListener("input", function () {
      var v = format(el.value);
      if (v !== el.value) {
        el.value = v;
        try { el.setSelectionRange(v.length, v.length); } catch (e) { /* some input types don't allow it */ }
      }
    });
  }
  window.formatTime = format;
  function start() { document.querySelectorAll("input[data-time]").forEach(attach); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();

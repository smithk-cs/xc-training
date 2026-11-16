/* Courses page: trace a course's route on the satellite photo of its area. Tap along the route in order; the line,
   the distance and the start and finish are drawn over the photo; Save sends the points (latitude, longitude) to
   become the flyover for a distance. The photo is linear in Web Mercator between its edges (cfg.box: north, west,
   south, east). A half-traced route is kept in this browser until it's saved or cleared. */
(function () {
  "use strict";
  var cfg = JSON.parse(document.getElementById("tr-cfg").textContent);
  var img = document.getElementById("tr-img"), svg = document.getElementById("tr-svg"), wrap = document.getElementById("tr-wrap");
  var $ = function (id) { return document.getElementById(id); };
  var N = cfg.box[0], W = cfg.box[1], S = cfg.box[2], E = cfg.box[3];
  var mercY = function (lat) { return Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)); };
  var yN = mercY(N), yS = mercY(S);
  var key = "trace:" + cfg.save, pts = [], centring = false;
  try { pts = JSON.parse(localStorage.getItem(key) || "[]"); } catch (e) { pts = []; }

  function toLatLon(fx, fy) {
    var lon = W + fx * (E - W);
    var y = yN - fy * (yN - yS);
    return [Math.atan(Math.sinh(y)) * 180 / Math.PI, lon];
  }
  function hav(a, b) {
    var r = 6371000, p1 = a[0] * Math.PI / 180, p2 = b[0] * Math.PI / 180, dp = p2 - p1, dl = (b[1] - a[1]) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function save() { try { localStorage.setItem(key, JSON.stringify(pts)); } catch (e) { /* not kept: fine */ } }

  function draw() {
    var w = img.naturalWidth || 1000, h = img.naturalHeight || 1000, sw = w / 260;
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    var xy = pts.map(function (p) { return [p[0] * w, p[1] * h]; });
    var html = "";
    if (xy.length > 1) {
      var line = xy.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
      html += '<polyline points="' + line + '" fill="none" stroke="#000" stroke-opacity=".5" stroke-width="' + (sw * 2.2) + '" stroke-linejoin="round" stroke-linecap="round"/>';
      html += '<polyline points="' + line + '" fill="none" stroke="#ffd400" stroke-width="' + sw + '" stroke-linejoin="round" stroke-linecap="round"/>';
    }
    xy.forEach(function (p, i) {
      var first = i === 0, last = i === xy.length - 1 && xy.length > 1;
      html += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="' + (first || last ? sw * 2.6 : sw * 1.1) +
        '" fill="' + (first ? "#1e9e3e" : last ? "#d0191b" : "#ffd400") + '" stroke="#000" stroke-width="' + (sw * 0.4) + '"/>';
    });
    svg.innerHTML = html;
    var m = 0;
    for (var i = 1; i < pts.length; i++) m += hav(toLatLon(pts[i - 1][0], pts[i - 1][1]), toLatLon(pts[i][0], pts[i][1]));
    $("tr-dist").textContent = (m / 1609.344).toFixed(2) + " mi (" + (m / 1000).toFixed(2) + " km), " + pts.length + " point" + (pts.length === 1 ? "" : "s");
    $("tr-save").disabled = pts.length < 2;
  }

  svg.addEventListener("click", function (e) {
    var r = svg.getBoundingClientRect();
    var fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return;
    if (centring) {   // the next photo is centred on this spot
      var ll = toLatLon(fx, fy);
      var form = document.querySelector('form[action$="/area"]');
      form.querySelector('[name="lat"]').value = ll[0].toFixed(5);
      form.querySelector('[name="lon"]').value = ll[1].toFixed(5);
      centring = false;
      wrap.classList.remove("centring");
      $("tr-status").textContent = "New centre set. Press Get the photo again to see the area around it.";
      return;
    }
    pts.push([fx, fy]);
    save();
    draw();
  });
  $("tr-undo").addEventListener("click", function () { pts.pop(); save(); draw(); });
  $("tr-clear").addEventListener("click", function () {
    if (pts.length && !confirm("Clear the traced route?")) return;
    pts = []; save(); draw();
  });
  $("tr-close").addEventListener("click", function () {
    if (pts.length > 1) { pts.push(pts[0].slice()); save(); draw(); }
  });
  $("tr-centre").addEventListener("click", function () {
    centring = !centring;
    wrap.classList.toggle("centring", centring);
    $("tr-status").textContent = centring ? "Tap the photo where the centre should be." : "";
  });
  $("tr-save").addEventListener("click", function () {
    var btn = $("tr-save");
    btn.disabled = true;
    $("tr-status").textContent = "Saving… (getting USGS heights and the satellite photo, a few seconds)";
    fetch(cfg.save, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf: cfg.csrf, distance: $("tr-distance").value,
        points: pts.map(function (p) { return toLatLon(p[0], p[1]); }) })
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) throw new Error(x.d.error || "Couldn’t save the route.");
        try { localStorage.removeItem(key); } catch (e) { /* fine */ }
        location.href = x.d.next;
      })
      .catch(function (err) { $("tr-status").textContent = err.message; btn.disabled = false; });
  });
  if (img.complete) draw(); else img.addEventListener("load", draw);
})();

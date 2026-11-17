/* The athlete page's Phone notifications panel: turns notifications on or off for this phone (push_web.py). */
(function () {
  function ready(fn) { if (document.readyState !== "loading") fn(); else document.addEventListener("DOMContentLoaded", fn); }
  ready(function () {
    var panel = document.getElementById("push-panel");
    if (!panel) return;
    var token = panel.dataset.token, csrf = panel.dataset.csrf, key = panel.dataset.key;
    function el(name) { return panel.querySelector('[data-push="' + name + '"]'); }
    function show(names) {
      ["ios", "unsupported", "blocked", "on", "test", "off"].forEach(function (n) { el(n).hidden = names.indexOf(n) < 0; });
    }
    function say(text) { el("msg").textContent = text || ""; }
    function post(path, fields) {
      var body = new FormData();
      body.append("csrf", csrf);
      Object.keys(fields || {}).forEach(function (k) { body.append(k, fields[k]); });
      return fetch("/a/" + token + path, { method: "POST", body: body, credentials: "same-origin" }).then(function (r) {
        return r.json().catch(function () { return { ok: false, error: "Something went wrong (" + r.status + "). Reload and try again." }; });
      });
    }
    function keyBytes(b64) {
      var s = (b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
      var raw = atob(s), out = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
      return out;
    }
    function device() {
      var ua = navigator.userAgent;
      return /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Browser";
    }
    var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
    var ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      show(ios && !standalone ? ["ios"] : ["unsupported"]);
      return;
    }
    var reg;
    function refresh() {
      return reg.pushManager.getSubscription().then(function (sub) {
        if (Notification.permission === "denied") show(["blocked"]);
        else show(sub ? ["test", "off"] : ["on"]);
        return sub;
      });
    }
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (r) { reg = r; return refresh(); })
      .catch(function () { show(["unsupported"]); });

    el("on").addEventListener("click", function () {
      say("Turning on…");
      Notification.requestPermission().then(function (p) {
        if (p !== "granted") { refresh(); say(p === "denied" ? "" : "Notifications weren’t allowed."); return null; }
        return navigator.serviceWorker.ready.then(function (r) {
          return r.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
        }).then(function (sub) {
          var j = sub.toJSON();
          return post("/push", { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth, device: device() });
        }).then(function (res) {
          if (res && res.ok) { say("Notifications are on for this phone."); }
          else { say((res && res.error) || "That didn’t work. Reload and try again."); }
          return refresh();
        });
      }).catch(function (e) { say("That didn’t work: " + (e && e.message ? e.message : e)); refresh(); });
    });
    el("test").addEventListener("click", function () {
      say("Sending…");
      post("/push/test").then(function (res) {
        say(res.ok ? "Sent. It should arrive in a few seconds." : "It couldn’t be sent. Turn notifications off and on again.");
      });
    });
    el("off").addEventListener("click", function () {
      reg.pushManager.getSubscription().then(function (sub) {
        var endpoint = sub ? sub.endpoint : "";
        return (sub ? sub.unsubscribe() : Promise.resolve()).then(function () { return post("/push/off", { endpoint: endpoint }); });
      }).then(function () { say("Notifications are off for this phone."); refresh(); });
    });
  });
})();

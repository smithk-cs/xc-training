/* Service worker for phone notifications (push.py): shows each notification the app sends, and opens the athlete's
   page when it's tapped. Nothing is cached: the app always loads fresh from the site. */
self.addEventListener("push", function (event) {
  var d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(d.title || "Cross country", {
    body: d.body || "", icon: "/static/icon-192.png", badge: "/static/icon-192.png", data: { url: d.url || "/" }
  }));
});
self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) { if (list[i].url === url && "focus" in list[i]) return list[i].focus(); }
    return self.clients.openWindow(url);
  }));
});

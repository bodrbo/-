// Deliberately does no caching — this app's data (payroll, checklists,
// orders) must always be current, and a stale cache would be worse than no
// offline support at all. The only reason this file exists is that Chrome
// requires an active service worker with a fetch handler before it will
// treat the site as installable ("Add to Home Screen" / PWA).

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", function (event) {
  event.respondWith(fetch(event.request));
});

// Push notifications — server sends {title, body, url} as JSON (see
// send_push_notification in app.py); this just has to display it and, on
// tap, focus an already-open tab on that URL or open a new one.
self.addEventListener("push", function (event) {
  var payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = {body: event.data ? event.data.text() : ""};
  }
  var title = payload.title || "Бодрый Боцман";
  var options = {
    body: payload.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: {url: payload.url || "/"},
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(url) !== -1 && "focus" in list[i]) return list[i].focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

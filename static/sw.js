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

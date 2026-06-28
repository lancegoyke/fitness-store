{% load static %}/* Meso athlete PWA service worker (athlete slice Phase 4b — decision S7).
 *
 * Served from /meso/sw.js (a Django view, not a hashed static file) so its scope
 * is /meso/ and it can control /meso/me/. Strategy:
 *   - install:  precache the static shell (css/js/icons) + the offline page.
 *   - activate: drop caches from older versions, take control immediately.
 *   - fetch:
 *       * navigations (HTML): network-first, falling back to the last-good
 *         cached page for that URL, then to the offline page. This is what lets
 *         the athlete re-open a session they viewed online and keep logging when
 *         the gym wifi drops.
 *       * same-origin static GETs: stale-while-revalidate from the cache.
 *       * POSTs (logging): never intercepted — the page's own offline queue owns
 *         writes (more reliable on iOS than the Background Sync API).
 *   - push / notificationclick: render + route delivery notifications (S3).
 */

const CACHE = "{{ cache_version }}";
const OFFLINE_URL = "{{ offline_url }}";
const HOME_URL = "{{ home_url }}";

// Static shell — safe to precache (no auth, hashed URLs resolved at render time).
const PRECACHE = [
  OFFLINE_URL,
  "{% static 'css/meso.css' %}",
  "{% static 'js/meso_athlete.js' %}",
  "{% static 'js/meso_push.js' %}",
  "{% static 'js/alpine.min.js' %}",
  "{% static 'png/meso-icon-192.png' %}",
  "{% static 'png/meso-icon-512.png' %}",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
      )
      .then(() => self.clients.claim()),
  );
});

function isNavigation(request) {
  return (
    request.mode === "navigate" ||
    (request.method === "GET" &&
      (request.headers.get("accept") || "").includes("text/html"))
  );
}

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Writes (logging) are owned by the page's offline queue — pass through.
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // leave cross-origin alone

  if (isNavigation(request)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Cache a copy of the rendered page so it re-opens offline next time —
          // but only a genuine 200. After the session expires the fetch follows
          // the login redirect; caching that would overwrite the last-good
          // athlete page with a login screen, breaking offline reopen.
          if (response.ok && !response.redirected) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() =>
          caches
            .match(request)
            .then((cached) => cached || caches.match(OFFLINE_URL)),
        ),
    );
    return;
  }

  // Static GETs: stale-while-revalidate.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});

/* -- Web push (delivery notifications, S3) -------------------------------- */

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {};
  }
  const title = data.title || "Meso";
  const options = {
    body: data.body || "Your training was updated.",
    icon: "{% static 'png/meso-icon-192.png' %}",
    badge: "{% static 'png/meso-icon-192.png' %}",
    tag: data.tag || "meso",
    data: { url: data.url || HOME_URL },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || HOME_URL;
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if (client.url.includes(target) && "focus" in client) return client.focus();
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
      }),
  );
});

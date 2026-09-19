/* Meso — the client beacon for browser-only product events (#509 slice 3).
 *
 * A handful of moments only exist in the browser and never touch a Django
 * view on their own: whether a push permission prompt was granted or denied,
 * whether the PWA got installed. `window.mesoTrack(name, props)` is the one
 * way anything on the page reports one of those moments, POSTing to the
 * beacon endpoint (`meso:track_beacon`, analytics/views.py::track_beacon).
 * The accepted names/props are a closed set enforced server-side
 * (analytics/beacon.py) — this file doesn't validate anything, it only ships
 * the pair; an unknown name or prop is simply a beacon the server drops.
 *
 * Loaded first (before meso_push.js / meso_onboarding.js) so both can call
 * `window.mesoTrack` unconditionally once it exists, the same way they guard
 * any optional global. Config (the URL, the CSRF token) lives on the same
 * `#meso-pwa-config` span meso_push.js reads, so there's one source of truth
 * for "what page am I on and what can it talk to".
 *
 * Unlike meso_push.js — which captures its config once when the script
 * executes, because it's only ever driven by a same-page gesture — this file
 * re-reads the span on every call. A beacon call can happen well after load
 * (a push prompt answered seconds later, an `appinstalled` event on return
 * from the home-screen icon), the span's attributes never change after
 * render, and reading it lazily is what makes `mesoTrack` a plain function
 * callers can invoke without ever checking whether tracking is configured.
 */
(function (root) {
  // The beacon's config, or null when tracking isn't available on this page
  // (no #meso-pwa-config span — a non-Meso page — or no data-track-url).
  function beaconConfig(doc) {
    var cfg =
      doc && doc.getElementById && doc.getElementById("meso-pwa-config");
    if (!cfg) return null;
    var url = cfg.dataset.trackUrl;
    if (!url) return null;
    return { url: url, csrf: cfg.dataset.csrf || "" };
  }

  // POST one event and forget it. Never throws and never rejects: a beacon is
  // a courtesy to analytics, not something a caller (a push permission flow,
  // an install listener) should have to guard against or let block on. Callers
  // that want to know it's finished (tests, mainly) can still `await` it,
  // since a swallowed failure still resolves.
  function mesoTrack(name, props) {
    var cfg = beaconConfig(root.document);
    if (!cfg) return Promise.resolve();
    return fetch(cfg.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": cfg.csrf,
      },
      body: JSON.stringify({ name: name, props: props || {} }),
    }).catch(function () {
      /* best-effort — nothing on the page reads a beacon's answer */
    });
  }
  root.mesoTrack = mesoTrack;

  // Test hook for Node-based runners (vitest); skipped in the browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { mesoTrack: mesoTrack };
  }
})(typeof window !== "undefined" ? window : this);

/* Meso — athlete web-push subscription (athlete slice Phase 4b, decision S3/S7).
 *
 * Pairs with the service worker's `push` handler: requests notification
 * permission (on a user gesture via mesoEnablePush()), subscribes through the
 * PushManager with the server's VAPID applicationServerKey, and POSTs the
 * resulting subscription to /meso/api/me/push/subscribe/. Config (URLs, CSRF,
 * whether push is enabled) is read from the #meso-pwa-config span; the VAPID key
 * from the meso-vapid-key meta. When push is disabled or unsupported, every
 * entry point is an inert no-op so the page still installs + logs offline.
 */
(function () {
  const cfg = document.getElementById("meso-pwa-config");
  if (!cfg) return;

  const pushEnabled = cfg.dataset.pushEnabled === "1";
  const subscribeUrl = cfg.dataset.subscribeUrl;
  const csrf = cfg.dataset.csrf || "";
  const keyMeta = document.querySelector('meta[name="meso-vapid-key"]');
  const vapidKey = keyMeta ? keyMeta.content : "";

  function supported() {
    return (
      pushEnabled &&
      !!vapidKey &&
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window
    );
  }

  // base64url VAPID key → the Uint8Array applicationServerKey expects.
  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) output[i] = raw.charCodeAt(i);
    return output;
  }

  async function subscribe() {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      });
    }
    await fetch(subscribeUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(sub.toJSON()),
    });
    return sub;
  }

  function refreshCta() {
    const cta = document.getElementById("meso-push-cta");
    if (!cta) return;
    // Only offer the prompt when push is usable and the user hasn't decided yet.
    cta.hidden = !(supported() && Notification.permission === "default");
  }

  // The gesture-driven entry point (wired to the "Enable notifications" button).
  async function enable() {
    if (!supported()) return;
    let permission;
    try {
      permission = await Notification.requestPermission();
    } catch (e) {
      console.error("Meso push permission failed", e);
      return;
    }
    if (permission === "granted") {
      try {
        await subscribe();
      } catch (e) {
        console.error("Meso push subscribe failed", e);
      }
    }
    refreshCta();
  }
  window.mesoEnablePush = enable;

  if (supported()) {
    // An existing grant: keep the device's subscription current on each load.
    if (Notification.permission === "granted") {
      subscribe().catch((e) => console.error("Meso push refresh failed", e));
    }
    const cta = document.getElementById("meso-push-cta");
    if (cta) cta.addEventListener("click", enable);
    refreshCta();
  }
})();

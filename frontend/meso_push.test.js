// Tests for the athlete push permission + subscribe flow
// (app/store_project/static/js/meso_push.js).
//
// Focus: the permission-report gate added in #509 slice 3 — enable() must
// report the permission answer through window.mesoTrack exactly once, and
// only when the athlete just answered a prompt that appeared (i.e.
// Notification.permission was "default" *before* requestPermission() was
// called). A page load with an already-decided permission, or a repeat call
// after the athlete decided, must report nothing.
//
// meso_push.js's IIFE reads its config (and bails out entirely with no
// #meso-pwa-config span) once, when the script executes — unlike
// meso_track.js, which reads lazily per call and so can be statically
// imported the way meso_onboarding.test.js imports its subject. Here the DOM
// and window stubs have to exist *before* the module runs, so each test sets
// them up first, then `vi.resetModules()` + a fresh dynamic `import()` runs
// the IIFE against that state (the technique this file's own author notes
// call for).

const MODULE_PATH = "../app/store_project/static/js/meso_push.js";

// A push config that makes supported() true: push enabled, a valid-shaped
// (decodable) base64url VAPID key — enable()'s "granted" path runs it
// through urlBase64ToUint8Array/atob for real — and
// serviceWorker/PushManager/Notification all present.
const FAKE_VAPID_KEY =
  "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ";

function setConfig() {
  document.body.innerHTML =
    '<span id="meso-pwa-config" hidden ' +
    'data-subscribe-url="/meso/api/me/push/subscribe/" ' +
    'data-csrf="tok" ' +
    'data-push-enabled="1"></span>' +
    `<meta name="meso-vapid-key" content="${FAKE_VAPID_KEY}">`;
}

// supported() only checks `'serviceWorker' in navigator` / `'PushManager' in
// window` — existence, not behaviour — but enable() does drive a real
// subscribe() through them on a "granted" answer, so the stubs are wired
// enough for that to resolve quietly rather than dangle/throw.
function stubPushPlumbing() {
  Object.defineProperty(window.navigator, "serviceWorker", {
    configurable: true,
    value: {
      ready: Promise.resolve({
        pushManager: {
          getSubscription: async () => null,
          subscribe: async () => ({ toJSON: () => ({}) }),
        },
      }),
    },
  });
  window.PushManager = function () {};
  global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 204 });
}

function stubNotification(initialPermission, resolvedPermission) {
  window.Notification = {
    permission: initialPermission,
    requestPermission: vi.fn().mockImplementation(async () => {
      window.Notification.permission = resolvedPermission;
      return resolvedPermission;
    }),
  };
}

async function loadEnable() {
  await import(MODULE_PATH);
  return window.mesoEnablePush;
}

beforeEach(() => {
  vi.resetModules();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  delete window.Notification;
  delete window.PushManager;
  delete window.mesoTrack;
  delete window.mesoEnablePush;
});

describe("enable(): permission reporting (#509 slice 3)", () => {
  it("reports the result exactly once when the prompt started at 'default'", async () => {
    setConfig();
    stubPushPlumbing();
    stubNotification("default", "denied");
    window.mesoTrack = vi.fn().mockResolvedValue();
    const enable = await loadEnable();

    await enable();

    expect(window.mesoTrack).toHaveBeenCalledTimes(1);
    expect(window.mesoTrack).toHaveBeenCalledWith("push_permission", {
      result: "denied",
    });
  });

  it("reports the granted result too, when the prompt started at 'default'", async () => {
    setConfig();
    stubPushPlumbing();
    stubNotification("default", "granted");
    window.mesoTrack = vi.fn().mockResolvedValue();
    const enable = await loadEnable();

    await enable();

    expect(window.mesoTrack).toHaveBeenCalledTimes(1);
    expect(window.mesoTrack).toHaveBeenCalledWith("push_permission", {
      result: "granted",
    });
  });

  it("reports nothing when permission was already granted before the call", async () => {
    setConfig();
    stubPushPlumbing();
    stubNotification("granted", "granted");
    window.mesoTrack = vi.fn().mockResolvedValue();
    const enable = await loadEnable();

    await enable();

    expect(window.mesoTrack).not.toHaveBeenCalled();
  });

  it("reports nothing when permission was already denied before the call", async () => {
    setConfig();
    stubPushPlumbing();
    stubNotification("denied", "denied");
    window.mesoTrack = vi.fn().mockResolvedValue();
    const enable = await loadEnable();

    await enable();

    expect(window.mesoTrack).not.toHaveBeenCalled();
  });

  it("does not report a second time on a repeat enable() after the athlete decided", async () => {
    setConfig();
    stubPushPlumbing();
    stubNotification("default", "granted");
    window.mesoTrack = vi.fn().mockResolvedValue();
    const enable = await loadEnable();

    await enable(); // default -> granted: reports once
    await enable(); // permission is now "granted" already: no report

    expect(window.mesoTrack).toHaveBeenCalledTimes(1);
  });

  it("is a no-op, not a throw, when window.mesoTrack isn't defined", async () => {
    setConfig();
    stubPushPlumbing();
    stubNotification("default", "denied");
    const enable = await loadEnable();

    await enable(); // must not throw even though window.mesoTrack is undefined
  });
});

// Tests for the athlete first-run onboarding chrome
// (app/store_project/static/js/meso_onboarding.js).
//
// The DOM wiring (revealing the install card, firing the deferred install
// prompt, persisting coachmark dismissals) is verified at the render level in
// the Django tests; what's unit-tested here is the pure logic that decides
// *whether* and *how* to show the install card across the browser matrix
// (already-installed / dismissed / Android-promptable / iOS-manual), plus the
// defensive localStorage read.

import {
  installPromptState,
  isDismissed,
  detectIOS,
  shouldTrackInstall,
} from "../app/store_project/static/js/meso_onboarding.js";

const IPHONE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15";
// iPadOS 13+ Safari reports a desktop "Macintosh" UA but is touch-capable.
const IPADOS_DESKTOP_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15";
const MAC_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome";
const ANDROID_UA =
  "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile";

describe("installPromptState", () => {
  it("hides when the app is already running standalone (installed)", () => {
    expect(
      installPromptState({ standalone: true, canPrompt: true, isIOS: true }),
    ).toEqual({ show: false, mode: null });
  });

  it("hides when the athlete previously dismissed it", () => {
    expect(
      installPromptState({ dismissed: true, canPrompt: true, isIOS: false }),
    ).toEqual({ show: false, mode: null });
  });

  it("offers the native prompt when the browser captured one", () => {
    expect(
      installPromptState({ canPrompt: true, isIOS: false }),
    ).toEqual({ show: true, mode: "prompt" });
  });

  it("falls back to manual iOS instructions when there's no native prompt", () => {
    expect(
      installPromptState({ canPrompt: false, isIOS: true }),
    ).toEqual({ show: true, mode: "ios" });
  });

  it("prefers the native prompt over the iOS path when both apply", () => {
    expect(installPromptState({ canPrompt: true, isIOS: true }).mode).toBe(
      "prompt",
    );
  });

  it("hides on a desktop browser with no prompt and no iOS", () => {
    expect(
      installPromptState({ canPrompt: false, isIOS: false }),
    ).toEqual({ show: false, mode: null });
  });

  it("treats a missing env as nothing-to-show", () => {
    expect(installPromptState()).toEqual({ show: false, mode: null });
  });
});

describe("detectIOS", () => {
  it("detects an iPhone from its UA", () => {
    expect(detectIOS(IPHONE_UA, 5)).toBe(true);
  });

  it("detects iPadOS 13+ Safari posing as desktop Macintosh (touch-capable)", () => {
    expect(detectIOS(IPADOS_DESKTOP_UA, 5)).toBe(true);
  });

  it("does not treat a real Mac (no touch) as iOS", () => {
    expect(detectIOS(MAC_UA, 0)).toBe(false);
    // A Mac UA with an undefined touch count must not throw or false-positive.
    expect(detectIOS(MAC_UA)).toBe(false);
  });

  it("is false for Android and a missing UA", () => {
    expect(detectIOS(ANDROID_UA, 5)).toBe(false);
    expect(detectIOS()).toBe(false);
  });
});

describe("isDismissed", () => {
  it("is true only when the stored flag is exactly '1'", () => {
    const store = { getItem: (k) => (k === "seen" ? "1" : null) };
    expect(isDismissed("seen", store)).toBe(true);
    expect(isDismissed("other", store)).toBe(false);
  });

  it("is false (never throws) when storage access throws", () => {
    const store = {
      getItem() {
        throw new Error("SecurityError: storage disabled");
      },
    };
    expect(isDismissed("seen", store)).toBe(false);
  });
});

// ---- install tracking (#509 slice 3) ----
//
// A fresh install is reported through the client beacon (window.mesoTrack)
// exactly once per device, off whichever of two signals fires first:
// `appinstalled` (Chromium/Android) or the standalone check evaluated on
// load (iOS's only signal, and true on every load once installed). The
// decision of WHETHER to look is `shouldTrackInstall` — a pure predicate,
// unit-tested directly like `installPromptState`, below.

describe("shouldTrackInstall", () => {
  it("is true for a fresh standalone load (not yet tracked)", () => {
    expect(shouldTrackInstall({ standalone: true, tracked: false })).toBe(
      true,
    );
  });

  it("is false once the flag is already set", () => {
    expect(shouldTrackInstall({ standalone: true, tracked: true })).toBe(
      false,
    );
  });

  it("is false when the app isn't running standalone", () => {
    expect(shouldTrackInstall({ standalone: false, tracked: false })).toBe(
      false,
    );
  });

  it("treats a missing env as nothing to track", () => {
    expect(shouldTrackInstall()).toBe(false);
  });
});

// The DOM wiring around install tracking (`reportInstall`,
// `initInstallTracking`, `storagePersists`) isn't exported — like
// `installPromptState`'s render wiring, it's exercised through the real
// module rather than a mock. Under review, three things about it changed
// that make the OLD technique (dispatch a real `appinstalled` event against
// the module instance this file statically imported once, at the top)
// unsafe to keep using:
//
//  1. The flag write moved INSIDE mesoTrack's own `.then(landed => ...)` —
//     it's asynchronous now, so a test has to await mesoTrack's returned
//     promise before checking localStorage, not just the trigger.
//  2. `reportInstall` grew a same-page-load guard (`installReportStarted`)
//     that nothing short of a fresh module instance resets — a real reload,
//     in production. The statically-imported instance at the top of this
//     file is ONE such "page load" for this whole test file: the first test
//     that successfully trips it leaves every later test dispatching against
//     that same instance seeing zero calls.
//  3. A new `storagePersists()` probe means some scenarios below (storage
//     that can't persist, or a device that's already been told) never trip
//     that guard at all — so a listener from one of those tests would stay
//     armed on `window` for the rest of the file, ready to double-fire on
//     the next real dispatch.
//
// So every test below gets its own "page load" via `vi.resetModules()` + a
// fresh dynamic `import()` (the same technique meso_push.test.js uses for
// its own import-time config capture), and reads the `appinstalled` handler
// straight off `addEventListener` rather than going through
// `window.dispatchEvent` — invoking it directly is exactly what a real
// dispatch does (the handler doesn't read anything off the event object),
// without touching whatever an earlier test's instance left registered on
// the shared `window`.

const INSTALL_TRACKED_KEY = "meso-install-tracked";
const ONBOARDING_MODULE_PATH =
  "../app/store_project/static/js/meso_onboarding.js";

// A fresh "page load": a brand-new module instance (a fresh
// `installReportStarted` closure), with its `appinstalled` handler captured
// off `addEventListener` at import time. jsdom's `document.readyState` is
// "complete" throughout this suite, so the module's own bootstrap runs
// `init()` synchronously during the import — no DOMContentLoaded wait.
async function loadOnboardingAppInstalledHandler() {
  vi.resetModules();
  const addSpy = vi.spyOn(window, "addEventListener");
  await import(ONBOARDING_MODULE_PATH);
  const registered = addSpy.mock.calls.find(
    ([type]) => type === "appinstalled",
  );
  addSpy.mockRestore();
  return registered && registered[1];
}

// A fresh "page load" that starts out already running standalone — the only
// signal iOS ever gives (it never fires `appinstalled`), and the fallback
// for an install this page never saw the event for. `initInstallTracking`'s
// on-load check runs synchronously as part of `init()`, during the import.
async function loadOnboardingStandalone() {
  vi.resetModules();
  window.matchMedia = vi.fn().mockReturnValue({ matches: true });
  await import(ONBOARDING_MODULE_PATH);
}

// Both `reportInstall` entry points read `window.mesoTrack` fresh on every
// call, so a stub just needs to be a resolvable promise. Awaiting mesoTrack's
// OWN returned promise is enough to know `reportInstall`'s
// `.then(landed => ...)` has already run too: callbacks chained on the same
// promise fire in registration order, and reportInstall's was registered
// before any of these helpers get a chance to await it.
function flushMesoTrack(callIndex = 0) {
  return window.mesoTrack.mock.results[callIndex].value;
}

describe("appinstalled listener: reports once per device", () => {
  beforeEach(() => {
    localStorage.clear();
    delete window.mesoTrack;
  });

  it("reports pwa_installed via 'appinstalled'", async () => {
    window.mesoTrack = vi.fn().mockResolvedValue(true);
    const onAppInstalled = await loadOnboardingAppInstalledHandler();

    onAppInstalled();
    await flushMesoTrack();

    expect(window.mesoTrack).toHaveBeenCalledTimes(1);
    expect(window.mesoTrack).toHaveBeenCalledWith("pwa_installed", {
      via: "appinstalled",
    });
  });

  it("reports nothing on a second appinstalled — the flag is already set", async () => {
    window.mesoTrack = vi.fn().mockResolvedValue(true);
    const onAppInstalled = await loadOnboardingAppInstalledHandler();

    onAppInstalled();
    onAppInstalled(); // same page load: the in-memory guard blocks this synchronously
    await flushMesoTrack();

    expect(window.mesoTrack).toHaveBeenCalledTimes(1);
  });

  it("sets the tracked flag so a later reload's on-init check is a no-op too", async () => {
    window.mesoTrack = vi.fn().mockResolvedValue(true);
    expect(isDismissed(INSTALL_TRACKED_KEY)).toBe(false);
    const onAppInstalled = await loadOnboardingAppInstalledHandler();

    onAppInstalled();
    await flushMesoTrack();

    expect(isDismissed(INSTALL_TRACKED_KEY)).toBe(true);
  });

  it("does not throw when window.mesoTrack isn't defined", async () => {
    const onAppInstalled = await loadOnboardingAppInstalledHandler();
    expect(() => onAppInstalled()).not.toThrow();
  });
});

// The install flag is written only when the beacon actually landed
// (#509 hardening). Previously meso_onboarding.js wrote
// `meso-install-tracked` BEFORE firing the beacon, and mesoTrack swallowed
// every failure — so an athlete who opened the installed app offline once (an
// ordinary Tuesday for a gym PWA) lost their install report for good. Now the
// flag is written only inside mesoTrack's own `.then(landed => ...)`, and
// only when `landed` is true.
describe("reportInstall: writes the flag only when the beacon landed", () => {
  beforeEach(() => {
    localStorage.clear();
    delete window.mesoTrack;
    delete window.matchMedia;
  });

  // The offline-install-lost regression: a beacon that never arrives must
  // not cost the athlete their install report. No flag gets written, and the
  // very next load (a fresh module instance — installReportStarted doesn't
  // survive a reload either) retries rather than assuming it's already been
  // told.
  it("leaves the flag unset on a failed beacon, so the next load retries", async () => {
    window.mesoTrack = vi.fn().mockResolvedValue(false);
    await loadOnboardingStandalone();
    await flushMesoTrack(0);

    expect(isDismissed(INSTALL_TRACKED_KEY)).toBe(false);
    expect(window.mesoTrack).toHaveBeenCalledTimes(1);

    // The next load: nothing durable stopped it, so it fires again.
    await loadOnboardingStandalone();
    await flushMesoTrack(1);

    expect(window.mesoTrack).toHaveBeenCalledTimes(2);
  });

  // The success counterpart: a landed beacon sets the flag for good, so a
  // second load's on-init check reads it and stays quiet — this is what
  // makes "report once per device" durable across reloads rather than just
  // good for one page's in-memory guard.
  it("sets the flag on a successful beacon, so a second init fires nothing", async () => {
    window.mesoTrack = vi.fn().mockResolvedValue(true);
    await loadOnboardingStandalone();
    await flushMesoTrack(0);

    expect(isDismissed(INSTALL_TRACKED_KEY)).toBe(true);

    await loadOnboardingStandalone();

    expect(window.mesoTrack).toHaveBeenCalledTimes(1); // still just the first load's call
  });
});

// Install reporting is skipped entirely when localStorage can't persist
// (#509 hardening). `isDismissed` answers "not dismissed" when storage
// throws (Safari private mode, blocked site data, ITP) — the right default
// for a coachmark someone waved away, wrong here: on an installed iOS device
// every load IS standalone, so an inert flag would report an install on
// every single page view, forever. `storagePersists()` probes with a real
// write/read/remove, and `reportInstall` bails before ever touching the
// beacon when it fails. Asserted across three separate "loads", not just the
// first — a device that can't persist the flag must never be reported, not
// just not reported once.
describe("reportInstall: skipped entirely when localStorage can't persist", () => {
  beforeEach(() => {
    localStorage.clear();
    delete window.mesoTrack;
    delete window.matchMedia;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reports nothing on a standalone load, nor on a second or third one", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });

    for (let i = 0; i < 3; i++) {
      window.mesoTrack = vi.fn().mockResolvedValue(true);
      await loadOnboardingStandalone();
      expect(window.mesoTrack).not.toHaveBeenCalled();
    }
  });
});

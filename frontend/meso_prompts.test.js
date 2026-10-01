// Prompt-card exclusivity across meso_push.js and meso_onboarding.js (#669).
//
// The athlete home renders several prompt cards (push = 1, install = 2,
// first-log tip = 3, each with data-prompt-priority). meso_push.js un-hides
// the push card; meso_onboarding.js is the coordinator that suppresses all but
// one via data-prompt-suppressed. Exactly-one-prompt must not depend on which
// script executes first, nor on one of them never running.
//
// Both scripts act when they execute (and meso_onboarding.js runs init()
// immediately because readyState isn't "loading" under vitest), so each test
// builds the DOM + stubs first, then vi.resetModules() + dynamic import().

const PUSH_PATH = "../app/store_project/static/js/meso_push.js";
const ONBOARDING_PATH = "../app/store_project/static/js/meso_onboarding.js";

const PUSH_KEY = "meso-push-dismissed";
const VAPID =
  "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ";

function render({ pushEnabled = true } = {}) {
  document.body.innerHTML =
    '<span id="meso-pwa-config" hidden data-subscribe-url="/s/" data-csrf="t" ' +
    `data-push-enabled="${pushEnabled ? "1" : "0"}"></span>` +
    `<meta name="meso-vapid-key" content="${VAPID}">` +
    `<div id="meso-push-prompt" data-prompt-priority="1" data-prompt-dismiss-key="${PUSH_KEY}" hidden>` +
    '<button id="meso-push-cta">Enable</button>' +
    "<button data-prompt-dismiss>No</button></div>" +
    '<div id="tip" data-prompt-priority="3" data-prompt-dismiss-key="meso-coachmark-firstlog-home" data-prompt-suppressed></div>';
}

function stubPush({ permission = "default", supported = true } = {}) {
  window.Notification = { permission, requestPermission: vi.fn() };
  if (supported) {
    Object.defineProperty(window.navigator, "serviceWorker", {
      configurable: true,
      value: { ready: new Promise(() => {}) },
    });
    window.PushManager = function () {};
  } else {
    delete window.PushManager;
  }
  global.fetch = vi.fn().mockResolvedValue({ ok: true });
}

const push = () => document.getElementById("meso-push-prompt");
const tip = () => document.getElementById("tip");
const visible = (el) => !el.hidden && !el.hasAttribute("data-prompt-suppressed");
const shown = () => [push(), tip()].filter(visible).map((el) => el.id);

const listeners = [];
let realAdd;

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  delete window.Notification;
  delete window.PushManager;
  delete window.mesoEnablePush;
  // meso_onboarding.js registers window listeners on every import; drop the
  // previous runs' so their stale closures can't react to this test's events.
  realAdd = window.addEventListener;
  window.addEventListener = function (type, fn, opts) {
    if (type.startsWith("meso:")) listeners.push([type, fn]);
    return realAdd.call(this, type, fn, opts);
  };
});

afterEach(() => {
  window.addEventListener = realAdd;
  while (listeners.length) {
    const [type, fn] = listeners.pop();
    window.removeEventListener(type, fn);
  }
});

const loadPush = () => import(PUSH_PATH);
const loadOnboarding = () => import(ONBOARDING_PATH);

describe("meso_push.js alone (onboarding never loaded)", () => {
  it("keeps a dismissed push card hidden", async () => {
    render();
    stubPush();
    window.localStorage.setItem(PUSH_KEY, "1");
    await loadPush();
    expect(push().hidden).toBe(true);
  });

  it("un-hides the push card when not dismissed (control)", async () => {
    render();
    stubPush();
    await loadPush();
    expect(push().hidden).toBe(false);
  });

  it("with onboarding blocked, the server-suppressed tip stays suppressed", async () => {
    render();
    stubPush();
    await loadPush();
    expect(tip().hasAttribute("data-prompt-suppressed")).toBe(true);
    expect(shown()).toEqual(["meso-push-prompt"]);
  });

  it("with onboarding blocked and push dismissed, nothing shows", async () => {
    render();
    stubPush();
    window.localStorage.setItem(PUSH_KEY, "1");
    await loadPush();
    expect(tip().hasAttribute("data-prompt-suppressed")).toBe(true);
    expect(shown()).toEqual([]);
  });
});

const ORDERS = {
  "normal order (push, then onboarding)": async () => {
    await loadPush();
    await loadOnboarding();
  },
  "reversed order (onboarding, then push)": async () => {
    await loadOnboarding();
    await loadPush();
  },
};

for (const [name, run] of Object.entries(ORDERS)) {
  describe(name, () => {
    it("push wins over the tip when eligible", async () => {
      render();
      stubPush();
      await run();
      expect(shown()).toEqual(["meso-push-prompt"]);
      expect(tip().hasAttribute("data-prompt-suppressed")).toBe(true);
    });

    it("the tip alone shows when push is not supported", async () => {
      render();
      stubPush({ supported: false });
      await run();
      expect(shown()).toEqual(["tip"]);
    });

    it("the tip alone shows when push is disabled server-side", async () => {
      render({ pushEnabled: false });
      stubPush();
      await run();
      expect(shown()).toEqual(["tip"]);
    });

    it("the tip alone shows when permission is already decided", async () => {
      render();
      stubPush({ permission: "granted" });
      await run();
      expect(shown()).toEqual(["tip"]);
    });

    it("a dismissed push leaves the tip as the single prompt", async () => {
      render();
      stubPush();
      window.localStorage.setItem(PUSH_KEY, "1");
      await run();
      expect(shown()).toEqual(["tip"]);
    });
  });
}

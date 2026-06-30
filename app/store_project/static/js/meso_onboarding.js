/* Meso — athlete first-run onboarding chrome (first-time UX Phase 4).
 *
 * Two browser-side affordances for a newly-invited athlete:
 *
 *  1. The installable-PWA prompt on the training home. Chromium fires
 *     `beforeinstallprompt`; we stash it (see _pwa_head.html) and reveal the
 *     install card with a real "Install" button. iOS Safari has no such event,
 *     so there we show the manual Add-to-Home-Screen instructions instead. When
 *     the app already runs standalone (installed) or the athlete dismissed the
 *     card, it stays hidden.
 *  2. Coachmark dismissal: the first-log coachmarks are *shown* server-side
 *     (only until the athlete's first log lands), but a dismiss here persists in
 *     localStorage so a coachmark the athlete waved away stays gone on reload.
 *
 * The decision logic is pure + unit-tested (frontend/meso_onboarding.test.js);
 * the DOM wiring is verified at the render level in the Django tests.
 */
(function (root) {
  // ---- pure logic (unit-tested) ----

  // Decide whether/how to show the install card given the browser's situation.
  // `env`: { standalone, dismissed, canPrompt, isIOS }. Returns
  // { show, mode } where mode is "prompt" (a captured native prompt), "ios"
  // (manual Add-to-Home-Screen), or null (nothing to show).
  function installPromptState(env) {
    const e = env || {};
    if (e.standalone || e.dismissed) return { show: false, mode: null };
    if (e.canPrompt) return { show: true, mode: "prompt" };
    if (e.isIOS) return { show: true, mode: "ios" };
    return { show: false, mode: null };
  }

  // Whether this is an iOS device — the install path with no `beforeinstallprompt`,
  // where we show manual Add-to-Home-Screen steps instead. iPhone/iPod/iPad report
  // it in the UA; iPadOS 13+ Safari reports a desktop "Macintosh" UA but is
  // touch-capable (maxTouchPoints > 1), so detect that too or iPads get nothing.
  function detectIOS(ua, maxTouchPoints) {
    const agent = String(ua || "");
    if (/iphone|ipad|ipod/i.test(agent)) return true;
    return /macintosh/i.test(agent) && (maxTouchPoints || 0) > 1;
  }

  // Read a dismissal flag, defensively: storage can be absent or throw (Safari
  // private mode), in which case "not dismissed" is the safe default.
  function isDismissed(key, storage) {
    try {
      const store = storage || root.localStorage;
      return !!store && store.getItem(key) === "1";
    } catch (e) {
      return false;
    }
  }

  // Persist a dismissal flag. Best-effort: if storage is unavailable the card
  // still hides for this page load, it just won't remember next time.
  function setDismissed(key, storage) {
    try {
      const store = storage || root.localStorage;
      if (store) store.setItem(key, "1");
    } catch (e) {
      /* best-effort — the element is hidden in-page regardless */
    }
  }

  // ---- DOM wiring (browser only) ----

  const INSTALL_DISMISS_KEY = "meso-install-dismissed";

  // Toggle visibility via inline `display` rather than the `hidden` attribute:
  // these cards carry an inline `display:flex` for layout, and an author inline
  // style beats the UA `[hidden] { display: none }` rule — so `hidden` alone
  // wouldn't actually hide them. Setting `style.display` directly always wins.
  function hide(el) {
    if (el) el.style.display = "none";
  }
  function show(el, display) {
    if (el) el.style.display = display || "block";
  }

  // localStorage key for one coachmark's dismissal.
  function coachmarkKey(key) {
    return "meso-coachmark-" + key;
  }

  // Hide any already-dismissed coachmark and wire its dismiss control. The
  // coachmark itself is rendered (or not) server-side; this only remembers a
  // manual dismissal across reloads.
  function initCoachmarks(doc) {
    const marks = doc.querySelectorAll("[data-coachmark-key]");
    for (let i = 0; i < marks.length; i++) {
      const el = marks[i];
      const storeKey = coachmarkKey(el.dataset.coachmarkKey);
      if (isDismissed(storeKey)) {
        hide(el);
        continue;
      }
      const btn = el.querySelector("[data-coachmark-dismiss]");
      if (btn) {
        btn.addEventListener("click", function () {
          setDismissed(storeKey);
          hide(el);
        });
      }
    }
  }

  // Snapshot the browser's install situation for installPromptState().
  function readInstallEnv() {
    let standalone = false;
    try {
      standalone =
        (root.matchMedia &&
          root.matchMedia("(display-mode: standalone)").matches) ||
        root.navigator.standalone === true;
    } catch (e) {
      standalone = false;
    }
    const nav = root.navigator || {};
    return {
      standalone: standalone,
      dismissed: isDismissed(INSTALL_DISMISS_KEY),
      canPrompt: !!root.__mesoInstallEvent,
      isIOS: detectIOS(nav.userAgent, nav.maxTouchPoints),
    };
  }

  // Reveal + wire the install card. Re-runs when a deferred `beforeinstallprompt`
  // arrives after first paint (the inline stash dispatches `meso:installable`).
  function initInstallCard(doc) {
    const card = doc.getElementById("meso-install-card");
    if (!card) return; // home only — absent on other athlete pages

    function render() {
      const state = installPromptState(readInstallEnv());
      if (!state.show) {
        hide(card);
        return;
      }
      const promptBlock = card.querySelector('[data-install-mode="prompt"]');
      const iosBlock = card.querySelector('[data-install-mode="ios"]');
      if (state.mode === "prompt") {
        show(promptBlock, "block");
        hide(iosBlock);
      } else {
        hide(promptBlock);
        show(iosBlock, "block");
      }
      show(card, "flex");
    }

    const installBtn = doc.getElementById("meso-install-btn");
    if (installBtn) {
      installBtn.addEventListener("click", async function () {
        const evt = root.__mesoInstallEvent;
        if (!evt) return;
        evt.prompt();
        try {
          await evt.userChoice;
        } catch (e) {
          /* user dismissed the native sheet — leave the card for a retry */
        }
        // The captured event is single-use; drop it and re-evaluate (a granted
        // install now reports standalone, so the card hides on its own).
        root.__mesoInstallEvent = null;
        render();
      });
    }

    const dismissBtn = card.querySelector("[data-install-dismiss]");
    if (dismissBtn) {
      dismissBtn.addEventListener("click", function () {
        setDismissed(INSTALL_DISMISS_KEY);
        hide(card);
      });
    }

    // A late-arriving install event (Chromium often fires it after load).
    root.addEventListener("meso:installable", render);
    render();
  }

  function init() {
    const doc = root.document;
    if (!doc) return;
    initCoachmarks(doc);
    initInstallCard(doc);
  }

  if (typeof document !== "undefined" && document.addEventListener) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  // Test hook for Node-based runners (vitest); skipped in the browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { installPromptState, isDismissed, detectIOS };
  }
})(typeof window !== "undefined" ? window : this);

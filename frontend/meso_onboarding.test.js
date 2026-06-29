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
} from "../app/store_project/static/js/meso_onboarding.js";

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

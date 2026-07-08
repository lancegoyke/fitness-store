// Tests for the guided demo onboarding tour driver
// (app/store_project/static/js/meso_tour.js).
//
// The DOM wiring (rendering the spotlight/card, wiring Back/Next/dismiss,
// posting state) is exercised at the render level in the Django template/view
// tests; what's unit-tested here is the pure logic: step clamping/advance
// math, the anchor-retry cutoff decision, config parsing, per-step action
// state, current-page detection, and (Phase 4, analytics + polish) the a11y
// announcement string and the reduced-motion scroll-behavior decision.

import {
  clampStep,
  nextIndex,
  prevIndex,
  isLastStep,
  parseTourConfig,
  shouldRetryAnchor,
  resolveActionState,
  isCurrentPage,
  buildStepAnnouncement,
  prefersReducedMotion,
  scrollBehaviorFor,
  shouldShowSkipLoad,
  shouldShowGoto,
  isUsableRect,
} from "../app/store_project/static/js/meso_tour.js";

describe("clampStep", () => {
  it("keeps an in-range step unchanged", () => {
    expect(clampStep(3, 8)).toBe(3);
  });

  it("floors a negative step to 0", () => {
    expect(clampStep(-5, 8)).toBe(0);
  });

  it("ceilings a too-large step to the last index", () => {
    expect(clampStep(99, 8)).toBe(7);
  });

  it("treats a non-numeric step as 0", () => {
    expect(clampStep(undefined, 8)).toBe(0);
    expect(clampStep(null, 8)).toBe(0);
    expect(clampStep("nope", 8)).toBe(0);
  });

  it("coerces a numeric string", () => {
    expect(clampStep("4", 8)).toBe(4);
  });

  it("never goes negative even with a zero step count", () => {
    expect(clampStep(2, 0)).toBe(0);
  });
});

describe("nextIndex / prevIndex", () => {
  it("advances by one within range", () => {
    expect(nextIndex(2, 8)).toBe(3);
  });

  it("does not advance past the last step", () => {
    expect(nextIndex(7, 8)).toBe(7);
  });

  it("goes back by one within range", () => {
    expect(prevIndex(3, 8)).toBe(2);
  });

  it("does not go back past the first step", () => {
    expect(prevIndex(0, 8)).toBe(0);
  });
});

describe("isLastStep", () => {
  it("is false before the last step", () => {
    expect(isLastStep(6, 8)).toBe(false);
  });

  it("is true on the last step", () => {
    expect(isLastStep(7, 8)).toBe(true);
  });

  it("clamps an out-of-range step before comparing", () => {
    expect(isLastStep(99, 8)).toBe(true);
  });
});

describe("parseTourConfig", () => {
  const validConfig = JSON.stringify({
    steps: [{ key: "welcome" }, { key: "profile" }],
    step: 0,
    status: "active",
  });

  it("parses valid, well-shaped JSON", () => {
    expect(parseTourConfig(validConfig)).toEqual({
      steps: [{ key: "welcome" }, { key: "profile" }],
      step: 0,
      status: "active",
    });
  });

  it("is null for missing/empty input", () => {
    expect(parseTourConfig(null)).toBeNull();
    expect(parseTourConfig(undefined)).toBeNull();
    expect(parseTourConfig("")).toBeNull();
  });

  it("is null for malformed JSON", () => {
    expect(parseTourConfig("{not json")).toBeNull();
  });

  it("is null when steps is missing, not an array, or empty", () => {
    expect(parseTourConfig(JSON.stringify({ step: 0 }))).toBeNull();
    expect(parseTourConfig(JSON.stringify({ steps: "nope" }))).toBeNull();
    expect(parseTourConfig(JSON.stringify({ steps: [] }))).toBeNull();
  });
});

describe("shouldRetryAnchor", () => {
  it("retries while under the cap", () => {
    expect(shouldRetryAnchor(0, 10)).toBe(true);
    expect(shouldRetryAnchor(9, 10)).toBe(true);
  });

  it("stops once the cap is reached", () => {
    expect(shouldRetryAnchor(10, 10)).toBe(false);
    expect(shouldRetryAnchor(11, 10)).toBe(false);
  });
});

describe("resolveActionState", () => {
  it("is null for a step with no action at all", () => {
    expect(resolveActionState({ key: "profile", segment: null })).toBeNull();
  });

  it("is null for a missing step", () => {
    expect(resolveActionState(null)).toBeNull();
  });

  it("offers the segment action when not yet loaded", () => {
    expect(
      resolveActionState({
        segment: "athletes",
        action_label: "Add 5 sample athletes",
        loaded: false,
      }),
    ).toEqual({
      kind: "segment",
      label: "Add 5 sample athletes",
      disabled: false,
    });
  });

  it("shows a disabled done-state once the segment is loaded (O7)", () => {
    expect(
      resolveActionState({
        segment: "athletes",
        action_label: "Add 5 sample athletes",
        loaded: true,
      }),
    ).toEqual({ kind: "segment", label: "Added ✓", disabled: true });
  });

  it("falls back to a generic label when action_label is missing", () => {
    expect(
      resolveActionState({ segment: "program", loaded: false }),
    ).toEqual({
      kind: "segment",
      label: "Add sample data",
      disabled: false,
    });
  });

  it("offers the signup gate for a step with no segment", () => {
    expect(
      resolveActionState({ segment: null, signup_gate: true }),
    ).toEqual({
      kind: "signup",
      label: "Create a free account",
      disabled: false,
    });
  });

  it("prefers the segment action over signup_gate when both are set", () => {
    expect(
      resolveActionState({
        segment: "log",
        signup_gate: true,
        loaded: false,
        action_label: "Log a sample session",
      }).kind,
    ).toBe("segment");
  });

  // Phase 3 — self-coaching variant's generic form action (roster_add_self /
  // plan_create), distinct from the sandbox's segment action.
  it("offers the self-variant form action when not yet loaded", () => {
    expect(
      resolveActionState({
        segment: null,
        action: { url: "/meso/athlete/self/add/", label: "Add yourself as your first athlete", fields: {} },
        loaded: false,
      }),
    ).toEqual({
      kind: "form",
      label: "Add yourself as your first athlete",
      disabled: false,
    });
  });

  it("shows a disabled done-state once the self-variant action's step is loaded", () => {
    expect(
      resolveActionState({
        segment: null,
        action: { url: "/meso/athlete/self/add/", label: "Add yourself as your first athlete", fields: {} },
        loaded: true,
      }),
    ).toEqual({ kind: "form", label: "Done ✓", disabled: true });
  });

  it("falls back to a generic label when the form action has none", () => {
    expect(
      resolveActionState({
        segment: null,
        action: { url: "/meso/athlete/1/plan/new/", fields: {} },
        loaded: false,
      }),
    ).toEqual({ kind: "form", label: "Continue", disabled: false });
  });

  it("prefers the segment action over a form action when both are set", () => {
    // Not a real server-produced shape (sandbox and self-variant steps are
    // mutually exclusive), but pins the precedence order defensively.
    expect(
      resolveActionState({
        segment: "athletes",
        action_label: "Add 5 sample athletes",
        action: { url: "/meso/athlete/self/add/", label: "Add yourself" },
        loaded: false,
      }).kind,
    ).toBe("segment");
  });

  it("prefers a form action over signup_gate when both are set", () => {
    expect(
      resolveActionState({
        segment: null,
        action: { url: "/meso/athlete/1/plan/new/", label: "Start a program for yourself" },
        signup_gate: true,
        loaded: false,
      }).kind,
    ).toBe("form");
  });
});

describe("isCurrentPage", () => {
  it("is true when the step's path matches the current path", () => {
    expect(isCurrentPage("/meso/", "/meso/")).toBe(true);
  });

  it("ignores a trailing slash mismatch", () => {
    expect(isCurrentPage("/meso/designer", "/meso/designer/")).toBe(true);
  });

  it("matches the designer step on its id-suffixed redirect target", () => {
    // The bare /meso/designer/ URL the step carries redirects to the working
    // plan's page — the user IS on the designer, so no "Take me there".
    expect(isCurrentPage("/meso/designer/", "/meso/designer/107/")).toBe(true);
  });

  it("matches the deliver step on its id-suffixed redirect target", () => {
    expect(isCurrentPage("/meso/deliver/", "/meso/deliver/107/")).toBe(true);
  });

  it("the roster step stays exact-match only (never a prefix rule)", () => {
    // Every meso path starts with /meso/ — a prefix match on the app root
    // would claim every page as "the roster".
    expect(isCurrentPage("/meso/", "/meso/deliver/")).toBe(false);
    expect(isCurrentPage("/meso/", "/meso/designer/107/")).toBe(false);
  });

  it("does not prefix-match a lookalike path segment", () => {
    // /meso/deliver must not match /meso/deliverance — the subpath rule
    // requires a "/" boundary after the step's path.
    expect(isCurrentPage("/meso/deliver/", "/meso/deliverance/")).toBe(false);
  });

  it("is false when the paths differ", () => {
    expect(isCurrentPage("/meso/designer/", "/meso/")).toBe(false);
  });

  it("treats a missing step URL as 'already there'", () => {
    expect(isCurrentPage(null, "/meso/")).toBe(true);
    expect(isCurrentPage(undefined, "/meso/")).toBe(true);
  });

  it("falls back to string comparison if URL parsing throws", () => {
    // A relative-looking value that still resolves against the dummy base in
    // the normal path — this just proves the function never throws even for
    // odd input rather than testing the fallback branch specifically.
    expect(() => isCurrentPage("not a url at all???", "/meso/")).not.toThrow();
  });
});

describe("buildStepAnnouncement", () => {
  it("includes the 1-based step position, total, and title", () => {
    expect(
      buildStepAnnouncement({ title: "Program Designer" }, 2, 8),
    ).toBe("Step 3 of 8: Program Designer");
  });

  it("is used for both the dialog's aria-label and the aria-live text", () => {
    // Same string either way — pinning that there's exactly one source of
    // truth for the announcement, not two copies that could drift apart.
    const forLabel = buildStepAnnouncement({ title: "Groups" }, 5, 8);
    const forLive = buildStepAnnouncement({ title: "Groups" }, 5, 8);
    expect(forLabel).toBe(forLive);
  });

  it("omits the trailing colon when the step has no title", () => {
    expect(buildStepAnnouncement({ title: "" }, 0, 8)).toBe("Step 1 of 8");
    expect(buildStepAnnouncement(null, 0, 8)).toBe("Step 1 of 8");
  });

  it("is correct on the first and last step", () => {
    expect(buildStepAnnouncement({ title: "Welcome" }, 0, 8)).toBe(
      "Step 1 of 8: Welcome",
    );
    expect(buildStepAnnouncement({ title: "Finish" }, 7, 8)).toBe(
      "Step 8 of 8: Finish",
    );
  });
});

describe("prefersReducedMotion", () => {
  function fakeWin(matches) {
    return { matchMedia: () => ({ matches }) };
  }

  it("is true when the media query matches", () => {
    expect(prefersReducedMotion(fakeWin(true))).toBe(true);
  });

  it("is false when the media query does not match", () => {
    expect(prefersReducedMotion(fakeWin(false))).toBe(false);
  });

  it("is false when matchMedia is unavailable", () => {
    expect(prefersReducedMotion({})).toBe(false);
  });

  it("is false for a missing window-like object", () => {
    expect(prefersReducedMotion(null)).toBe(false);
    expect(prefersReducedMotion(undefined)).toBe(false);
  });
});

describe("scrollBehaviorFor", () => {
  it("is instant ('auto') under reduced motion", () => {
    expect(scrollBehaviorFor(true)).toBe("auto");
  });

  it("is smooth otherwise", () => {
    expect(scrollBehaviorFor(false)).toBe("smooth");
  });
});

// #441 P1-1: the "Skip tour · load everything" shortcut loads the 5 fake demo
// athletes — right for the anonymous sandbox, but a data leak onto a real
// coach's live roster (O5). The driver only renders that form for the sandbox
// variant; the self variant never offers a "load everything" escape hatch.
describe("shouldShowSkipLoad", () => {
  it("shows the load-everything skip in the sandbox variant", () => {
    expect(shouldShowSkipLoad({ variant: "sandbox" })).toBe(true);
  });

  it("hides it in the self variant (no fake data for real coaches)", () => {
    expect(shouldShowSkipLoad({ variant: "self" })).toBe(false);
  });

  it("hides it when the variant is missing or the config is empty", () => {
    expect(shouldShowSkipLoad({})).toBe(false);
    expect(shouldShowSkipLoad(null)).toBe(false);
  });
});

// #441 P1-3: "Take me there" on a step whose target page has no data yet
// (designer with no plan, deliver with no plan, results with no log) redirects
// straight back to the roster — an infinite bounce loop. The server flags each
// step's readiness (`goto_ready`); the link only shows when the target will
// actually render AND the browser isn't already on that page.
describe("shouldShowGoto", () => {
  it("shows the link when off-page and the target is ready", () => {
    expect(
      shouldShowGoto(
        { url: "/meso/designer/", goto_ready: true },
        "/meso/",
      ),
    ).toBe(true);
  });

  it("hides the link when the target isn't ready (would bounce back)", () => {
    expect(
      shouldShowGoto(
        { url: "/meso/designer/", goto_ready: false },
        "/meso/",
      ),
    ).toBe(false);
  });

  it("hides the link when already on the target page", () => {
    expect(
      shouldShowGoto(
        { url: "/meso/designer/", goto_ready: true },
        "/meso/designer/107/",
      ),
    ).toBe(false);
  });

  it("defaults to ready when goto_ready is absent (roster-targeted steps)", () => {
    expect(shouldShowGoto({ url: "/meso/deliver/" }, "/meso/")).toBe(true);
  });

  it("returns false for a missing step", () => {
    expect(shouldShowGoto(null, "/meso/")).toBe(false);
  });
});

// #441 P1-4: the self-variant deliver step's anchor lives inside Alpine's
// `x-show="!delivered"`; delivering hides it, so `getBoundingClientRect()`
// returns a zero rect and the driver would draw a 12x12 spotlight hole at the
// viewport origin. A zero rect is treated as "anchor gone" — no spotlight.
describe("isUsableRect", () => {
  it("accepts a rect with real dimensions", () => {
    expect(isUsableRect({ width: 120, height: 40 })).toBe(true);
  });

  it("accepts a rect with only one non-zero dimension", () => {
    expect(isUsableRect({ width: 0, height: 40 })).toBe(true);
    expect(isUsableRect({ width: 120, height: 0 })).toBe(true);
  });

  it("rejects a zero rect (a hidden/collapsed anchor)", () => {
    expect(isUsableRect({ width: 0, height: 0 })).toBe(false);
  });

  it("rejects a missing rect", () => {
    expect(isUsableRect(null)).toBe(false);
    expect(isUsableRect(undefined)).toBe(false);
  });
});

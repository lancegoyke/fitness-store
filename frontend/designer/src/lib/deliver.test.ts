// Ported from frontend/meso.test.js's "deliver link (deliverHref)"
// describe-block, adapted to the pure function signature (planId,
// viewedWeekId in — no component instance).
import { describe, expect, it } from "vitest";
import { deliverHref } from "./deliver";

describe("deliverHref", () => {
  it("carries the viewed week as a ?week= query param", () => {
    expect(deliverHref(7, 2)).toBe("/meso/deliver/7/?week=2");
  });

  it("omits ?week= when no week is viewed yet", () => {
    expect(deliverHref(7, null)).toBe("/meso/deliver/7/");
  });

  it("falls back to the bare deliver URL without a plan", () => {
    expect(deliverHref(null, 2)).toBe("/meso/deliver/");
  });
});

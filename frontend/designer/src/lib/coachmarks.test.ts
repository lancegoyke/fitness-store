// Ported from frontend/meso.test.js's "designer coachmarks" describe-block,
// adapted to the pure storage primitives (storageKey/readDismissed/dismiss).
// The reactive dismissed-map / coachmarkVisible derivation moves to
// useCoachmarks's own RTL specs (CONTRACT.md).
import { beforeEach, describe, expect, it, vi } from "vitest";
import { COACHMARK_KEYS, dismiss, readDismissed, storageKey } from "./coachmarks";

beforeEach(() => {
  window.localStorage.clear();
});

describe("storageKey", () => {
  it("namespaces each coachmark's storage key", () => {
    expect(storageKey("grid")).toBe("meso-coachmark-designer-grid");
    expect(storageKey("phone")).toBe("meso-coachmark-designer-phone");
    expect(storageKey("table")).toBe("meso-coachmark-designer-table");
  });
});

describe("COACHMARK_KEYS", () => {
  it("lists the designer region notes, including the multi-week table (issue #455 phase A4)", () => {
    expect(COACHMARK_KEYS).toEqual(["grid", "phone", "table"]);
  });
});

describe("readDismissed", () => {
  it("is false for every key until dismissed", () => {
    for (const key of COACHMARK_KEYS) {
      expect(readDismissed(key)).toBe(false);
    }
  });

  it("reads a previously persisted dismissal", () => {
    window.localStorage.setItem("meso-coachmark-designer-phone", "1");
    expect(readDismissed("phone")).toBe(true);
    expect(readDismissed("grid")).toBe(false);
  });

  it("treats a storage read failure as not-dismissed", () => {
    const spy = vi
      .spyOn(window.localStorage.__proto__, "getItem")
      .mockImplementation(() => {
        throw new Error("SecurityError");
      });
    expect(readDismissed("grid")).toBe(false);
    spy.mockRestore();
  });
});

describe("dismiss", () => {
  it("persists the dismissal", () => {
    dismiss("agent");
    expect(window.localStorage.getItem("meso-coachmark-designer-agent")).toBe("1");
    expect(readDismissed("agent")).toBe(true);
    expect(readDismissed("grid")).toBe(false); // others unaffected
  });

  it("never throws when storage write fails (private mode)", () => {
    const spy = vi
      .spyOn(window.localStorage.__proto__, "setItem")
      .mockImplementation(() => {
        throw new Error("QuotaExceeded");
      });
    expect(() => dismiss("grid")).not.toThrow();
    spy.mockRestore();
  });
});

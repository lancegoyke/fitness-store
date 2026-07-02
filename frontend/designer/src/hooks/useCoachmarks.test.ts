// Specs for useCoachmarks (CONTRACT.md "useCoachmarks") — seeds dismissed
// state from lib/coachmarks.ts's readDismissed for every COACHMARK_KEYS entry,
// dismissCoachmark reassigns (non-mutating) + persists best-effort. Ported
// from meso.test.js's "designer coachmarks" block.
import { act, renderHook } from "@testing-library/react";
import { useCoachmarks } from "./useCoachmarks";
import { COACHMARK_KEYS, storageKey } from "../lib/coachmarks";

beforeEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("initial hydration", () => {
  it("shows every coachmark until dismissed", () => {
    const { result } = renderHook(() => useCoachmarks());
    for (const key of COACHMARK_KEYS) {
      expect(result.current.coachmarkVisible(key)).toBe(true);
    }
  });

  it("loads a previously persisted dismissal into reactive state", () => {
    window.localStorage.setItem(storageKey("phone"), "1");
    const { result } = renderHook(() => useCoachmarks());
    expect(result.current.coachmarkVisible("phone")).toBe(false);
    expect(result.current.coachmarkVisible("grid")).toBe(true);
  });
});

describe("dismissCoachmark", () => {
  it("hides the coachmark and persists the dismissal, leaving others untouched", () => {
    const { result } = renderHook(() => useCoachmarks());
    act(() => result.current.dismissCoachmark("grid"));
    expect(result.current.coachmarkVisible("grid")).toBe(false);
    expect(result.current.coachmarkVisible("phone")).toBe(true);
    expect(window.localStorage.getItem(storageKey("grid"))).toBe("1");
  });

  it("still hides in-page when the storage write throws (private mode)", () => {
    const { result } = renderHook(() => useCoachmarks());
    const spy = vi
      .spyOn(window.localStorage.__proto__, "setItem")
      .mockImplementation(() => {
        throw new Error("QuotaExceeded");
      });
    expect(() => act(() => result.current.dismissCoachmark("grid"))).not.toThrow();
    expect(result.current.coachmarkVisible("grid")).toBe(false);
    spy.mockRestore();
  });

  it("reassigns the dismissed map rather than mutating it", () => {
    const { result } = renderHook(() => useCoachmarks());
    const before = result.current.dismissed;
    act(() => result.current.dismissCoachmark("grid"));
    expect(result.current.dismissed).not.toBe(before);
  });
});

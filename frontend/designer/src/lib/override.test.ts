// Ported from frontend/meso.test.js's "override editor" describe-block —
// only the pure draft/hasExisting/parseOverrideLoadPct cases; the
// open/select/save/clear network flow moves to useOverrideEditor's own
// RTL specs (CONTRACT.md).
import { describe, expect, it } from "vitest";
import { overrideDraft, overrideHasExisting, parseOverrideLoadPct } from "./override";
import type { Exercise } from "./api";

// A shared-program row Maya already adjusts (load 90% of a 100kg base) —
// mirrors meso.test.js's groupRow() fixture.
function groupRow(overrides: Partial<Exercise> = {}): Exercise {
  return {
    id: 11,
    name: "Back Squat",
    sets: "3",
    reps: "10",
    load: "100",
    adj: "MO -10%",
    adjusts: [
      {
        id: "a1",
        name: "Maya Okonkwo",
        initials: "MO",
        label: "-10%",
        swap: "",
        load_pct: 90,
        sets: "",
        reps: "",
        note: "",
      },
    ],
    ...overrides,
  };
}

describe("overrideDraft", () => {
  it("pre-fills from a member's stored adjust, as strings", () => {
    const draft = overrideDraft(groupRow(), "a1");
    expect(draft.load_pct).toBe("90");
    expect(draft.swap).toBe("");
    expect(draft.sets).toBe("");
    expect(draft.reps).toBe("");
    expect(draft.note).toBe("");
  });

  it("blanks the draft for a member with no stored adjust", () => {
    const draft = overrideDraft(groupRow(), "a2");
    expect(draft).toEqual({ swap: "", load_pct: "", sets: "", reps: "", note: "" });
  });
});

describe("overrideHasExisting", () => {
  it("is true for a member with a stored adjust", () => {
    expect(overrideHasExisting(groupRow(), "a1")).toBe(true);
  });

  it("is false for a member with none", () => {
    expect(overrideHasExisting(groupRow(), "a2")).toBe(false);
  });
});

describe("parseOverrideLoadPct", () => {
  it("blank clears to null", () => {
    expect(parseOverrideLoadPct("")).toEqual({ ok: true, value: null });
    expect(parseOverrideLoadPct("   ")).toEqual({ ok: true, value: null });
  });

  it("accepts a whole number in the 1-200 band", () => {
    expect(parseOverrideLoadPct("85")).toEqual({ ok: true, value: 85 });
    expect(parseOverrideLoadPct("1")).toEqual({ ok: true, value: 1 });
    expect(parseOverrideLoadPct("200")).toEqual({ ok: true, value: 200 });
  });

  it("rejects a non-numeric value", () => {
    expect(parseOverrideLoadPct("abc")).toEqual({ ok: false });
  });

  it("rejects an out-of-band value", () => {
    expect(parseOverrideLoadPct("0")).toEqual({ ok: false });
    expect(parseOverrideLoadPct("201")).toEqual({ ok: false });
    expect(parseOverrideLoadPct("500")).toEqual({ ok: false });
  });
});

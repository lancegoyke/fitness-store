// Ported from frontend/meso.test.js's "coach 1RM editor" describe-block —
// only the parseOneRm-relevant cases (the reject/accept boundary); the
// open/save/network/error-message assertions move to the useOneRmEditor
// hook's own RTL specs (CONTRACT.md).
import { describe, expect, it } from "vitest";
import { parseOneRm } from "./oneRm";

describe("parseOneRm", () => {
  it("accepts a blank value (clears back to the log-derived estimate)", () => {
    expect(parseOneRm("")).toEqual({ ok: true, value: "" });
    expect(parseOneRm("   ")).toEqual({ ok: true, value: "" });
  });

  it("accepts a positive number, trimmed", () => {
    expect(parseOneRm("150")).toEqual({ ok: true, value: "150" });
    expect(parseOneRm("  140.5  ")).toEqual({ ok: true, value: "140.5" });
  });

  it("rejects a non-numeric value", () => {
    expect(parseOneRm("heavy")).toEqual({ ok: false });
  });

  it("rejects zero or a negative number", () => {
    expect(parseOneRm("0")).toEqual({ ok: false });
    expect(parseOneRm("-10")).toEqual({ ok: false });
  });
});

// loadSuffix cases ported from frontend/meso.test.js's "load type (%1RM)"
// describe-block (the loadSuffix-specific its; toggleLoadType/persistRow
// move to useAutosave, not lib). numeric/barH/cellOn/cellStyle had no direct
// spec on createMeso() before (exercised only indirectly via the Alpine
// template) — new specs here pin them to the source read verbatim from
// meso.js so the port is provably faithful.
import { describe, expect, it } from "vitest";
import { barH, cellOn, cellStyle, loadSuffix, numeric } from "./grid";

describe("numeric", () => {
  it("accepts plain non-negative decimal strings, including a bare 0", () => {
    expect(numeric("100")).toBe(true);
    expect(numeric("12.5")).toBe(true);
    expect(numeric(0)).toBe(true); // String(0) === "0" — a valid numeric string
  });

  it("rejects blank, non-numeric, and null/undefined", () => {
    expect(numeric("")).toBe(false);
    expect(numeric("  ")).toBe(false);
    expect(numeric("BW")).toBe(false);
    expect(numeric(null)).toBe(false);
    expect(numeric(undefined)).toBe(false);
  });
});

describe("loadSuffix", () => {
  it("shows the unit for an absolute (or typeless) numeric load", () => {
    expect(loadSuffix({ load: "100", load_type: "abs" }, "kg")).toBe("kg");
    expect(loadSuffix({ load: "100" }, "kg")).toBe("kg"); // typeless → absolute
  });

  it("shows % for a percent load and nothing for non-numeric", () => {
    expect(loadSuffix({ load: "75", load_type: "pct" }, "lb")).toBe("%");
    expect(loadSuffix({ load: "BW", load_type: "abs" }, "lb")).toBe("");
    expect(loadSuffix({ load: "", load_type: "pct" }, "lb")).toBe("");
  });

  it("handles a null/undefined exercise", () => {
    expect(loadSuffix(null, "kg")).toBe("");
    expect(loadSuffix(undefined, "kg")).toBe("");
  });
});

describe("barH", () => {
  it("scales pct against the track height", () => {
    expect(barH(50, 156)).toBe("78px");
    expect(barH(100, 156)).toBe("156px");
  });

  it("clamps to a 6px floor for a near-zero pct", () => {
    expect(barH(0, 156)).toBe("6px");
    expect(barH(1, 100)).toBe("6px");
  });
});

describe("cellOn", () => {
  it("is on for the default Mon/Wed/Fri fixture columns", () => {
    expect(cellOn({ deload: false }, 0)).toBe(true); // Mon
    expect(cellOn({ deload: false }, 2)).toBe(true); // Wed
    expect(cellOn({ deload: false }, 4)).toBe(true); // Fri
    expect(cellOn({ deload: false }, 1)).toBe(false); // Tue
  });

  it("a deload week suppresses the Friday (index 4) column", () => {
    expect(cellOn({ deload: true }, 4)).toBe(false);
    expect(cellOn({ deload: true }, 0)).toBe(true); // Monday unaffected
  });

  it("accepts an injected sessionDays override", () => {
    expect(cellOn({ deload: false }, 1, [1, 3])).toBe(true);
    expect(cellOn({ deload: false }, 0, [1, 3])).toBe(false);
  });
});

describe("cellStyle", () => {
  it("paints an on cell in the current week with the accent color", () => {
    const style = cellStyle({ current: true, deload: false }, 0);
    expect(style).toContain("background:var(--accent)");
    expect(style).toContain("border:1px solid var(--soft-line)");
  });

  it("paints an on cell in a non-current week with the soft color", () => {
    const style = cellStyle({ current: false, deload: false }, 0);
    expect(style).toContain("background:var(--soft)");
    expect(style).toContain("border:1px solid var(--line)");
  });

  it("paints an off cell with the rail color", () => {
    const style = cellStyle({ current: false, deload: false }, 1);
    expect(style).toContain("background:var(--rail)");
  });
});

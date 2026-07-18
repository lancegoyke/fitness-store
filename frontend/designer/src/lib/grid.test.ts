// numeric/barH/cellOn/cellStyle had no direct spec on createMeso() before
// (exercised only indirectly via the Alpine template) — the specs here pin
// them to the source read verbatim from meso.js so the port is provably
// faithful. (loadSuffix retired in Phase 2a with the typed load fields —
// its cases went with it.)
import { describe, expect, it } from "vitest";
import { barH, cellOn, cellStyle, cycleLabelFromGrid, gridToProgram, numeric } from "./grid";
import type { GridCell, GridDay, GridRow, GridWeek, MesoGrid, Phase } from "./api";

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
  // Programs are date-less and carry no "current week" pointer
  // (docs/meso/remove-current-week-plan.md) — every live week paints the
  // same border/on-color now.
  it("paints an on cell with the soft color", () => {
    const style = cellStyle({ deload: false }, 0);
    expect(style).toContain("background:var(--soft)");
    expect(style).toContain("border:1px solid var(--line)");
  });

  it("paints an off cell with the rail color", () => {
    const style = cellStyle({ deload: false }, 1);
    expect(style).toContain("background:var(--rail)");
  });
});

// --- Issue #455 phase A5: gridToProgram / cycleLabelFromGrid --------------
// The one-week `usePlanData`/`serialize_plan` owner is retired — AthletePreview
// and TopBar's cycle chip now re-source off the grid alone via these two pure
// transforms, so DesignerRoot doesn't need to keep hydrating a second,
// slimmed one-week payload just to feed them.

function week(overrides: Partial<GridWeek> = {}): GridWeek {
  return {
    id: 1,
    index: 0,
    label: "Wk 1",
    phase: "Accum",
    deload: false,
    delivered_at: null,
    vol: 70,
    inten: 65,
    ...overrides,
  };
}

function cell(overrides: Partial<GridCell> = {}): GridCell {
  return {
    prescription_id: 100,
    text: "3 x 5, RPE 8, 100",
    skipped: false,
    lines: [],
    ...overrides,
  };
}

function row(overrides: Partial<GridRow> = {}): GridRow {
  return {
    exercise_slot_id: 9,
    name: "Squat",
    exercise_id: 55,
    order: 0,
    tags: [],
    tempo: "",
    rest: "",
    note: "",
    cells: { "1": cell() },
    ...overrides,
  };
}

function day(overrides: Partial<GridDay> = {}): GridDay {
  return {
    session_slot_id: 1,
    session_id: 11,
    session_ids: { "1": 11 },
    day_number: 1,
    name: "Lower",
    bias: "",
    order: 0,
    rows: [row()],
    ...overrides,
  };
}

function grid(overrides: Partial<MesoGrid> = {}): MesoGrid {
  return {
    mesocycle: { id: 1, plan_id: 7, name: "Block 1", week_count: 1 },
    weeks: [week()],
    days: [day()],
    history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" },
    ...overrides,
  };
}

describe("gridToProgram", () => {
  it("defaults to the FIRST week and resolves each row's cell for it", () => {
    const g = grid();
    const program = gridToProgram(g);
    expect(program).toEqual([
      {
        id: 11,
        n: 1,
        name: "Lower",
        bias: "",
        exercises: [
          {
            id: 100,
            name: "Squat",
            text: "3 x 5, RPE 8, 100",
            lines: [],
            tempo: "",
            rest: "",
            note: "",
            tag: undefined,
            skipped: false,
          },
        ],
      },
    ]);
  });

  it("omits a day whose resolved week has no live session (session_ids omits the week — Codex A5 review)", () => {
    // A per-week session delete leaves the slot live for other weeks; the
    // athlete won't see that day this week, so neither should the preview
    // (the retired serialize_plan filtered on the open week's live sessions).
    const g = grid({
      days: [
        day({ session_slot_id: 1, session_ids: { "1": 11 }, rows: [row()] }),
        day({
          session_slot_id: 2,
          day_number: 2,
          name: "Upper",
          session_id: 99, // display fallback from another week
          session_ids: {}, // no live session for week 1
          rows: [row({ exercise_slot_id: 10, cells: { "1": cell({ prescription_id: 200 }) } })],
        }),
      ],
    });
    const program = gridToProgram(g);
    expect(program).toHaveLength(1);
    expect(program[0]!.name).toBe("Lower");
  });

  it("uses the resolved week's own session id for the preview day, not the display fallback", () => {
    const g = grid({
      days: [day({ session_id: 99, session_ids: { "1": 11 } })],
    });
    expect(gridToProgram(g)[0]!.id).toBe(11);
  });

  it("resolves the requested weekId instead of the default (first) week when given", () => {
    const g = grid({
      weeks: [week({ id: 1 }), week({ id: 2 })],
      days: [
        day({
          session_ids: { "1": 11, "2": 12 }, // a live session per live week (real serializer shape)
          rows: [
            row({
              cells: {
                "1": cell({ prescription_id: 100, text: "3 x 5" }),
                "2": cell({ prescription_id: 200, text: "5 x 5" }),
              },
            }),
          ],
        }),
      ],
    });
    const program = gridToProgram(g, 2);
    expect(program[0]!.exercises[0]!.id).toBe(200);
    expect(program[0]!.exercises[0]!.text).toBe("5 x 5");
  });

  it("uses the row's block name (Phase 2a: the one-week swap fields are gone)", () => {
    const g = grid({
      days: [day({ rows: [row({ name: "Back Squat" })] })],
    });
    const program = gridToProgram(g);
    expect(program[0]!.exercises[0]!.name).toBe("Back Squat");
  });

  it("carries text/lines/tempo/rest/note/skipped onto the derived exercise", () => {
    const g = grid({
      days: [
        day({
          rows: [
            row({
              tempo: "31X1",
              rest: "2 min",
              note: "brace hard",
              cells: {
                "1": cell({
                  text: "4 x 6",
                  lines: [{ id: 5, line: 1, text: "RPE 8" }],
                  skipped: true,
                }),
              },
            }),
          ],
        }),
      ],
    });
    const ex = gridToProgram(g)[0]!.exercises[0]!;
    expect(ex).toMatchObject({
      text: "4 x 6",
      lines: [{ id: 5, line: 1, text: "RPE 8" }],
      tempo: "31X1",
      rest: "2 min",
      note: "brace hard",
      skipped: true,
    });
  });

  it("omits a row with no cell for the resolved week (mirrors session.cells())", () => {
    const g = grid({
      days: [
        day({
          rows: [
            row({ exercise_slot_id: 9, cells: { "1": cell({ prescription_id: 100 }) } }),
            row({ exercise_slot_id: 10, name: "RDL", cells: {} }),
          ],
        }),
      ],
    });
    const program = gridToProgram(g);
    expect(program[0]!.exercises).toHaveLength(1);
    expect(program[0]!.exercises[0]!.id).toBe(100);
  });

  it("carries the row's first tag through as `tag`", () => {
    const g = grid({ days: [day({ rows: [row({ tags: ["main", "compound"] })] })] });
    const program = gridToProgram(g);
    expect(program[0]!.exercises[0]!.tag).toBe("main");
  });

  it("returns one entry per day, in order, even a day with zero resolved exercises", () => {
    const g = grid({
      days: [
        day({ session_slot_id: 1, session_id: 11, name: "Lower" }),
        day({ session_slot_id: 2, session_id: 22, name: "Upper", rows: [] }),
      ],
    });
    const program = gridToProgram(g);
    expect(program.map((d) => d.name)).toEqual(["Lower", "Upper"]);
    expect(program[1]!.exercises).toEqual([]);
  });

  it("returns an empty program when the resolved week doesn't exist", () => {
    const g = grid();
    expect(gridToProgram(g, 999)).toEqual([]);
  });
});

describe("cycleLabelFromGrid", () => {
  const phases: Phase[] = [
    { name: "Base", weeks: "4 wk", state: "done" },
    { name: "Hypertrophy", weeks: "4 wk", state: "current" },
  ];

  it("joins the current phase's name and the FIRST week's label/count", () => {
    const weeks = [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2" })];
    expect(cycleLabelFromGrid(phases, weeks)).toBe("Hypertrophy · Wk 1 / 2");
  });

  it("falls back to the first phase when none is flagged current", () => {
    const noCurrentPhases: Phase[] = [{ name: "Base", weeks: "4 wk", state: "done" }];
    const weeks = [week({ id: 1, label: "Wk 1" })];
    expect(cycleLabelFromGrid(noCurrentPhases, weeks)).toBe("Base · Wk 1 / 1");
  });

  it("omits either half when absent, without a stray separator", () => {
    expect(cycleLabelFromGrid([], [])).toBe("");
    expect(cycleLabelFromGrid(phases, [])).toBe("Hypertrophy");
  });
});

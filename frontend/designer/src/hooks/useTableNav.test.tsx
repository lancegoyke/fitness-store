// Specs for useTableNav (issue #455 phase A1) — the P1 multi-week table's
// roving-tabindex + keyboard-nav + focus-restoration hook. Mirrors
// useGridNav.test.tsx's structure/coverage, generalized from a 1D
// (prescriptionId, column) identity to a 2D (rowId, weekId, field) identity
// over a variable-width week axis (see useTableNav.ts's header for the
// sibling-not-shared rationale).
//
// Phase 2a (text-first cells) collapsed the per-week editable surface from
// six structured fields to ONE freeform "text" input. Phase 2b (spreadsheet
// keyboard flow) then widened both axes to the full sheet: the horizontal
// axis is name → tempo → week 1 text → … → week N text → notes → rest (the
// source spreadsheet's column order, Tab-walkable), and cell identity gained
// a LINE — each week cell's sub-line stack (existing `cell.lines` entries
// plus the trailing ghost at max line + 1) is a run of vertical stops, so
// ArrowDown from a prescription steps INTO its stack before the next row.
// Enter = commit + move down one stop, appending a row at a day's last stop
// (Enter-adds-row) via the onAppendRow option.
//
// Fixture: day 1 (session_slot_id 1) has rows 9, 10; day 2 (session_slot_id
// 2) has row 11 — flattened row order (day-major/row-minor): (9), (10),
// (11). Two weeks (id 1 "Wk 1", id 2 "Wk 2") so ArrowRight/Left week-
// crossing and tier 2b (a week removed, row survives) are exercisable
// without a second fixture. Every cell has an empty `lines` stack unless a
// test says otherwise, so its stops are [0, 1(ghost)].
import { act, renderHook } from "@testing-library/react";
import type { FocusEvent, KeyboardEvent } from "react";
import { useTableNav, tableCellDomKey, tableCellAriaLabel } from "./useTableNav";
import type { TableColumn, UseTableNavResult } from "./useTableNav";
import type { GridCell, GridDay, GridRow, GridWeek, MesoGrid } from "../lib/api";

function week(overrides: Partial<GridWeek> = {}): GridWeek {
  return {
    id: 1,
    index: 0,
    label: "Wk 1",
    phase: "Accum",
    deload: false,
    current: true,
    delivered_at: null,
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

function row(id: number, weekIds: number[], overrides: Partial<GridRow> = {}): GridRow {
  const cells: Record<string, GridCell> = {};
  weekIds.forEach((wid, i) => {
    cells[String(wid)] = cell({ prescription_id: id * 100 + i });
  });
  return {
    exercise_slot_id: id,
    name: `Ex ${id}`,
    exercise_id: id + 1000,
    order: 0,
    tags: [],
    tempo: "",
    rest: "",
    note: "",
    cells,
    ...overrides,
  };
}

function day(id: number, rows: GridRow[], overrides: Partial<GridDay> = {}): GridDay {
  return {
    session_slot_id: id,
    session_id: id + 10,
    session_ids: { "1": id + 10, "2": id + 10 },
    day_number: id,
    name: `Day ${id}`,
    bias: "",
    order: 0,
    rows,
    ...overrides,
  };
}

function grid(overrides: Partial<MesoGrid> = {}): MesoGrid {
  return {
    mesocycle: { id: 1, plan_id: 1, name: "Block", week_count: 2 },
    weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false })],
    days: [day(1, [row(9, [1, 2]), row(10, [1, 2])]), day(2, [row(11, [1, 2])])],
    history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" },
    ...overrides,
  };
}

const GRID: MesoGrid = grid();

const NOOP_CALLBACKS = { onCommit: vi.fn(), onRevert: vi.fn() };

const ROW_COLUMNS: TableColumn[] = ["tempo", "note", "rest"];

/** Mounts one real `<input>` per rendered position of `g` — the full 2b
 * sheet: name, tempo, each week cell's text (line 0) + its sub-lines + its
 * trailing ghost (max line + 1), notes, rest — so the hook's
 * `document.querySelector`-based focus moves have somewhere real to land.
 * Mirrors what GridCellEditor/RowNameEditor/CellSubLineInput/RowColumnInput
 * would render, without needing a full React render of the table tree. A
 * row's OWN `cells` map decides what actually mounts: a week id missing
 * from `row.cells` gets no node for any of its lines — the same "no
 * rendered input at all" shape MesoTable produces for a hole (an
 * add-this-week row's bare `<td/>`, no GridCellEditor) or a skipped cell
 * (em-dash + Unskip button). Fixtures express holes just by leaving a
 * weekId out of `row(id, weekIds)`. The row-scoped columns always mount —
 * MesoTable renders RowNameEditor/RowColumnInput unconditionally,
 * independent of any week's holes. */
function mountCells(g: MesoGrid) {
  const nodes: Record<string, HTMLInputElement> = {};
  function mount(key: string, value: string) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.setAttribute("data-grid-cell", key);
    document.body.appendChild(input);
    nodes[key] = input;
  }
  for (const d of g.days) {
    for (const r of d.rows) {
      mount(tableCellDomKey(r.exercise_slot_id, null, "name"), r.name);
      for (const field of ROW_COLUMNS) {
        mount(tableCellDomKey(r.exercise_slot_id, null, field), String(r[field as "tempo" | "note" | "rest"] ?? ""));
      }
      for (const w of g.weeks) {
        const c = r.cells[String(w.id)];
        if (!c) continue; // hole: no cell for this row this week, no inputs at all.
        mount(tableCellDomKey(r.exercise_slot_id, w.id, "text"), c.text);
        const lineNos = (c.lines ?? []).map((l) => l.line).sort((a, b) => a - b);
        for (const l of c.lines ?? []) {
          mount(tableCellDomKey(r.exercise_slot_id, w.id, "text", l.line), l.text);
        }
        const ghost = (lineNos[lineNos.length - 1] ?? 0) + 1;
        mount(tableCellDomKey(r.exercise_slot_id, w.id, "text", ghost), "");
      }
    }
  }
  return nodes;
}

/** Simulates the DOM swap a real refetchGrid-driven re-render performs: tear
 * down every existing grid cell and mount fresh ones for the new grid. */
function resyncCells(g: MesoGrid) {
  document.querySelectorAll("[data-grid-cell]").forEach((n) => n.remove());
  return mountCells(g);
}

function keyEvent(
  key: string,
  target: HTMLInputElement,
  extra: Partial<{ ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }> = {},
): KeyboardEvent<HTMLInputElement> {
  return {
    key,
    currentTarget: target,
    target,
    preventDefault: vi.fn(),
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    altKey: false,
    ...extra,
  } as unknown as KeyboardEvent<HTMLInputElement>;
}

function focusEvent(target: HTMLInputElement): FocusEvent<HTMLInputElement> {
  return { currentTarget: target, target } as unknown as FocusEvent<HTMLInputElement>;
}

function focus(
  result: UseTableNavResult,
  cells: Record<string, HTMLInputElement>,
  rowId: number,
  weekId: number | null,
  field: TableColumn,
  line = 0,
) {
  act(() => {
    result
      .cellProps(rowId, weekId, field, NOOP_CALLBACKS, line)
      .onFocus(focusEvent(cells[tableCellDomKey(rowId, weekId, field, line)]!));
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("pure helpers", () => {
  describe("tableCellDomKey", () => {
    it("joins rowId, weekId, and field with colons", () => {
      expect(tableCellDomKey(9, 2, "text")).toBe("9:2:text");
    });

    it("uses the 'row' sentinel for a null weekId (the row-scoped columns)", () => {
      expect(tableCellDomKey(9, null, "name")).toBe("9:row:name");
      expect(tableCellDomKey(9, null, "tempo")).toBe("9:row:tempo");
    });

    it("line 0 keeps the legacy 3-part key; a sub-line appends its number", () => {
      expect(tableCellDomKey(9, 2, "text", 0)).toBe("9:2:text");
      expect(tableCellDomKey(9, 2, "text", 1)).toBe("9:2:text:1");
    });
  });

  describe("tableCellAriaLabel", () => {
    it("builds '<name> — <week label> — <field label>' for a week field", () => {
      expect(tableCellAriaLabel("Box Squat", "Wk 2", "text")).toBe("Box Squat — Wk 2 — prescription");
    });

    it("labels a row-scoped column as '<name> — <field label>' (no week context)", () => {
      expect(tableCellAriaLabel("Box Squat", null, "name")).toBe("Box Squat — exercise name");
      expect(tableCellAriaLabel("Box Squat", null, "tempo")).toBe("Box Squat — tempo");
    });

    it("falls back to 'exercise' when the row has no name yet", () => {
      expect(tableCellAriaLabel("", "Wk 1", "text")).toBe("exercise — Wk 1 — prescription");
    });
  });
});

describe("anchor + roving tabindex", () => {
  it("starts anchored on the first row's name column", () => {
    mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "name", line: 0 });
  });

  it("exactly one cell is tabbable (0) initially — every other cell is -1", () => {
    mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    expect(result.current.cellProps(9, null, "name", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(9, null, "tempo", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(10, null, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(11, 2, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("focusing another cell moves the roving 0 to it", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    act(() => {
      result.current.cellProps(10, 2, "text", NOOP_CALLBACKS).onFocus(focusEvent(cells[tableCellDomKey(10, 2, "text")]!));
    });
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: 2, field: "text", line: 0 });
    expect(result.current.cellProps(10, 2, "text", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, null, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("cell identity is (rowId, weekId, field, line), never an index — the same field on a different row/week/line is a different cell", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(9, 2, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(10, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
    // A sub-line is its own cell: focusing the ghost anchors (…, line 1),
    // not the prescription at line 0.
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).onFocus(focusEvent(cells[tableCellDomKey(9, 1, "text", 1)]!));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 1 });
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });
});

describe("ArrowDown / ArrowUp", () => {
  it("ArrowDown from a prescription steps INTO its stack first (the ghost when there are no sub-lines)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text")]!);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text", 1)]);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 1 });
  });

  it("ArrowDown from the last stop of a row's stack moves to the next row's prescription", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text", 1)]!);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: 1, field: "text", line: 0 });
  });

  it("ArrowDown walks an existing sub-line stack in order: line 0 → line 1 → ghost (D3's RPE row)", () => {
    const STACK_GRID = grid({
      days: [day(1, [row(9, [1, 2], { cells: { "1": cell({ prescription_id: 900, lines: [{ line: 1, text: "RPE 8" }] }), "2": cell({ prescription_id: 901 }) } }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])],
    });
    const cells = mountCells(STACK_GRID);
    const { result } = renderHook(() => useTableNav({ grid: STACK_GRID }));
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text")]!));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 1 });
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).onKeyDown(keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text", 1)]!));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 2 });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text", 2)]);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 2).onKeyDown(keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text", 2)]!));
    });
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: 1, field: "text", line: 0 });
  });

  it("ArrowUp mirrors: from a prescription it lands on the PREVIOUS row's last stop (its ghost)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowUp", cells[tableCellDomKey(10, 1, "text")]!);
    act(() => {
      result.current.cellProps(10, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text", 1)]);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 1 });
  });

  it("ArrowDown crosses a day-table boundary (last stop of day 1 -> first row of day 2)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(10, 2, "text", 1)]!);
    act(() => {
      result.current.cellProps(10, 2, "text", NOOP_CALLBACKS, 1).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(11, 2, "text")]);
  });

  it("ArrowDown at the table's very last stop is a no-op but still preventDefault", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(11, 2, "text", 1)]!);
    act(() => {
      result.current.cellProps(11, 2, "text", NOOP_CALLBACKS, 1).onKeyDown(event);
    });
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 11, weekId: 2, field: "text", line: 1 });
  });

  it("ArrowUp at the very first row is a no-op but still preventDefault", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowUp", cells[tableCellDomKey(9, null, "name")]!);
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("the name column moves vertically too, keeping weekId null (single-line: row to row)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, null, "name")]!);
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "name", line: 0 });
  });

  it("the row columns (tempo/notes/rest) move vertically row to row too", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, null, "tempo")]!);
    act(() => {
      result.current.cellProps(9, null, "tempo", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "tempo")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "tempo", line: 0 });
  });
});

describe("ArrowRight / ArrowLeft (caret-conditional)", () => {
  it("ArrowRight at the end of the text moves to the next week's text cell (no special-case)", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.value = "4 x 6";
    textInput.setSelectionRange(5, 5); // caret at end (value.length === 5)
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "text")]);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 2, field: "text", line: 0 });
  });

  it("ArrowLeft at the start of the text moves back to the previous week's text cell", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 2, "text")]!;
    textInput.value = "3 x 5";
    textInput.setSelectionRange(0, 0); // caret at start
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", textInput);
    act(() => {
      result.current.cellProps(9, 2, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text")]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("the horizontal axis is the sheet's column order: name → tempo → weeks → notes → rest", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const path: Array<[number | null, TableColumn]> = [
      [null, "name"],
      [null, "tempo"],
      [1, "text"],
      [2, "text"],
      [null, "note"],
      [null, "rest"],
    ];
    for (let i = 0; i < path.length - 1; i++) {
      const [fromWeek, fromField] = path[i]!;
      const [toWeek, toField] = path[i + 1]!;
      const input = cells[tableCellDomKey(9, fromWeek, fromField)]!;
      input.setSelectionRange(input.value.length, input.value.length);
      act(() => {
        result.current.cellProps(9, fromWeek, fromField, NOOP_CALLBACKS).onKeyDown(keyEvent("ArrowRight", input));
      });
      expect(document.activeElement).toBe(cells[tableCellDomKey(9, toWeek, toField)]);
      expect(result.current.anchor).toEqual({ rowId: 9, weekId: toWeek, field: toField, line: 0 });
    }
  });

  it("ArrowLeft at the start of the tempo column moves back to the name column", () => {
    const cells = mountCells(GRID);
    const tempoInput = cells[tableCellDomKey(9, null, "tempo")]!;
    tempoInput.setSelectionRange(0, 0);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", tempoInput);
    act(() => {
      result.current.cellProps(9, null, "tempo", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "name")]);
  });

  it("a sub-line moves horizontally at its own line, clamping to a shorter stack's nearest stop (the ghost)", () => {
    // Row 9's week 1 has a sub-line (stops 0,1,2-ghost); week 2 has none
    // (stops 0,1-ghost). Moving right from week 1's line-2 ghost clamps to
    // week 2's line-1 ghost — the spreadsheet's "the merged cell is still
    // there" feel, never a skid past the whole cell.
    const STACK_GRID = grid({
      days: [day(1, [row(9, [1, 2], { cells: { "1": cell({ prescription_id: 900, lines: [{ line: 1, text: "RPE 8" }] }), "2": cell({ prescription_id: 901 }) } }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])],
    });
    const cells = mountCells(STACK_GRID);
    const { result } = renderHook(() => useTableNav({ grid: STACK_GRID }));
    const ghost = cells[tableCellDomKey(9, 1, "text", 2)]!;
    ghost.setSelectionRange(0, 0);
    const event = keyEvent("ArrowRight", ghost);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 2).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "text", 1)]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 2, field: "text", line: 1 });
  });

  it("a sub-line keeps its line when the next week has the same stop", () => {
    const cells = mountCells(GRID); // every cell: stops [0, 1(ghost)]
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const ghost = cells[tableCellDomKey(9, 1, "text", 1)]!;
    ghost.setSelectionRange(0, 0);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).onKeyDown(keyEvent("ArrowRight", ghost));
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "text", 1)]);
  });

  it("a sub-line moving into a row-scoped column lands at line 0 (the sheet's merged cell)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const ghost = cells[tableCellDomKey(9, 2, "text", 1)]!;
    ghost.setSelectionRange(0, 0);
    act(() => {
      result.current.cellProps(9, 2, "text", NOOP_CALLBACKS, 1).onKeyDown(keyEvent("ArrowRight", ghost));
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "note")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "note", line: 0 });
  });

  it("ArrowRight mid-text does NOT move focus or preventDefault (native caret move wins)", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.value = "4 x 6, RPE 9";
    textInput.setSelectionRange(4, 4); // caret in the middle
    textInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(textInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("ArrowLeft with a non-collapsed selection does NOT move focus", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.value = "3 x 5";
    textInput.setSelectionRange(0, 1); // a selection, not a collapsed caret
    textInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(textInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("no wrap at an absolute extreme: ArrowLeft at the start of the very first column (name, row 9) is a pure no-op", () => {
    const cells = mountCells(GRID);
    const nameInput = cells[tableCellDomKey(9, null, "name")]!;
    nameInput.setSelectionRange(0, 0);
    nameInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", nameInput);
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(nameInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("no wrap at an absolute extreme: ArrowRight at the end of the very last column (rest, row 9) is a pure no-op", () => {
    const cells = mountCells(GRID);
    const restInput = cells[tableCellDomKey(9, null, "rest")]!;
    restInput.value = "";
    restInput.setSelectionRange(0, 0);
    restInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", restInput);
    act(() => {
      result.current.cellProps(9, null, "rest", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(restInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});

describe("Tab / Shift+Tab (unconditional column walk)", () => {
  it("Tab moves to the next column regardless of caret position", () => {
    const cells = mountCells(GRID);
    const nameInput = cells[tableCellDomKey(9, null, "name")]!;
    nameInput.value = "Ex 9";
    nameInput.setSelectionRange(1, 1); // mid-text: Tab still moves (arrows wouldn't)
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Tab", nameInput);
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "tempo")]);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "tempo", line: 0 });
  });

  it("Shift+Tab moves to the previous column", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Tab", cells[tableCellDomKey(9, 1, "text")]!, { shiftKey: true });
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "tempo")]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("Tab at the row's last column (rest) wraps to the NEXT row's name column", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Tab", cells[tableCellDomKey(9, null, "rest")]!);
    act(() => {
      result.current.cellProps(9, null, "rest", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "name", line: 0 });
  });

  it("Shift+Tab at the row's first column (name) wraps to the PREVIOUS row's rest column", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Tab", cells[tableCellDomKey(10, null, "name")]!, { shiftKey: true });
    act(() => {
      result.current.cellProps(10, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "rest")]);
  });

  it("Tab from a sub-line keeps its line across week columns (clamping like the arrows)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Tab", cells[tableCellDomKey(9, 1, "text", 1)]!);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "text", 1)]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 2, field: "text", line: 1 });
  });

  it("Tab at the table's very last cell is NOT preventDefault'd (native tab order leaves the grid)", () => {
    const cells = mountCells(GRID);
    const restInput = cells[tableCellDomKey(11, null, "rest")]!;
    restInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Tab", restInput);
    act(() => {
      result.current.cellProps(11, null, "rest", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(restInput);
  });

  it("Ctrl+Tab / Alt+Tab are browser chrome, never grid moves", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    for (const extra of [{ ctrlKey: true }, { altKey: true }, { metaKey: true }]) {
      const event = keyEvent("Tab", cells[tableCellDomKey(9, 1, "text")]!, extra);
      act(() => {
        result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
      });
      expect(event.preventDefault).not.toHaveBeenCalled();
    }
  });
});

describe("Enter (commit + move down) / Escape (revert)", () => {
  it("Enter calls the cell's onCommit, preventDefaults, and moves down one stop (the ghost)", () => {
    const cells = mountCells(GRID);
    cells[tableCellDomKey(9, 1, "text")]!.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onCommit = vi.fn();
    const event = keyEvent("Enter", cells[tableCellDomKey(9, 1, "text")]!);
    act(() => {
      result.current.cellProps(9, 1, "text", { ...NOOP_CALLBACKS, onCommit }).onKeyDown(event);
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text", 1)]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 1 });
  });

  it("Enter on the name column moves to the next row's name (single-line column)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", cells[tableCellDomKey(9, null, "name")]!));
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
  });

  it("Enter never crosses a day boundary: at day 1's last stop with no onAppendRow it stays put", () => {
    const cells = mountCells(GRID);
    const input = cells[tableCellDomKey(10, null, "name")]!;
    input.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("Enter", input);
    act(() => {
      result.current.cellProps(10, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });

  it("Escape reverts to the focus-time value via onRevert, preventDefaults, and keeps focus", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.value = "3 x 5";
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onRevert = vi.fn();
    const callbacks = { ...NOOP_CALLBACKS, onRevert };
    textInput.focus();
    act(() => {
      result.current.cellProps(9, 1, "text", callbacks).onFocus(focusEvent(textInput));
    });
    textInput.value = "9 x 9";
    const event = keyEvent("Escape", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", callbacks).onKeyDown(event);
    });
    expect(onRevert).toHaveBeenCalledWith("3 x 5");
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(textInput);
  });

  it("Escape without a prior focus call reverts to the DOM value at keydown time", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.value = "4 x 6";
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onRevert = vi.fn();
    const event = keyEvent("Escape", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", { ...NOOP_CALLBACKS, onRevert }).onKeyDown(event);
    });
    expect(onRevert).toHaveBeenCalledWith("4 x 6");
  });

  it("Enter resets the Escape baseline to the committed value", () => {
    const cells = mountCells(GRID);
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.value = "3 x 5";
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onCommit = vi.fn();
    const onRevert = vi.fn();
    const callbacks = { ...NOOP_CALLBACKS, onCommit, onRevert };
    textInput.focus();
    act(() => {
      result.current.cellProps(9, 1, "text", callbacks).onFocus(focusEvent(textInput));
    });
    textInput.value = "4 x 6";
    act(() => {
      result.current.cellProps(9, 1, "text", callbacks).onKeyDown(keyEvent("Enter", textInput));
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    textInput.value = "5 x 3";
    act(() => {
      result.current.cellProps(9, 1, "text", callbacks).onKeyDown(keyEvent("Escape", textInput));
    });
    expect(onRevert).toHaveBeenCalledWith("4 x 6");
  });
});

describe("Enter-adds-row (onAppendRow)", () => {
  // Day 1's last vertical stop at the name column is row 10's name (single-
  // line column). Rows are non-blank by default (mountCells seeds "Ex 10"),
  // so Enter there fires the append.
  it("Enter at a day's last stop appends: fires onAppendRow with the day id, no focus move yet", () => {
    const cells = mountCells(GRID);
    const input = cells[tableCellDomKey(10, null, "name")]!;
    input.focus();
    const onAppendRow = vi.fn();
    const { result } = renderHook(() => useTableNav({ grid: GRID, onAppendRow }));
    act(() => {
      result.current.cellProps(10, null, "name", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", input));
    });
    expect(onAppendRow).toHaveBeenCalledTimes(1);
    expect(onAppendRow).toHaveBeenCalledWith(1);
    expect(document.activeElement).toBe(input); // focus lands only once the refetch delivers the row.
  });

  it("Enter mid-day still just moves down — never appends", () => {
    const cells = mountCells(GRID);
    const onAppendRow = vi.fn();
    const { result } = renderHook(() => useTableNav({ grid: GRID, onAppendRow }));
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", cells[tableCellDomKey(9, null, "name")]!));
    });
    expect(onAppendRow).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
  });

  it("a fully blank row never appends another (the Enter-Enter-Enter guard)", () => {
    // Row 10 (day 1's last) renders entirely blank — name, tempo, cells,
    // notes, rest all empty — exactly what a just-appended row looks like.
    const BLANK_LAST = grid({
      days: [
        day(1, [row(9, [1, 2]), row(10, [1, 2], { name: "", cells: { "1": cell({ prescription_id: 1000, text: "" }), "2": cell({ prescription_id: 1001, text: "" }) } })]),
        day(2, [row(11, [1, 2])]),
      ],
    });
    const cells = mountCells(BLANK_LAST);
    const input = cells[tableCellDomKey(10, null, "name")]!;
    input.focus();
    const onAppendRow = vi.fn();
    const { result } = renderHook(() => useTableNav({ grid: BLANK_LAST, onAppendRow }));
    act(() => {
      result.current.cellProps(10, null, "name", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", input));
    });
    expect(onAppendRow).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });

  it("an uncommitted draft counts as content: the blank check reads the DOM, not the grid", () => {
    const BLANK_LAST = grid({
      days: [
        day(1, [row(9, [1, 2]), row(10, [1, 2], { name: "", cells: { "1": cell({ prescription_id: 1000, text: "" }), "2": cell({ prescription_id: 1001, text: "" }) } })]),
        day(2, [row(11, [1, 2])]),
      ],
    });
    const cells = mountCells(BLANK_LAST);
    const input = cells[tableCellDomKey(10, null, "name")]!;
    input.value = "Front squat"; // typed but not yet in the grid payload
    input.focus();
    const onAppendRow = vi.fn();
    const { result } = renderHook(() => useTableNav({ grid: BLANK_LAST, onAppendRow }));
    act(() => {
      result.current.cellProps(10, null, "name", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", input));
    });
    expect(onAppendRow).toHaveBeenCalledWith(1);
  });

  it("once the day's last row changes on a grid swap, focus lands on the new row at the column Enter came from", () => {
    let cells = mountCells(GRID);
    const onAppendRow = vi.fn();
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g, onAppendRow }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 10, 1, "text");
    act(() => {
      // (10,1,text) line 0 -> ghost line 1 first…
      result.current.cellProps(10, 1, "text", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", cells[tableCellDomKey(10, 1, "text")]!));
    });
    act(() => {
      // …then Enter at the ghost (day 1's last stop of this column) appends.
      result.current.cellProps(10, 1, "text", NOOP_CALLBACKS, 1).onKeyDown(keyEvent("Enter", cells[tableCellDomKey(10, 1, "text", 1)]!));
    });
    expect(onAppendRow).toHaveBeenCalledWith(1);

    // The commit's own optimistic grid change arrives FIRST — same last row,
    // so the intent must survive it without focusing anything new.
    const OPTIMISTIC = grid({ days: [day(1, [row(9, [1, 2]), row(10, [1, 2], { name: "Ex 10 v2" })]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(OPTIMISTIC);
    act(() => rerender({ grid: OPTIMISTIC }));
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: 1, field: "text", line: 1 });

    // The refetch lands with the appended row 12 — focus jumps to it.
    const APPENDED = grid({
      days: [day(1, [row(9, [1, 2]), row(10, [1, 2], { name: "Ex 10 v2" }), row(12, [1, 2], { name: "" })]), day(2, [row(11, [1, 2])])],
    });
    cells = resyncCells(APPENDED);
    act(() => rerender({ grid: APPENDED }));
    expect(document.activeElement).toBe(cells[tableCellDomKey(12, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 12, weekId: 1, field: "text", line: 0 });
  });

  it("a stale append intent expires instead of stealing focus on a later unrelated change", () => {
    let cells = mountCells(GRID);
    const onAppendRow = vi.fn(); // the POST fails silently: no row ever appears.
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g, onAppendRow }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 10, null, "name");
    act(() => {
      result.current.cellProps(10, null, "name", NOOP_CALLBACKS).onKeyDown(keyEvent("Enter", cells[tableCellDomKey(10, null, "name")]!));
    });
    expect(onAppendRow).toHaveBeenCalled();

    // Four unrelated grid identity changes exhaust the intent's ttl…
    for (let i = 0; i < 4; i++) {
      const NEXT = grid({ days: [day(1, [row(9, [1, 2], { name: `Ex 9 v${i}` }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
      cells = resyncCells(NEXT);
      act(() => rerender({ grid: NEXT }));
    }
    // …so a LATER append (someone clicking "+ Add exercise") doesn't get its
    // focus stolen to the tail of day 1.
    const LATER = grid({ days: [day(1, [row(9, [1, 2]), row(10, [1, 2]), row(12, [1, 2], { name: "" })]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(LATER);
    act(() => rerender({ grid: LATER }));
    // Normal tier-1 restoration keeps the anchor (and any restored focus) on
    // row 10's name — the expired intent never drags it to row 12.
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "name", line: 0 });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
  });
});

describe("undo/redo bypass + native keys", () => {
  it("Ctrl+Z / Cmd+Z / Shift+Ctrl+Z on a cell are NOT intercepted", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const callbacks = { onCommit: vi.fn(), onRevert: vi.fn() };
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { ctrlKey: true, shiftKey: true }]) {
      const event = keyEvent("z", cells[tableCellDomKey(9, null, "name")]!, extra);
      act(() => {
        result.current.cellProps(9, null, "name", callbacks).onKeyDown(event);
      });
      expect(event.preventDefault).not.toHaveBeenCalled();
    }
    expect(callbacks.onCommit).not.toHaveBeenCalled();
    expect(callbacks.onRevert).not.toHaveBeenCalled();
  });

  it.each(["Home", "End", "PageUp", "PageDown", "a"])("%s is not preventDefault'd", (key) => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent(key, cells[tableCellDomKey(9, 1, "text")]!);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("Shift+ArrowDown is not intercepted", () => {
    const cells = mountCells(GRID);
    cells[tableCellDomKey(9, 1, "text")]!.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text")]!, { shiftKey: true });
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text")]);
  });

  it("Ctrl+ArrowLeft at the caret boundary is not intercepted", () => {
    const cells = mountCells(GRID);
    const input = cells[tableCellDomKey(9, 1, "text")]!;
    input.value = "3 x 5";
    input.focus();
    input.setSelectionRange(0, 0);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", input, { ctrlKey: true });
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });
});

describe("focus restoration across a grid swap", () => {
  it("tier 1: re-focuses the same (rowId, weekId, field, line) when it survives the swap", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "text");

    const NEXT = grid({ days: [day(1, [row(9, [1, 2], { name: "Box Squat" }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 0 });
  });

  it("tier 2a: falls back to the first row of the same day (name column) when the row is gone but the day survives", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "text");

    // Day 1 survives (id 1) but row 9 is gone; row 10 remains.
    const NEXT = grid({ days: [day(1, [row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "name", line: 0 });
  });

  it("tier 2b: keeps the row+field but snaps to the first remaining week when the focused week is removed", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 2, "text");

    // Week 2 is removed; both rows survive on week 1 only.
    const NEXT = grid({ weeks: [week({ id: 1, label: "Wk 1" })], days: [day(1, [row(9, [1]), row(10, [1])]), day(2, [row(11, [1])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 0 });
  });

  it("post-tier guard: a surviving cell whose focused SUB-LINE vanished falls back to its line 0", () => {
    // Focus week 1's sub-line 1 (a real line, not the ghost), then swap in a
    // grid where that line is gone (an undo dropped it): row, week and cell
    // all survive, but (…, line 1) is now only the ghost's coordinate —
    // which still renders, so tier 1 holds. Drop the GHOST too (a skip
    // would) to force the line-0 fallback.
    const STACK_GRID = grid({
      days: [day(1, [row(9, [1, 2], { cells: { "1": cell({ prescription_id: 900, lines: [{ line: 1, text: "RPE 8" }, { line: 2, text: "cue" }] }), "2": cell({ prescription_id: 901 }) } }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])],
    });
    let cells = mountCells(STACK_GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: STACK_GRID },
    });
    focus(result.current, cells, 9, 1, "text", 2);

    // The stack shrank to nothing: stops are [0, 1(ghost)] now — line 2
    // renders nowhere, so restoration falls back to the cell's line 0.
    const SHRUNK = grid({
      days: [day(1, [row(9, [1, 2], { cells: { "1": cell({ prescription_id: 900 }), "2": cell({ prescription_id: 901 }) } }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])],
    });
    cells = resyncCells(SHRUNK);
    act(() => rerender({ grid: SHRUNK }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 0 });
  });

  it("post-tier guard: skids forward off a surviving-but-hollow coordinate (skip on the focused cell)", () => {
    // Skipping the FOCUSED cell hollows out its coordinate on the refetch:
    // row 9 and week 1 both survive, but row 9 renders nothing at week 1
    // anymore. Tier 1 alone would strand the anchor there — no rendered
    // cell would hold tabIndex=0 and the table would drop out of the tab
    // order. The guard skids to the nearest rendered column of the row
    // (forward first: week 2's text).
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "text");

    const NEXT = grid({ days: [day(1, [row(9, [2]), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 2, field: "text", line: 0 });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "text")]);
    expect(result.current.cellProps(9, 2, "text", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("post-tier guard: skids backward when everything after the hollow coordinate is holes too", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 2, "text");

    // Row 9 keeps only week 1 — the focused week-2 coordinate is gone, so
    // the guard scans forward (notes/rest always render) — landing on the
    // nearest rendered column after it: the notes column.
    const NEXT = grid({ days: [day(1, [row(9, [1]), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "note", line: 0 });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "note")]);
    expect(result.current.cellProps(9, null, "note", NOOP_CALLBACKS).tabIndex).toBe(0);
  });

  it("tier 3: falls back to the table's first cell when the day itself is gone", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 11, 1, "text"); // day 2's row

    // Day 2 (id 2) is gone entirely; only day 1 remains.
    const NEXT = grid({ days: [day(1, [row(9, [1, 2]), row(10, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "name", line: 0 });
  });

  it("tier 4: does nothing (no throw, no focus) when the whole table is emptied", () => {
    const cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, null, "name");

    const NEXT = grid({ days: [] });
    resyncCells(NEXT);
    expect(() => act(() => rerender({ grid: NEXT }))).not.toThrow();

    expect(result.current.anchor).toBe(null);
  });

  it("tier 4: a null grid behaves the same as an empty table (no throw, anchor null)", () => {
    mountCells(GRID);
    expect(() => renderHook(() => useTableNav({ grid: null }))).not.toThrow();
    const { result } = renderHook(() => useTableNav({ grid: null }));
    expect(result.current.anchor).toBe(null);
  });

  it("does NOT steal focus on a grid swap when the table was never focused", () => {
    let cells = mountCells(GRID);
    const { rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    const NEXT = grid({ days: [day(1, [row(9, [1, 2]), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(document.body);
    expect(cells[tableCellDomKey(9, null, "name")]).not.toBe(document.activeElement);
  });

  it("restoration never steals focus from a non-grid form field (e.g. the chat composer)", () => {
    let cells = mountCells(GRID);
    const composer = document.createElement("input");
    document.body.appendChild(composer);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "text");
    composer.focus();

    const NEXT = grid({ days: [day(1, [row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(composer);
  });

  it("an unmarked control keeps focus across a grid identity change", () => {
    let cells = mountCells(GRID);
    const toggle = document.createElement("button");
    document.body.appendChild(toggle);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "text");
    toggle.focus();

    const NEXT = grid({ days: [day(1, [row(9, [1, 2], { name: "Box Squat" }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(toggle);
  });

  it("a data-grid-restore control still hands focus back to the table", () => {
    let cells = mountCells(GRID);
    const undoButton = document.createElement("button");
    undoButton.setAttribute("data-grid-restore", "");
    document.body.appendChild(undoButton);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "text");
    undoButton.focus();

    const NEXT = grid({ days: [day(1, [row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
  });
});

describe("holes (missing DOM cells)", () => {
  it("arrowing onto a hole doesn't throw and doesn't move activeElement", () => {
    // Mount only row 9's tempo + week 1 cell — week 2 is a total hole (as
    // add-this-week rows and skipped-cell display leave gaps in the real
    // app; MesoTable's own <td/> with no GridCellEditor is the same shape),
    // and the notes/rest columns after it are unmounted too.
    const tempoInput = document.createElement("input");
    tempoInput.setAttribute("data-grid-cell", tableCellDomKey(9, null, "tempo"));
    document.body.appendChild(tempoInput);
    const textInput = document.createElement("input");
    textInput.value = "";
    textInput.setAttribute("data-grid-cell", tableCellDomKey(9, 1, "text"));
    document.body.appendChild(textInput);
    // Week 2's "text" cell (and everything after) is intentionally never mounted.

    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    textInput.focus();
    const event = keyEvent("ArrowRight", textInput);
    expect(() => {
      act(() => {
        result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
      });
    }).not.toThrow();
    expect(document.activeElement).toBe(textInput);
  });
});

// Issue #455 review nit: arrow handlers used to commit the anchor to the
// adjacent COORDINATE before checking whether it actually renders — landing
// on a hole left the old input with real DOM focus but tabIndex -1, and no
// cell anywhere holding tabIndex 0 (the grid drops out of the tab order).
// The fix scans past holes, at keydown time, to the first coordinate that
// DOES render — mountCells expresses a hole as a weekId left out of a row's
// `cells` map, exactly like MesoTable's `!cell` bare <td/> case. Phase 2b:
// the row-scoped notes/rest columns now sit AFTER the last week and always
// render, so "everything right of here is holes" needs the horizontal cases
// to compare against them, and pure row-extreme no-ops live on the rest
// column (see the arrow describe above).
describe("hole-skidding: arrows land on the next RENDERED cell, never a phantom coordinate", () => {
  it("(1) ArrowRight over a hole in the middle of a row skips the entire missing week and lands on the next rendered cell", () => {
    // Row 9 has cells for weeks 1 and 3 only — week 2 is a total hole.
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false }), week({ id: 3, label: "Wk 3", current: false })],
      days: [day(1, [row(9, [1, 3])])],
    });
    const cells = mountCells(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.setSelectionRange(textInput.value.length, textInput.value.length);
    const event = keyEvent("ArrowRight", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 3, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 3, field: "text", line: 0 });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant: the anchor addresses a real node holding tabIndex 0.
    expect(result.current.cellProps(9, 3, "text", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("(2) ArrowRight skids past a trailing hole week onto the notes column (always rendered)", () => {
    // Row 9 has a cell for week 1 only — week 2 is a total hole, but the
    // notes/rest columns after it always render, so the skid lands there.
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false })],
      days: [day(1, [row(9, [1])])],
    });
    const cells = mountCells(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const textInput = cells[tableCellDomKey(9, 1, "text")]!;
    textInput.focus();
    textInput.setSelectionRange(textInput.value.length, textInput.value.length);
    const event = keyEvent("ArrowRight", textInput);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "note")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "note", line: 0 });
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.cellProps(9, null, "note", NOOP_CALLBACKS).tabIndex).toBe(0);
  });

  it("(3) ArrowLeft mirrors (1): skips the entire missing week backwards, landing on the previous rendered cell", () => {
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false }), week({ id: 3, label: "Wk 3", current: false })],
      days: [day(1, [row(9, [1, 3])])],
    });
    const cells = mountCells(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const textInput = cells[tableCellDomKey(9, 3, "text")]!;
    textInput.setSelectionRange(0, 0);
    const event = keyEvent("ArrowLeft", textInput);
    act(() => {
      result.current.cellProps(9, 3, "text", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 0 });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant.
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 3, "text", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("(4) ArrowDown skips a row whose cell at (weekId, field) is unrendered, landing on the next row that has it", () => {
    // Row 10 (between 9 and 11) has no week-1 cell at all. From row 9's
    // GHOST (its last stop), down lands on row 11's line 0 — skipping every
    // stop row 10 doesn't render at this column.
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false })],
      days: [day(1, [row(9, [1, 2]), row(10, [2]), row(11, [1, 2])])],
    });
    const cells = mountCells(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "text", 1)]!);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(11, 1, "text")]);
    expect(result.current.anchor).toEqual({ rowId: 11, weekId: 1, field: "text", line: 0 });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant: row 10 (skipped over, never had this cell) never became
    // the anchor; row 11 (the real landing) holds the roving tabIndex 0.
    expect(result.current.cellProps(11, 1, "text", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "text", NOOP_CALLBACKS, 1).tabIndex).toBe(-1);
  });

  it("(5) invariant holds across a repeated skid in both horizontal directions: the anchor always addresses an existing, tabIndex-0 node", () => {
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false }), week({ id: 3, label: "Wk 3", current: false })],
      days: [day(1, [row(9, [1, 3])])],
    });
    const cells = mountCells(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));

    function assertAnchorIsReal() {
      const a = result.current.anchor;
      expect(a).not.toBeNull();
      if (!a) return;
      expect(document.querySelector(`[data-grid-cell="${tableCellDomKey(a.rowId, a.weekId, a.field, a.line)}"]`)).not.toBeNull();
      expect(result.current.cellProps(a.rowId, a.weekId, a.field, NOOP_CALLBACKS, a.line).tabIndex).toBe(0);
    }

    const w1Input = cells[tableCellDomKey(9, 1, "text")]!;
    w1Input.setSelectionRange(w1Input.value.length, w1Input.value.length);
    act(() => {
      result.current.cellProps(9, 1, "text", NOOP_CALLBACKS).onKeyDown(keyEvent("ArrowRight", w1Input));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 3, field: "text", line: 0 });
    assertAnchorIsReal();

    const w3Input = cells[tableCellDomKey(9, 3, "text")]!;
    w3Input.setSelectionRange(0, 0);
    act(() => {
      result.current.cellProps(9, 3, "text", NOOP_CALLBACKS).onKeyDown(keyEvent("ArrowLeft", w3Input));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "text", line: 0 });
    assertAnchorIsReal();
  });
});

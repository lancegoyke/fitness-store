// Specs for useTableNav (issue #455 phase A1) — the P1 multi-week table's
// roving-tabindex + keyboard-nav + focus-restoration hook. Mirrors
// useGridNav.test.tsx's structure/coverage, generalized from a 1D
// (prescriptionId, column) identity to a 2D (rowId, weekId, field) identity
// over a variable-width week axis (see useTableNav.ts's header for the
// sibling-not-shared rationale).
//
// Fixture: day 1 (session_slot_id 1) has rows 9, 10; day 2 (session_slot_id
// 2) has row 11 — flattened row order (day-major/row-minor): (9), (10),
// (11). Two weeks (id 1 "Wk 1", id 2 "Wk 2") so ArrowRight/Left week-
// crossing and tier 2b (a week removed, row survives) are exercisable
// without a second fixture.
import { act, renderHook } from "@testing-library/react";
import type { FocusEvent, KeyboardEvent } from "react";
import { useTableNav, tableCellDomKey, tableCellAriaLabel, TABLE_FIELDS } from "./useTableNav";
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
    sets: "3",
    reps: "5",
    load: "100",
    load_type: "abs",
    rpe: "8",
    rest: "90",
    note: "",
    skipped: false,
    swap_name: "",
    swap_exercise_id: null,
    swap_display: "",
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

/** Mounts one real `<input>` per (rowId, weekId, field) in `g`, so the
 * hook's `document.querySelector`-based focus moves have somewhere real to
 * land — mirrors what GridCellEditor/RowNameEditor would render, without
 * needing a full React render of the table tree. */
function mountCells(g: MesoGrid) {
  const nodes: Record<string, HTMLInputElement> = {};
  for (const d of g.days) {
    for (const r of d.rows) {
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.value = r.name;
      nameInput.setAttribute("data-grid-cell", tableCellDomKey(r.exercise_slot_id, null, "name"));
      document.body.appendChild(nameInput);
      nodes[tableCellDomKey(r.exercise_slot_id, null, "name")] = nameInput;

      for (const w of g.weeks) {
        const c = r.cells[String(w.id)];
        for (const field of TABLE_FIELDS) {
          const input = document.createElement("input");
          input.type = "text";
          input.value = c ? String((c as unknown as Record<string, unknown>)[field] ?? "") : "";
          input.setAttribute("data-grid-cell", tableCellDomKey(r.exercise_slot_id, w.id, field));
          document.body.appendChild(input);
          nodes[tableCellDomKey(r.exercise_slot_id, w.id, field)] = input;
        }
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

/** Like mountCells, but a row's OWN `cells` map decides what actually
 * mounts: a week id missing from `row.cells` gets no `data-grid-cell` node
 * for any of its fields — the same "no rendered input at all" shape
 * MesoTable produces for a hole (an add-this-week row's bare `<td/>`, no
 * GridCellEditor) or a skipped cell (em-dash + Unskip button, no
 * GridCellEditor). Fixtures express holes just by leaving a weekId out of
 * `row(id, weekIds)`. The row-name column always mounts — MesoTable renders
 * RowNameEditor unconditionally, independent of any week's holes. */
function mountCellsWithHoles(g: MesoGrid) {
  const nodes: Record<string, HTMLInputElement> = {};
  for (const d of g.days) {
    for (const r of d.rows) {
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.value = r.name;
      nameInput.setAttribute("data-grid-cell", tableCellDomKey(r.exercise_slot_id, null, "name"));
      document.body.appendChild(nameInput);
      nodes[tableCellDomKey(r.exercise_slot_id, null, "name")] = nameInput;

      for (const w of g.weeks) {
        const c = r.cells[String(w.id)];
        if (!c) continue; // hole: no cell for this row this week, no inputs at all.
        for (const field of TABLE_FIELDS) {
          const input = document.createElement("input");
          input.type = "text";
          input.value = String((c as unknown as Record<string, unknown>)[field] ?? "");
          input.setAttribute("data-grid-cell", tableCellDomKey(r.exercise_slot_id, w.id, field));
          document.body.appendChild(input);
          nodes[tableCellDomKey(r.exercise_slot_id, w.id, field)] = input;
        }
      }
    }
  }
  return nodes;
}

function keyEvent(
  key: string,
  target: HTMLInputElement,
  extra: Partial<{ ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }> = {},
): KeyboardEvent<HTMLInputElement> {
  return {
    key,
    currentTarget: target,
    target,
    preventDefault: vi.fn(),
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
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
) {
  act(() => {
    result.cellProps(rowId, weekId, field, NOOP_CALLBACKS).onFocus(focusEvent(cells[tableCellDomKey(rowId, weekId, field)]!));
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
      expect(tableCellDomKey(9, 2, "sets")).toBe("9:2:sets");
    });

    it("uses the 'row' sentinel for a null weekId (the name column)", () => {
      expect(tableCellDomKey(9, null, "name")).toBe("9:row:name");
    });
  });

  describe("tableCellAriaLabel", () => {
    it("builds '<name> — <week label> — <field label>' for a week field", () => {
      expect(tableCellAriaLabel("Box Squat", "Wk 2", "sets")).toBe("Box Squat — Wk 2 — sets");
      expect(tableCellAriaLabel("Box Squat", "Wk 2", "rpe")).toBe("Box Squat — Wk 2 — RPE");
    });

    it("labels the name column as '<name> — exercise name' regardless of weekLabel", () => {
      expect(tableCellAriaLabel("Box Squat", null, "name")).toBe("Box Squat — exercise name");
    });

    it("falls back to 'exercise' when the row has no name yet", () => {
      expect(tableCellAriaLabel("", "Wk 1", "sets")).toBe("exercise — Wk 1 — sets");
    });
  });
});

describe("anchor + roving tabindex", () => {
  it("starts anchored on the first row's name column", () => {
    mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "name" });
  });

  it("exactly one cell is tabbable (0) initially — every other cell is -1", () => {
    mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    expect(result.current.cellProps(9, null, "name", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(10, null, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(11, 2, "note", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("focusing another cell moves the roving 0 to it", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    act(() => {
      result.current.cellProps(10, 2, "load", NOOP_CALLBACKS).onFocus(focusEvent(cells[tableCellDomKey(10, 2, "load")]!));
    });
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: 2, field: "load" });
    expect(result.current.cellProps(10, 2, "load", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, null, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("cell identity is (rowId, weekId, field), never an index — the same field on a different row/week is a different cell", () => {
    mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    expect(result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(9, 2, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(10, 1, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });
});

describe("ArrowDown / ArrowUp", () => {
  it("ArrowDown moves focus to the same (weekId, field) on the next row within a day", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "sets")]!);
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, 1, "sets")]);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: 1, field: "sets" });
  });

  it("ArrowDown crosses a day-table boundary (last row of day 1 -> first row of day 2)", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(10, 2, "load")]!);
    act(() => {
      result.current.cellProps(10, 2, "load", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(11, 2, "load")]);
  });

  it("ArrowDown at the very last row is a no-op but still preventDefault", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(11, 2, "note")]!);
    act(() => {
      result.current.cellProps(11, 2, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ rowId: 11, weekId: 2, field: "note" });
  });

  it("ArrowUp mirrors ArrowDown, crossing day boundaries upward", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowUp", cells[tableCellDomKey(11, 1, "rpe")]!);
    act(() => {
      result.current.cellProps(11, 1, "rpe", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, 1, "rpe")]);
    expect(event.preventDefault).toHaveBeenCalled();
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

  it("the name column moves vertically too, keeping weekId null", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, null, "name")]!);
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "name" });
  });
});

describe("ArrowRight / ArrowLeft (caret-conditional)", () => {
  it("ArrowRight at the end of the text moves to the next field within the same week", () => {
    const cells = mountCells(GRID);
    const setsInput = cells[tableCellDomKey(9, 1, "sets")]!;
    setsInput.value = "42";
    setsInput.setSelectionRange(2, 2); // caret at end (value.length === 2)
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", setsInput);
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "reps")]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("ArrowLeft at the start of the text moves to the previous field within the same week", () => {
    const cells = mountCells(GRID);
    const repsInput = cells[tableCellDomKey(9, 1, "reps")]!;
    repsInput.value = "5";
    repsInput.setSelectionRange(0, 0); // caret at start
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", repsInput);
    act(() => {
      result.current.cellProps(9, 1, "reps", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "sets")]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("ArrowRight at the last field of week 1 crosses into week 2's first field (no special-case)", () => {
    const cells = mountCells(GRID);
    const noteInput = cells[tableCellDomKey(9, 1, "note")]!;
    noteInput.value = "";
    noteInput.setSelectionRange(0, 0);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", noteInput);
    act(() => {
      result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "sets")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 2, field: "sets" });
  });

  it("ArrowLeft at the first field of week 2 crosses back into week 1's last field", () => {
    const cells = mountCells(GRID);
    const setsInput = cells[tableCellDomKey(9, 2, "sets")]!;
    setsInput.value = "3";
    setsInput.setSelectionRange(0, 0);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", setsInput);
    act(() => {
      result.current.cellProps(9, 2, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "note")]);
  });

  it("ArrowRight at the end of the name column moves to week 1's sets field", () => {
    const cells = mountCells(GRID);
    const nameInput = cells[tableCellDomKey(9, null, "name")]!;
    nameInput.value = "Ex 9";
    nameInput.setSelectionRange(4, 4);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", nameInput);
    act(() => {
      result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "sets")]);
  });

  it("ArrowLeft at the start of week 1's sets field moves back to the name column", () => {
    const cells = mountCells(GRID);
    const setsInput = cells[tableCellDomKey(9, 1, "sets")]!;
    setsInput.value = "3";
    setsInput.setSelectionRange(0, 0);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", setsInput);
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "name")]);
  });

  it("ArrowRight mid-text does NOT move focus or preventDefault (native caret move wins)", () => {
    const cells = mountCells(GRID);
    const setsInput = cells[tableCellDomKey(9, 1, "sets")]!;
    setsInput.value = "4200";
    setsInput.setSelectionRange(2, 2); // caret in the middle
    setsInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", setsInput);
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(setsInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("ArrowLeft with a non-collapsed selection does NOT move focus", () => {
    const cells = mountCells(GRID);
    const repsInput = cells[tableCellDomKey(9, 1, "reps")]!;
    repsInput.value = "5";
    repsInput.setSelectionRange(0, 1); // a selection, not a collapsed caret
    repsInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", repsInput);
    act(() => {
      result.current.cellProps(9, 1, "reps", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(repsInput);
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

  it("no wrap at an absolute extreme: ArrowRight at the end of the very last column (note, week 2, row 9) is a pure no-op", () => {
    const cells = mountCells(GRID);
    const noteInput = cells[tableCellDomKey(9, 2, "note")]!;
    noteInput.value = "";
    noteInput.setSelectionRange(0, 0);
    noteInput.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowRight", noteInput);
    act(() => {
      result.current.cellProps(9, 2, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(noteInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});

describe("Enter (commit) / Escape (revert)", () => {
  it("Enter calls the cell's onCommit, preventDefaults, and does not move focus", () => {
    const cells = mountCells(GRID);
    cells[tableCellDomKey(9, 1, "load")]!.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onCommit = vi.fn();
    const event = keyEvent("Enter", cells[tableCellDomKey(9, 1, "load")]!);
    act(() => {
      result.current.cellProps(9, 1, "load", { ...NOOP_CALLBACKS, onCommit }).onKeyDown(event);
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "load")]);
  });

  it("Escape reverts to the focus-time value via onRevert, preventDefaults, and keeps focus", () => {
    const cells = mountCells(GRID);
    const loadInput = cells[tableCellDomKey(9, 1, "load")]!;
    loadInput.value = "100";
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onRevert = vi.fn();
    const callbacks = { ...NOOP_CALLBACKS, onRevert };
    loadInput.focus();
    act(() => {
      result.current.cellProps(9, 1, "load", callbacks).onFocus(focusEvent(loadInput));
    });
    loadInput.value = "999";
    const event = keyEvent("Escape", loadInput);
    act(() => {
      result.current.cellProps(9, 1, "load", callbacks).onKeyDown(event);
    });
    expect(onRevert).toHaveBeenCalledWith("100");
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(loadInput);
  });

  it("Escape without a prior focus call reverts to the DOM value at keydown time", () => {
    const cells = mountCells(GRID);
    const rpeInput = cells[tableCellDomKey(9, 1, "rpe")]!;
    rpeInput.value = "8";
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onRevert = vi.fn();
    const event = keyEvent("Escape", rpeInput);
    act(() => {
      result.current.cellProps(9, 1, "rpe", { ...NOOP_CALLBACKS, onRevert }).onKeyDown(event);
    });
    expect(onRevert).toHaveBeenCalledWith("8");
  });

  it("Enter resets the Escape baseline to the committed value", () => {
    const cells = mountCells(GRID);
    const loadInput = cells[tableCellDomKey(9, 1, "load")]!;
    loadInput.value = "100";
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const onCommit = vi.fn();
    const onRevert = vi.fn();
    const callbacks = { ...NOOP_CALLBACKS, onCommit, onRevert };
    loadInput.focus();
    act(() => {
      result.current.cellProps(9, 1, "load", callbacks).onFocus(focusEvent(loadInput));
    });
    loadInput.value = "150";
    act(() => {
      result.current.cellProps(9, 1, "load", callbacks).onKeyDown(keyEvent("Enter", loadInput));
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    loadInput.value = "175";
    act(() => {
      result.current.cellProps(9, 1, "load", callbacks).onKeyDown(keyEvent("Escape", loadInput));
    });
    expect(onRevert).toHaveBeenCalledWith("150");
  });
});

describe("undo/redo bypass + native keys", () => {
  it("Ctrl+Z / Cmd+Z / Shift+Ctrl+Z on a cell are NOT intercepted", () => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { ctrlKey: true, shiftKey: true }]) {
      const event = keyEvent("z", cells[tableCellDomKey(9, null, "name")]!, extra);
      act(() => {
        result.current.cellProps(9, null, "name", NOOP_CALLBACKS).onKeyDown(event);
      });
      expect(event.preventDefault).not.toHaveBeenCalled();
    }
    expect(NOOP_CALLBACKS.onCommit).not.toHaveBeenCalled();
    expect(NOOP_CALLBACKS.onRevert).not.toHaveBeenCalled();
  });

  it.each(["Home", "End", "PageUp", "PageDown", "a", "Tab"])("%s is not preventDefault'd", (key) => {
    const cells = mountCells(GRID);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent(key, cells[tableCellDomKey(9, 1, "sets")]!);
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("Shift+ArrowDown is not intercepted", () => {
    const cells = mountCells(GRID);
    cells[tableCellDomKey(9, 1, "sets")]!.focus();
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "sets")]!, { shiftKey: true });
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "sets")]);
  });

  it("Ctrl+ArrowLeft at the caret boundary is not intercepted", () => {
    const cells = mountCells(GRID);
    const input = cells[tableCellDomKey(9, 1, "sets")]!;
    input.value = "3";
    input.focus();
    input.setSelectionRange(0, 0);
    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    const event = keyEvent("ArrowLeft", input, { ctrlKey: true });
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });
});

describe("focus restoration across a grid swap", () => {
  it("tier 1: re-focuses the same (rowId, weekId, field) when it survives the swap", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "sets");

    const NEXT = grid({ days: [day(1, [row(9, [1, 2], { name: "Box Squat" }), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "sets")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "sets" });
  });

  it("tier 2a: falls back to the first row of the same day (name column) when the row is gone but the day survives", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "sets");

    // Day 1 survives (id 1) but row 9 is gone; row 10 remains.
    const NEXT = grid({ days: [day(1, [row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 10, weekId: null, field: "name" });
  });

  it("tier 2b: keeps the row+field but snaps to the first remaining week when the focused week is removed", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 2, "sets");

    // Week 2 is removed; both rows survive on week 1 only.
    const NEXT = grid({ weeks: [week({ id: 1, label: "Wk 1" })], days: [day(1, [row(9, [1]), row(10, [1])]), day(2, [row(11, [1])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "sets")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "sets" });
  });

  it("post-tier guard: skids forward off a surviving-but-hollow coordinate (skip on the focused cell)", () => {
    // Skipping the FOCUSED cell hollows out its coordinate on the refetch:
    // row 9 and week 1 both survive, but row 9 renders nothing at week 1
    // anymore. Tier 1 alone would strand the anchor there — no rendered
    // cell would hold tabIndex=0 and the table would drop out of the tab
    // order. The guard skids to the nearest rendered column of the row
    // (forward first: week 2's sets).
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 1, "sets");

    const NEXT = grid({ days: [day(1, [row(9, [2]), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    document.querySelectorAll("[data-grid-cell]").forEach((n) => n.remove());
    cells = mountCellsWithHoles(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 2, field: "sets" });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 2, "sets")]);
    expect(result.current.cellProps(9, 2, "sets", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("post-tier guard: skids backward when everything after the hollow coordinate is holes too", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 9, 2, "sets");

    // Row 9 keeps only week 1 — the focused week-2 coordinate and every
    // column after it are gone, so the guard scans backward and lands on
    // week 1's trailing note field (the nearest rendered column).
    const NEXT = grid({ days: [day(1, [row(9, [1]), row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    document.querySelectorAll("[data-grid-cell]").forEach((n) => n.remove());
    cells = mountCellsWithHoles(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "note" });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "note")]);
    expect(result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).tabIndex).toBe(0);
  });

  it("tier 3: falls back to the table's first cell when the day itself is gone", () => {
    let cells = mountCells(GRID);
    const { result, rerender } = renderHook(({ grid: g }) => useTableNav({ grid: g }), {
      initialProps: { grid: GRID },
    });
    focus(result.current, cells, 11, 1, "sets"); // day 2's row

    // Day 2 (id 2) is gone entirely; only day 1 remains.
    const NEXT = grid({ days: [day(1, [row(9, [1, 2]), row(10, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(9, null, "name")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: null, field: "name" });
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
    focus(result.current, cells, 9, 1, "sets");
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
    focus(result.current, cells, 9, 1, "sets");
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
    focus(result.current, cells, 9, 1, "sets");
    undoButton.focus();

    const NEXT = grid({ days: [day(1, [row(10, [1, 2])]), day(2, [row(11, [1, 2])])] });
    cells = resyncCells(NEXT);
    act(() => rerender({ grid: NEXT }));

    expect(document.activeElement).toBe(cells[tableCellDomKey(10, null, "name")]);
  });
});

describe("holes (missing DOM cells)", () => {
  it("arrowing onto a hole doesn't throw and doesn't move activeElement", () => {
    // Mount only row 9's name + week 1 cells — week 2 is a total hole (as
    // add-this-week rows and skipped-cell display leave gaps in the real
    // app; MesoTable's own <td/> with no GridCellEditor is the same shape).
    const nameInput = document.createElement("input");
    nameInput.setAttribute("data-grid-cell", tableCellDomKey(9, null, "name"));
    document.body.appendChild(nameInput);
    const noteInput = document.createElement("input");
    noteInput.value = "";
    noteInput.setAttribute("data-grid-cell", tableCellDomKey(9, 1, "note"));
    document.body.appendChild(noteInput);
    // Week 2's "sets" cell is intentionally never mounted.

    const { result } = renderHook(() => useTableNav({ grid: GRID }));
    noteInput.focus();
    const event = keyEvent("ArrowRight", noteInput);
    expect(() => {
      act(() => {
        result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).onKeyDown(event);
      });
    }).not.toThrow();
    expect(document.activeElement).toBe(noteInput);
  });
});

// Issue #455 review nit: arrow handlers used to commit the anchor to the
// adjacent COORDINATE before checking whether it actually renders — landing
// on a hole left the old input with real DOM focus but tabIndex -1, and no
// cell anywhere holding tabIndex 0 (the grid drops out of the tab order).
// The fix scans past holes, at keydown time, to the first coordinate that
// DOES render — using mountCellsWithHoles so a fixture's holes are just a
// row's own `cells` map, exactly like MesoTable's `!cell` bare <td/> case.
describe("hole-skidding: arrows land on the next RENDERED cell, never a phantom coordinate", () => {
  it("(1) ArrowRight over a hole in the middle of a row skips the entire missing week and lands on the next rendered cell", () => {
    // Row 9 has cells for weeks 1 and 3 only — week 2 is a total hole.
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false }), week({ id: 3, label: "Wk 3", current: false })],
      days: [day(1, [row(9, [1, 3])])],
    });
    const cells = mountCellsWithHoles(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const noteInput = cells[tableCellDomKey(9, 1, "note")]!;
    noteInput.setSelectionRange(0, 0);
    const event = keyEvent("ArrowRight", noteInput);
    act(() => {
      result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 3, "sets")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 3, field: "sets" });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant: the anchor addresses a real node holding tabIndex 0.
    expect(result.current.cellProps(9, 3, "sets", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("(2) ArrowRight when everything to the right is holes leaves the anchor/focus unchanged, but still preventDefaults", () => {
    // Row 9 has a cell for week 1 only — week 2 (the only thing to its
    // right) is a total hole all the way to the table's edge.
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false })],
      days: [day(1, [row(9, [1])])],
    });
    const cells = mountCellsWithHoles(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const noteInput = cells[tableCellDomKey(9, 1, "note")]!;
    noteInput.focus();
    noteInput.setSelectionRange(0, 0);
    const event = keyEvent("ArrowRight", noteInput);
    act(() => {
      result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(noteInput);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "note" });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant: the anchor still addresses the real node it started at.
    expect(result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).tabIndex).toBe(0);
  });

  it("(3) ArrowLeft mirrors (1): skips the entire missing week backwards, landing on the previous rendered cell", () => {
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false }), week({ id: 3, label: "Wk 3", current: false })],
      days: [day(1, [row(9, [1, 3])])],
    });
    const cells = mountCellsWithHoles(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const setsInput = cells[tableCellDomKey(9, 3, "sets")]!;
    setsInput.setSelectionRange(0, 0);
    const event = keyEvent("ArrowLeft", setsInput);
    act(() => {
      result.current.cellProps(9, 3, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(9, 1, "note")]);
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "note" });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant.
    expect(result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 3, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("(4) ArrowDown skips a row whose cell at (weekId, field) is unrendered, landing on the next row that has it", () => {
    // Row 10 (between 9 and 11) has no week-1 cell at all.
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false })],
      days: [day(1, [row(9, [1, 2]), row(10, [2]), row(11, [1, 2])])],
    });
    const cells = mountCellsWithHoles(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));
    const event = keyEvent("ArrowDown", cells[tableCellDomKey(9, 1, "sets")]!);
    act(() => {
      result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells[tableCellDomKey(11, 1, "sets")]);
    expect(result.current.anchor).toEqual({ rowId: 11, weekId: 1, field: "sets" });
    expect(event.preventDefault).toHaveBeenCalled();
    // (5) invariant: row 10 (skipped over, never had this cell) never became
    // the anchor; row 11 (the real landing) holds the roving tabIndex 0.
    expect(result.current.cellProps(11, 1, "sets", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, 1, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("(5) invariant holds across a repeated skid in both horizontal directions: the anchor always addresses an existing, tabIndex-0 node", () => {
    const HOLE_GRID = grid({
      weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", current: false }), week({ id: 3, label: "Wk 3", current: false })],
      days: [day(1, [row(9, [1, 3])])],
    });
    const cells = mountCellsWithHoles(HOLE_GRID);
    const { result } = renderHook(() => useTableNav({ grid: HOLE_GRID }));

    function assertAnchorIsReal() {
      const a = result.current.anchor;
      expect(a).not.toBeNull();
      if (!a) return;
      expect(document.querySelector(`[data-grid-cell="${tableCellDomKey(a.rowId, a.weekId, a.field)}"]`)).not.toBeNull();
      expect(result.current.cellProps(a.rowId, a.weekId, a.field, NOOP_CALLBACKS).tabIndex).toBe(0);
    }

    const noteInput = cells[tableCellDomKey(9, 1, "note")]!;
    noteInput.setSelectionRange(0, 0);
    act(() => {
      result.current.cellProps(9, 1, "note", NOOP_CALLBACKS).onKeyDown(keyEvent("ArrowRight", noteInput));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 3, field: "sets" });
    assertAnchorIsReal();

    const setsInput = cells[tableCellDomKey(9, 3, "sets")]!;
    setsInput.setSelectionRange(0, 0);
    act(() => {
      result.current.cellProps(9, 3, "sets", NOOP_CALLBACKS).onKeyDown(keyEvent("ArrowLeft", setsInput));
    });
    expect(result.current.anchor).toEqual({ rowId: 9, weekId: 1, field: "note" });
    assertAnchorIsReal();
  });
});

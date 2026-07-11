// Specs for useTableReorder (issue #455 phase A2) — a pure, stateless
// drag-event -> verb-call translator for MesoTable's row/day reordering.
// Unlike useReorder (WeekGrid's Phase-4 hook), this hook owns NO state and
// makes NO fetch of its own: onDragEnd's ONLY job is to read `grid` (passed
// in whole, mirroring useReorder's `program`) and the hand-built dnd-kit-
// shaped drop event, then call one of the two injected verbs
// (`reorderRow`/`reorderDay`, which DesignerRoot wires to useGrid's own
// `reorderExercises`/`reorderDays` — already awaiting their POST behind
// useGrid's shared busyRef guard) with the new id order. No fetch mocking
// needed here — see useGrid.test.ts for the verbs' own POST/refetch specs.
//
// === Contract this file pins ===
// - Drag identity = exercise_slot_id / session_slot_id (P0 block-wide
//   identity), never prescription_id — a cell's *content* identity is
//   always resolved fresh from `grid` at drop time (never carried on the
//   event), mirroring useReorder's dayId/prescriptionId split.
// - Row order payload = the CURRENT week's (`grid.weeks.find(w =>
//   w.current)`) live row cells' prescription_ids, in the new order — a row
//   with no cell for that week (an add-this-week-only hole) is excluded,
//   mirroring the server's own `session.cells()` query for that week
//   (views.py `session_reorder`).
// - Day order payload = the CURRENT week's live Session pks (GridDay.
//   session_id), in the new order (views.py `week_reorder_sessions`) — a
//   defensive no-op if ANY live day's session_id is null.
// - Cross-day row moves are OUT of scope for A2 (own follow-up) — enforced
//   here as a second, independent guard even though MesoTable's own
//   collision filter (filterTableDragCandidates) should already keep a row
//   drag from colliding with another day's rows in the first place.
import { renderHook } from "@testing-library/react";
import { useTableReorder } from "./useTableReorder";
import type { TableDragEndEvent } from "./useTableReorder";
import type { GridCell, GridDay, GridRow, GridWeek, MesoGrid } from "../lib/api";
import type { Id } from "./useGrid";

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

function row(overrides: Partial<GridRow> = {}): GridRow {
  return {
    exercise_slot_id: 9,
    name: "Squat",
    exercise_id: 55,
    order: 0,
    tags: [],
    cells: { "1": cell() },
    ...overrides,
  };
}

function day(overrides: Partial<GridDay> = {}): GridDay {
  return {
    session_slot_id: 1,
    session_id: 11,
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

/** Builds a dnd-kit-shaped row drag event — see useTableReorder.ts's
 * `TableDragData` union. `overDaySlotId` defaults to the active row's own
 * day (the common within-day case); pass a different one to build a
 * cross-day drop for the scope-guard spec. */
function rowDragEvent(
  activeExerciseSlotId: Id,
  activeDaySlotId: Id,
  overExerciseSlotId: Id | null,
  overDaySlotId: Id = activeDaySlotId,
): TableDragEndEvent {
  return {
    active: {
      id: activeExerciseSlotId,
      data: { current: { type: "row", daySlotId: activeDaySlotId, exerciseSlotId: activeExerciseSlotId } },
    },
    over:
      overExerciseSlotId == null
        ? null
        : {
            id: overExerciseSlotId,
            data: { current: { type: "row", daySlotId: overDaySlotId, exerciseSlotId: overExerciseSlotId } },
          },
  };
}

/** Builds a dnd-kit-shaped day-strip drag event. */
function dayDragEvent(activeSessionSlotId: Id, overSessionSlotId: Id | null): TableDragEndEvent {
  return {
    active: { id: activeSessionSlotId, data: { current: { type: "day", sessionSlotId: activeSessionSlotId } } },
    over:
      overSessionSlotId == null
        ? null
        : { id: overSessionSlotId, data: { current: { type: "day", sessionSlotId: overSessionSlotId } } },
  };
}

function setup(g: MesoGrid | null) {
  const reorderRow = vi.fn();
  const reorderDay = vi.fn();
  const { result } = renderHook(() => useTableReorder({ grid: g, reorderRow, reorderDay }));
  return { onDragEnd: result.current.onDragEnd, reorderRow, reorderDay };
}

describe("within-day row reorder", () => {
  it("calls reorderRow with the day's session_id and the current week's cell ids in the new order", () => {
    const g = grid({
      days: [
        day({
          session_slot_id: 1,
          session_id: 11,
          rows: [
            row({ exercise_slot_id: 9, cells: { "1": cell({ prescription_id: 900 }) } }),
            row({ exercise_slot_id: 10, cells: { "1": cell({ prescription_id: 1000 }) } }),
          ],
        }),
      ],
    });
    const { onDragEnd, reorderRow } = setup(g);
    onDragEnd(rowDragEvent(9, 1, 10));
    expect(reorderRow).toHaveBeenCalledWith(11, [1000, 900]);
  });

  it("arrayMove semantics: a 3-row day, moving row1 past row2, orders as [row2, row1, row3]", () => {
    const g = grid({
      days: [
        day({
          session_slot_id: 1,
          session_id: 11,
          rows: [
            row({ exercise_slot_id: 9, cells: { "1": cell({ prescription_id: 900 }) } }),
            row({ exercise_slot_id: 10, cells: { "1": cell({ prescription_id: 1000 }) } }),
            row({ exercise_slot_id: 12, cells: { "1": cell({ prescription_id: 1200 }) } }),
          ],
        }),
      ],
    });
    const { onDragEnd, reorderRow } = setup(g);
    onDragEnd(rowDragEvent(9, 1, 10));
    expect(reorderRow).toHaveBeenCalledWith(11, [1000, 900, 1200]);
  });

  it("excludes a hole row (no cell for the current week) from the posted order array", () => {
    const g = grid({
      days: [
        day({
          session_slot_id: 1,
          session_id: 11,
          rows: [
            row({ exercise_slot_id: 9, cells: { "1": cell({ prescription_id: 900 }) } }),
            row({ exercise_slot_id: 10, cells: {} }), // add-this-week-only hole: no cell for week 1
            row({ exercise_slot_id: 12, cells: { "1": cell({ prescription_id: 1200 }) } }),
          ],
        }),
      ],
    });
    const { onDragEnd, reorderRow } = setup(g);
    onDragEnd(rowDragEvent(9, 1, 12));
    expect(reorderRow).toHaveBeenCalledWith(11, [1200, 900]);
  });
});

describe("day reorder", () => {
  it("calls reorderDay with the current week's id and the block's session ids in the new order", () => {
    const g = grid({
      weeks: [week({ id: 1, current: true })],
      days: [day({ session_slot_id: 1, session_id: 11 }), day({ session_slot_id: 2, session_id: 22 })],
    });
    const { onDragEnd, reorderDay } = setup(g);
    onDragEnd(dayDragEvent(1, 2));
    expect(reorderDay).toHaveBeenCalledWith(1, [22, 11]);
  });

  it("no-ops when any live day's session_id is null", () => {
    const g = grid({
      days: [day({ session_slot_id: 1, session_id: 11 }), day({ session_slot_id: 2, session_id: null })],
    });
    const { onDragEnd, reorderDay } = setup(g);
    onDragEnd(dayDragEvent(1, 2));
    expect(reorderDay).not.toHaveBeenCalled();
  });
});

describe("cross-day scope guard", () => {
  it("no-ops a row dropped over a row belonging to a DIFFERENT day (A2 defers cross-day moves)", () => {
    const g = grid({
      days: [
        day({ session_slot_id: 1, session_id: 11, rows: [row({ exercise_slot_id: 9 })] }),
        day({
          session_slot_id: 2,
          session_id: 22,
          rows: [row({ exercise_slot_id: 20, cells: { "1": cell({ prescription_id: 2000 }) } })],
        }),
      ],
    });
    const { onDragEnd, reorderRow } = setup(g);
    onDragEnd(rowDragEvent(9, 1, 20, 2));
    expect(reorderRow).not.toHaveBeenCalled();
  });
});

describe("no-op drops", () => {
  it("does nothing when over is null", () => {
    const { onDragEnd, reorderRow, reorderDay } = setup(grid());
    onDragEnd(rowDragEvent(9, 1, null));
    expect(reorderRow).not.toHaveBeenCalled();
    expect(reorderDay).not.toHaveBeenCalled();
  });

  it("does nothing when active and over are the same item", () => {
    const { onDragEnd, reorderRow } = setup(grid());
    onDragEnd(rowDragEvent(9, 1, 9));
    expect(reorderRow).not.toHaveBeenCalled();
  });

  it("does nothing when there is no current week", () => {
    const g = grid({
      weeks: [week({ id: 1, current: false })],
      days: [
        day({
          session_slot_id: 1,
          session_id: 11,
          rows: [
            row({ exercise_slot_id: 9, cells: { "1": cell({ prescription_id: 900 }) } }),
            row({ exercise_slot_id: 10, cells: { "1": cell({ prescription_id: 1000 }) } }),
          ],
        }),
      ],
    });
    const { onDragEnd, reorderRow } = setup(g);
    onDragEnd(rowDragEvent(9, 1, 10));
    expect(reorderRow).not.toHaveBeenCalled();
  });

  it("does nothing when grid is null", () => {
    const { onDragEnd, reorderRow, reorderDay } = setup(null);
    onDragEnd(rowDragEvent(9, 1, 10));
    onDragEnd(dayDragEvent(1, 2));
    expect(reorderRow).not.toHaveBeenCalled();
    expect(reorderDay).not.toHaveBeenCalled();
  });
});

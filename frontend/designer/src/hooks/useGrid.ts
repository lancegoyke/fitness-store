// useGrid — self-contained state-owning hook for the P1 multi-week table
// (MesoTable). Owns `grid`/`history` and every verb that mutates it. Issue
// #455 phase A5 retired the sibling one-week `usePlanData` owner (and
// everything that only existed to feed it) — this hook is now DesignerRoot's
// SOLE data owner island-wide, not just the table's: `grid.plan`/
// `.athlete`/`.phases` (added in A5 step 1) feed TopBar/LeftRail/BlockView/
// AthletePreview too, via the pure `lib/grid.ts` helpers `gridToProgram`/
// `cycleLabelFromGrid`.
//
// Cell edits (patchCell/renameExercise) are optimistic + fire-and-forget,
// mirroring useAutosave's semantics (CONTRACT.md "useAutosave") — updated in
// local state immediately, POSTed without being awaited by the caller, and
// NOT rolled back on failure (only console.error'd), same as persistRow. Each
// in-flight autosave POST is tracked in `pendingWritesRef` so fillAcrossWeeks
// can flush (await) them before it fills — the fill endpoint copies the
// source cell's already-committed DB values, so an in-flight edit must land
// first or the fill can copy stale data (Codex P2).
//
// Structural verbs (add/remove day|week|exercise, undo/redo)
// await their POST, then call refetchGrid() (a plain GET, mirroring
// usePlanData's switchWeek) to re-sync the whole grid — mirroring
// usePlanData/useReorder's ref-guard idiom, one shared in-flight guard across
// every structural verb so a double-click can't race two refetches.
import { useCallback, useRef, useState } from "react";
import { apiPost } from "../lib/api";
import type { GridCell, GridDay, GridHistory, GridRow, GridWeek, MesoGrid } from "../lib/api";

export type Id = number | string;

/** The one cell field the coach types into (text-first, Phase 2a). Everything
 * else on GridCell — prescription_id/skipped/lines — is server-derived or
 * written through its own verb (writeCellLine), never this patch. */
export type GridCellPatch = Partial<Pick<GridCell, "text">>;

/** The per-exercise row columns (Phase 2a, D2) writable via patchRowColumns. */
export type GridRowPatch = Partial<Pick<GridRow, "tempo" | "rest" | "note">>;

/** Any payload carrying a fresh plan history — tolerant of `undo_label`/
 * `redo_label` arriving as `string | null` (the `serialize_plan_history`
 * convention) as well as `GridHistory`'s always-string labels. Coerced to
 * `GridHistory` on adoption below, so either convention lands cleanly. */
interface GridHistoryCarrier {
  history?: {
    can_undo: boolean;
    can_redo: boolean;
    undo_label: string | null;
    redo_label: string | null;
  };
}

const EMPTY_GRID_HISTORY: GridHistory = {
  can_undo: false,
  can_redo: false,
  undo_label: "",
  redo_label: "",
};

export interface UseGridOptions {
  planId: Id;
  csrf: string;
  initialGrid: MesoGrid | null;
}

function findRow(grid: MesoGrid | null, exerciseSlotId: Id): GridRow | undefined {
  if (!grid) return undefined;
  for (const day of grid.days) {
    const row = day.rows.find((r) => r.exercise_slot_id === exerciseSlotId);
    if (row) return row;
  }
  return undefined;
}

/** The row's FIRST live week's cell — always non-swapped in normal data (see
 * CONTRACT: renameExercise must retarget a swapped cell otherwise). */
function firstWeekCellId(grid: MesoGrid | null, row: GridRow | undefined): Id | undefined {
  if (!grid || !row) return undefined;
  const firstWeek = grid.weeks[0];
  if (!firstWeek) return undefined;
  return row.cells[String(firstWeek.id)]?.prescription_id;
}

/** The row's IDENTITY cell: the first live week's cell. (Phase 2a: the
 * one-week swap fields are gone — identity is always the block-shared
 * slot's, so any cell of the row identifies it; the first live week's is the
 * stable pick.) Used by rename (prescription_patch's `name` branch rewrites
 * the block ExerciseSlot.name). */
export function rowIdentityCellId(weeks: GridWeek[], row: GridRow | undefined): Id | undefined {
  if (!row) return undefined;
  const first = weeks[0];
  return first ? row.cells[String(first.id)]?.prescription_id : undefined;
}

// The viewed week for structural verbs that need ONE week id to anchor a
// POST (add-day's `week_id`, undo/redo's `week_id`) — programs are date-less
// and carry no "current" pointer (docs/meso/remove-current-week-plan.md), so
// this is simply the grid's first live week, mirroring the server's own
// `current_week(plan)` degrade (explicit week -> else earliest live week).
function currentWeekId(grid: MesoGrid | null): Id | undefined {
  if (!grid) return undefined;
  return grid.weeks[0]?.id;
}

/** Immutably patch every cell (across every day/row/week) whose
 * prescription_id matches — in practice exactly one, since prescription_id
 * is unique per (row, week). */
function updateCellInGrid(grid: MesoGrid, cellId: Id, patch: Partial<GridCell>): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) => {
        let changed = false;
        const cells: Record<string, GridCell> = {};
        for (const [weekId, c] of Object.entries(row.cells)) {
          if (c.prescription_id === cellId) {
            changed = true;
            cells[weekId] = { ...c, ...patch };
          } else {
            cells[weekId] = c;
          }
        }
        return changed ? { ...row, cells } : row;
      }),
    })),
  };
}

function updateRowInGrid(grid: MesoGrid, exerciseSlotId: Id, patch: Partial<GridRow>): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) =>
        row.exercise_slot_id === exerciseSlotId ? { ...row, ...patch } : row,
      ),
    })),
  };
}

/** Immutably set one (week × line) sub-line's text on a row — updates an
 * existing entry or inserts a new one in line order (the optimistic local
 * mirror of the server's get_or_create upsert in `cell_line_write`). */
function updateCellLineInGrid(
  grid: MesoGrid,
  exerciseSlotId: Id,
  weekId: Id,
  line: number,
  text: string,
): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) => {
        if (row.exercise_slot_id !== exerciseSlotId) return row;
        const key = String(weekId);
        const cell = row.cells[key];
        if (!cell) return row;
        if (line === 0) {
          return { ...row, cells: { ...row.cells, [key]: { ...cell, text } } };
        }
        const lines = [...(cell.lines ?? [])];
        const idx = lines.findIndex((l) => l.line === line);
        if (idx >= 0) {
          lines[idx] = { ...lines[idx]!, text };
        } else {
          lines.push({ line, text });
          lines.sort((a, b) => a.line - b.line);
        }
        return { ...row, cells: { ...row.cells, [key]: { ...cell, lines } } };
      }),
    })),
  };
}

export function useGrid(options: UseGridOptions) {
  const { planId, csrf, initialGrid } = options;
  const [grid, setGrid] = useState<MesoGrid | null>(initialGrid);
  const [history, setHistory] = useState<GridHistory>(initialGrid?.history ?? EMPTY_GRID_HISTORY);

  // One shared in-flight guard across every structural (refetch-driven) verb
  // — mirrors useDeletes' deletingRef / useReorder's reorderingRef, checked
  // synchronously so a double-click can't race two refetches.
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);

  // In-flight cell-autosave POSTs (patchCell/renameExercise are fire-and-
  // forget). fillAcrossWeeks reads the source cell's already-stored DB values
  // server-side, so it must flush these first or it can copy stale data to
  // sibling weeks when a coach edits then immediately fills (Codex P2).
  const pendingWritesRef = useRef<Set<Promise<unknown>>>(new Set());

  const adoptGridHistory = useCallback((data: GridHistoryCarrier) => {
    const h = data?.history;
    if (!h) return;
    setHistory({
      can_undo: h.can_undo,
      can_redo: h.can_redo,
      undo_label: h.undo_label ?? "",
      redo_label: h.redo_label ?? "",
    });
  }, []);

  const flushPendingWrites = useCallback(async () => {
    await Promise.allSettled([...pendingWritesRef.current]);
  }, []);

  const refetchGrid = useCallback(async () => {
    try {
      const res = await fetch(`/meso/api/plan/${planId}/grid/`);
      if (!res.ok) throw new Error("Request failed: " + res.status);
      const data = (await res.json()) as MesoGrid & { ok?: boolean };
      // Issue #455 phase A5: plan/athlete/phases must ride every
      // refetch too, not just the initial hydration — this is now the
      // front-end's ONLY source for the top bar / left rail / block view (the
      // one-week plan_data owner that used to carry them is gone). Dropping
      // any of these here would silently blank that chrome after the very
      // next structural edit (regression test: useGrid.test.ts "refetchGrid
      // carries the new plan/athlete/phases fields through").
      setGrid({
        plan: data.plan,
        athlete: data.athlete,
        phases: data.phases,
        mesocycle: data.mesocycle,
        weeks: data.weeks,
        days: data.days,
        history: data.history,
      });
      setHistory(data.history);
    } catch (err) {
      console.error("Refetch grid failed", err);
    }
  }, [planId]);

  const runStructural = useCallback(async (fn: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await fn();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  const patchCell = useCallback(
    (cellId: Id, patch: GridCellPatch) => {
      setGrid((prev) => (prev ? updateCellInGrid(prev, cellId, patch) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, patch, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Cell autosave failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory],
  );

  const renameExercise = useCallback(
    (exerciseSlotId: Id, name: string) => {
      const row = findRow(grid, exerciseSlotId);
      const cellId = rowIdentityCellId(grid?.weeks ?? [], row);
      if (cellId == null) return;
      setGrid((prev) => (prev ? updateRowInGrid(prev, exerciseSlotId, { name }) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, { name }, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Rename exercise failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [grid, planId, csrf, adoptGridHistory],
  );

  // Phase 2a: write one freeform (week × line) sub-line of a row's stack —
  // addressed by (exercise_slot, week, line), not pk, since a sub-line cell
  // may not exist yet (the server upserts via `cell_line_write`). Same
  // optimistic fire-and-forget shape as patchCell: local repaint immediately,
  // POST not awaited, failure console.error'd. Line 0 routes here too when
  // the caller has no pk handy, though patchCell (by pk) is the normal line-0
  // path.
  const writeCellLine = useCallback(
    (exerciseSlotId: Id, weekId: Id, line: number, text: string) => {
      setGrid((prev) =>
        prev ? updateCellLineInGrid(prev, exerciseSlotId, weekId, line, text) : prev,
      );
      const write = apiPost(
        `/meso/api/plan/${planId}/row/${exerciseSlotId}/cell/`,
        { week_id: weekId, line, text },
        csrf,
      )
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Cell line write failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory],
  );

  // Phase 2a (D2): the per-exercise Tempo/Rest/instructions columns — row
  // attributes on the block-shared ExerciseSlot, written through
  // `exercise_slot_patch`. Same optimistic fire-and-forget shape as patchCell.
  const patchRowColumns = useCallback(
    (exerciseSlotId: Id, patch: GridRowPatch) => {
      setGrid((prev) => (prev ? updateRowInGrid(prev, exerciseSlotId, patch) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/row/${exerciseSlotId}/`, patch, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Row columns autosave failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory],
  );

  const addExercise = useCallback(
    (day: GridDay) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${day.session_id}/exercise/`, null, csrf);
        } catch (err) {
          console.error("Add exercise failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const removeExercise = useCallback(
    (exerciseSlotId: Id) =>
      runStructural(async () => {
        const row = findRow(grid, exerciseSlotId);
        const cellId = firstWeekCellId(grid, row);
        if (cellId == null) return;
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/delete/`, null, csrf);
        } catch (err) {
          console.error("Remove exercise failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, planId, csrf, runStructural, refetchGrid],
  );

  const addDay = useCallback(
    () =>
      runStructural(async () => {
        const weekId = currentWeekId(grid);
        // No live week in the block we're viewing means there is nothing to hang
        // a day on. Posting anyway would send `{week_id: undefined}` — JSON drops
        // the key, and the server's own fallback would create the day in whatever
        // block DOES have a live week, i.e. not the one on screen. Add a week
        // first (that path is block-scoped).
        if (weekId == null) return;
        try {
          await apiPost(`/meso/api/plan/${planId}/session/`, { week_id: weekId }, csrf);
        } catch (err) {
          console.error("Add day failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, planId, csrf, runStructural, refetchGrid],
  );

  const removeDay = useCallback(
    (day: GridDay) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${day.session_id}/delete/`, null, csrf);
        } catch (err) {
          console.error("Remove day failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const addWeek = useCallback(
    () =>
      runStructural(async () => {
        // Post the block we're VIEWING. Without it the server falls back to the
        // plan's first block, which is the same block the grid opens on today —
        // but only by coincidence, and it silently diverged before (an empty
        // first block sent the new week to a later one, where this grid would
        // never show it).
        const mesocycleId = grid?.mesocycle?.id;
        try {
          await apiPost(
            `/meso/api/plan/${planId}/week/`,
            mesocycleId != null ? { mesocycle_id: mesocycleId } : null,
            csrf,
          );
        } catch (err) {
          console.error("Add week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, planId, csrf, runStructural, refetchGrid],
  );

  const removeWeek = useCallback(
    (weekId: Id) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/week/${weekId}/delete/`, null, csrf);
        } catch (err) {
          console.error("Remove week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  // Issue #455 phase A2 (drag reordering): same STRUCTURAL shape as every
  // verb above — the server owns the authoritative order (block-wide P0
  // ExerciseSlot/SessionSlot.order), so these await their POST then
  // refetch the whole grid, sharing busyRef. useTableReorder (the pure
  // drag-event translator) builds `order` from the CURRENT week's live
  // cell/session ids and calls these two verbs — see its own header for the
  // payload contract (mirrors views.py session_reorder/week_reorder_sessions
  // exactly: `order` must be EXACTLY the live id set for the target session/
  // week, in the new order).

  const reorderExercises = useCallback(
    (sessionId: Id, order: number[]) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${sessionId}/reorder/`, { order }, csrf);
        } catch (err) {
          console.error("Reorder exercises failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const reorderDays = useCallback(
    (weekId: Id, order: number[]) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/week/${weekId}/reorder/`, { order }, csrf);
        } catch (err) {
          console.error("Reorder days failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  // Issue #455 phase A2.5 (menu-based cross-day move): closes the parity gap
  // A2's drag scope deliberately left out (separate <table> containers +
  // sticky columns = high dnd-kit risk — see useTableReorder.ts's header).
  // --- P2 exceptions: skip / fill / add-this-week -------------------------
  // Same STRUCTURAL shape as add/removeExercise|Day|Week above — the grid
  // (not just one cell) can change shape/content in ways only the server
  // knows (fill rewrites whole stacks, add-this-week creates a new
  // slot+cells) so these await their POST then refetch, sharing busyRef.
  // (The one-week swap verb is gone — Phase 2a: a substitution is sub-line
  // text, written through writeCellLine above.)

  const skipCell = useCallback(
    (cellId: number, skipped: boolean) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/skip/`, { skipped }, csrf);
        } catch (err) {
          console.error("Skip cell failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const fillAcrossWeeks = useCallback(
    (cellId: number) =>
      runStructural(async () => {
        // Flush any in-flight cell autosave first — fill copies the source
        // cell's ALREADY-STORED DB values server-side, so a just-edited cell
        // must finish committing or the fill can copy stale data (Codex P2).
        await flushPendingWrites();
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/fill/`, {}, csrf);
        } catch (err) {
          console.error("Fill across weeks failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid, flushPendingWrites],
  );

  const addExerciseThisWeek = useCallback(
    (day: GridDay, weekId: number) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${day.session_id}/exercise/`, { week_id: weekId }, csrf);
        } catch (err) {
          console.error("Add exercise this week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const undo = useCallback(
    () =>
      runStructural(async () => {
        if (!history.can_undo) return;
        const weekId = currentWeekId(grid);
        try {
          await apiPost(`/meso/api/plan/${planId}/undo/`, { week_id: weekId }, csrf);
        } catch (err) {
          console.error("Undo failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, history.can_undo, planId, csrf, runStructural, refetchGrid],
  );

  const redo = useCallback(
    () =>
      runStructural(async () => {
        if (!history.can_redo) return;
        const weekId = currentWeekId(grid);
        try {
          await apiPost(`/meso/api/plan/${planId}/redo/`, { week_id: weekId }, csrf);
        } catch (err) {
          console.error("Redo failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, history.can_redo, planId, csrf, runStructural, refetchGrid],
  );

  return {
    grid,
    history,
    busy,
    patchCell,
    renameExercise,
    writeCellLine,
    patchRowColumns,
    addExercise,
    removeExercise,
    addDay,
    removeDay,
    addWeek,
    removeWeek,
    reorderExercises,
    reorderDays,
    skipCell,
    fillAcrossWeeks,
    addExerciseThisWeek,
    undo,
    redo,
    refetchGrid,
  };
}

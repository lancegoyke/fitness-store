// useGrid — self-contained state-owning hook for the P1 multi-week table
// (MesoTable). Owns `grid`/`history` and every verb that mutates it.
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
// Structural verbs (add/remove day|week|exercise, set-current, undo/redo)
// await their POST, then call refetchGrid() (a plain GET, mirroring
// usePlanData's switchWeek) to re-sync the whole grid — mirroring
// usePlanData/useReorder's ref-guard idiom, one shared in-flight guard across
// every structural verb so a double-click can't race two refetches.
import { useCallback, useRef, useState } from "react";
import { apiPost } from "../lib/api";
import type { GridCell, GridDay, GridHistory, GridRow, GridWeek, MesoGrid } from "../lib/api";

export type Id = number | string;

/** The cell fields the coach can type into (rest is new in P1). Everything
 * else on GridCell — prescription_id/skipped/swap_* — is server-derived,
 * display-only in P1 (never sent back in a patch). */
export type GridCellPatch = Partial<
  Pick<GridCell, "sets" | "reps" | "load" | "load_type" | "rpe" | "rest" | "note">
>;

/** P5 group: the adj/adjusts a group override save/clear returns for one cell
 * — patched into that cell (matched by prescription_id) so its adjust badge
 * repaints without a full grid refetch. The grid analog of usePlanData's
 * `patchExercise({adj, adjusts})` on the single-week path. */
export type GridCellAdjPatch = Pick<GridCell, "adj" | "adjusts">;

/** Issue #455 phase A3: the one_rm/one_rm_source a coach_set_one_rm save
 * returns — patched into the row's identity cell (matched by
 * prescription_id) so its %1RM badge repaints without a full grid refetch.
 * The grid analog of GridCellAdjPatch above / useOneRmEditor's
 * `patchExercise({one_rm, one_rm_source})` on the single-week path. */
export type GridCellOneRmPatch = Pick<GridCell, "one_rm" | "one_rm_source">;

/** Any payload carrying a fresh plan history — accepts BOTH the grid
 * endpoints' `GridHistory` (string labels) AND the override editor's
 * `serialize_plan_history` reply routed through `useOverrideEditor`
 * (`undo_label`/`redo_label` typed `string | null`). Coerced to `GridHistory`
 * on adoption below, so either convention lands cleanly. */
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

/** The row's IDENTITY cell: the first live week's cell that is NOT a one-week
 * swap. Shared by rename (prescription_patch only rewrites the block
 * ExerciseSlot.name for an unswapped cell) and setOneRm below (a %1RM is a
 * property of the ROW's block identity, not a swapped week's substitute
 * lift — see setOneRm's header). Falls back to the first week's cell if
 * every week is swapped (rare) — best-effort. Exported so MesoTable can
 * derive the identity cell to READ (`cell.one_rm`/`one_rm_source`) without
 * duplicating this rule. */
export function rowIdentityCellId(weeks: GridWeek[], row: GridRow | undefined): Id | undefined {
  if (!row) return undefined;
  for (const week of weeks) {
    const c = row.cells[String(week.id)];
    if (c && c.swap_name === "" && c.swap_exercise_id == null) return c.prescription_id;
  }
  const first = weeks[0];
  return first ? row.cells[String(first.id)]?.prescription_id : undefined;
}

function currentWeekId(grid: MesoGrid | null): Id | undefined {
  if (!grid) return undefined;
  return (grid.weeks.find((w) => w.current) ?? grid.weeks[0])?.id;
}

/** Immutably patch every cell (across every day/row/week) whose
 * prescription_id matches — in practice exactly one, since prescription_id
 * is unique per (row, week). Takes a `Partial<GridCell>` so both a typed-in
 * field patch (GridCellPatch) and a group adjust repaint (GridCellAdjPatch)
 * route through the same immutable walk. */
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

function updateRowNameInGrid(grid: MesoGrid, exerciseSlotId: Id, name: string): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) => (row.exercise_slot_id === exerciseSlotId ? { ...row, name } : row)),
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
      setGrid({ mesocycle: data.mesocycle, weeks: data.weeks, days: data.days, history: data.history });
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

  // P5 group: repaint one cell's per-athlete adjust badge from a group
  // override save/clear reply. Purely local (no POST) — useOverrideEditor
  // already POSTed to prescription/<id>/override/ and hands back {adj,
  // adjusts, history}; DesignerRoot routes its `patchExercise` here and its
  // `adoptHistory` to adoptGridHistory above. Matches on prescription_id, so
  // it no-ops harmlessly when the edited row lives on the OTHER data owner
  // (planData) rather than the grid.
  const patchCellAdj = useCallback((cellId: Id, patch: GridCellAdjPatch) => {
    setGrid((prev) => (prev ? updateCellInGrid(prev, cellId, patch) : prev));
  }, []);

  const renameExercise = useCallback(
    (exerciseSlotId: Id, name: string) => {
      const row = findRow(grid, exerciseSlotId);
      const cellId = rowIdentityCellId(grid?.weeks ?? [], row);
      if (cellId == null) return;
      setGrid((prev) => (prev ? updateRowNameInGrid(prev, exerciseSlotId, name) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, { name }, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Rename exercise failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [grid, planId, csrf, adoptGridHistory],
  );

  // Issue #455 phase A3: the %1RM editor's save verb — AWAITED (unlike
  // patchCell/renameExercise's fire-and-forget autosave) so RowOneRmEditor
  // (MesoTable.tsx) can drive its own saving/error UI off the promise, mirror-
  // ing useOneRmEditor.saveOneRm on the single-week path. A %1RM is a
  // property of the athlete + lift identity (AthleteOneRm has NO week
  // dimension), so it always targets the row's IDENTITY cell (rowIdentityCellId
  // — shared with renameExercise above), never the cell the coach happens to
  // be looking at. Patches the result LOCALLY (updateCellInGrid, patchCellAdj's
  // precedent) — no refetchGrid (nothing structural changed) and no
  // adoptGridHistory (coach_set_one_rm does not record_plan_action, so the
  // reply carries no `history`).
  const setOneRm = useCallback(
    async (exerciseSlotId: Id, value: string): Promise<GridCellOneRmPatch> => {
      const row = findRow(grid, exerciseSlotId);
      const cellId = rowIdentityCellId(grid?.weeks ?? [], row);
      if (cellId == null) throw new Error("No cell to set a 1RM against.");
      // A just-blurred free-text rename may still be in flight — the server
      // keys a MANUAL 1RM off the prescription's RESOLVED name, so saving
      // before the rename lands would store it under the OLD lift identity
      // (Codex #455 A3 review). Same flush fillAcrossWeeks uses.
      await flushPendingWrites();
      const data = await apiPost<{ one_rm?: string; source?: string }>(
        `/meso/api/plan/${planId}/prescription/${cellId}/one-rm/`,
        { value },
        csrf,
      );
      const patch: GridCellOneRmPatch = { one_rm: data.one_rm ?? "", one_rm_source: data.source ?? "" };
      setGrid((prev) => (prev ? updateCellInGrid(prev, cellId, patch) : prev));
      return patch;
    },
    [grid, planId, csrf, flushPendingWrites],
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
        try {
          await apiPost(`/meso/api/plan/${planId}/week/`, null, csrf);
        } catch (err) {
          console.error("Add week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
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

  const setCurrentWeek = useCallback(
    (weekId: Id) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/week/${weekId}/current/`, null, csrf);
        } catch (err) {
          console.error("Set current week failed", err);
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

  // --- P2 exceptions: skip / swap / fill / add-this-week -----------------
  // Same STRUCTURAL shape as add/removeExercise|Day|Week above — the grid
  // (not just one cell) can change shape/content in ways only the server
  // knows (swap_display is server-resolved, add-this-week creates a new
  // slot+cells) so these await their POST then refetch, sharing busyRef.

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

  const swapCell = useCallback(
    (cellId: number, swapName: string) =>
      runStructural(async () => {
        const body = swapName.trim() ? { swap_name: swapName } : { clear: true };
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/swap/`, body, csrf);
        } catch (err) {
          console.error("Swap cell failed", err);
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
    patchCellAdj,
    adoptGridHistory,
    renameExercise,
    setOneRm,
    addExercise,
    removeExercise,
    addDay,
    removeDay,
    addWeek,
    removeWeek,
    setCurrentWeek,
    reorderExercises,
    reorderDays,
    skipCell,
    swapCell,
    fillAcrossWeeks,
    addExerciseThisWeek,
    undo,
    redo,
    refetchGrid,
  };
}

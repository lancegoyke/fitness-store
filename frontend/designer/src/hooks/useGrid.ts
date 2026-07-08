// useGrid — self-contained state-owning hook for the P1 multi-week table
// (MesoTable). Owns `grid`/`history` and every verb that mutates it.
//
// Cell edits (patchCell/renameExercise) are optimistic + fire-and-forget,
// mirroring useAutosave's semantics (CONTRACT.md "useAutosave") — updated in
// local state immediately, POSTed without being awaited by the caller, and
// NOT rolled back on failure (only console.error'd), same as persistRow.
//
// Structural verbs (add/remove day|week|exercise, set-current, undo/redo)
// await their POST, then call refetchGrid() (a plain GET, mirroring
// usePlanData's switchWeek) to re-sync the whole grid — mirroring
// usePlanData/useReorder's ref-guard idiom, one shared in-flight guard across
// every structural verb so a double-click can't race two refetches.
import { useCallback, useRef, useState } from "react";
import { apiPost } from "../lib/api";
import type { GridCell, GridDay, GridHistory, GridRow, MesoGrid } from "../lib/api";

export type Id = number | string;

/** The cell fields the coach can type into (rest is new in P1). Everything
 * else on GridCell — prescription_id/skipped/swap_* — is server-derived,
 * display-only in P1 (never sent back in a patch). */
export type GridCellPatch = Partial<
  Pick<GridCell, "sets" | "reps" | "load" | "load_type" | "rpe" | "rest" | "note">
>;

interface GridHistoryCarrier {
  history?: GridHistory;
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

function currentWeekId(grid: MesoGrid | null): Id | undefined {
  if (!grid) return undefined;
  return (grid.weeks.find((w) => w.current) ?? grid.weeks[0])?.id;
}

/** Immutably patch every cell (across every day/row/week) whose
 * prescription_id matches — in practice exactly one, since prescription_id
 * is unique per (row, week). */
function updateCellInGrid(grid: MesoGrid, cellId: Id, patch: GridCellPatch): MesoGrid {
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

  const adoptGridHistory = useCallback((data: GridHistoryCarrier) => {
    if (data && data.history) setHistory(data.history);
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
      apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, patch, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Cell autosave failed", err));
    },
    [planId, csrf, adoptGridHistory],
  );

  const renameExercise = useCallback(
    (exerciseSlotId: Id, name: string) => {
      const row = findRow(grid, exerciseSlotId);
      const cellId = firstWeekCellId(grid, row);
      if (cellId == null) return;
      setGrid((prev) => (prev ? updateRowNameInGrid(prev, exerciseSlotId, name) : prev));
      apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, { name }, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => console.error("Rename exercise failed", err));
    },
    [grid, planId, csrf, adoptGridHistory],
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
    addExercise,
    removeExercise,
    addDay,
    removeDay,
    addWeek,
    removeWeek,
    setCurrentWeek,
    undo,
    redo,
    refetchGrid,
  };
}

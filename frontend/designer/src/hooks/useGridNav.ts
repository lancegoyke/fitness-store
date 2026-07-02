// useGridNav — grid keyboard navigation + cell a11y (Phase 3, docs/meso/
// designer-framework-plan.md + scratchpad phase3-spec.md). Owns the roving-
// tabindex anchor, arrow/Enter/Escape key handling on the six per-exercise
// cells (name, sets, reps, load, rpe, note), and focus restoration across a
// full-envelope program swap (undo/redo/week switch/add/delete/confirm).
// Instantiated ONCE inside WeekGrid (see useGridNav.test.tsx's header) —
// DayCard/ExerciseRow receive the result as an optional passthrough prop and
// never call this hook themselves.
//
// Cell identity is always (prescriptionId, column) — NEVER an index — so a
// day/exercise reorder or delete can't silently point the anchor at the
// wrong row. DOM lookups go through `document.querySelector` against a
// `data-grid-cell` attribute (see `gridCellDomKey`) rather than ref-plumbing
// through DayCard/ExerciseRow, so arrow-key moves and focus restoration work
// identically for a hook-only unit test (no React DOM at all) and the real
// mounted app.
import { useEffect, useMemo, useRef, useState } from "react";
import type { FocusEvent, KeyboardEvent } from "react";
import type { Day } from "../lib/api";

type Id = number | string;

export type GridColumn = "name" | "sets" | "reps" | "load" | "rpe" | "note";

export const GRID_COLUMNS: readonly GridColumn[] = ["name", "sets", "reps", "load", "rpe", "note"];

export const GRID_COLUMN_LABELS: Record<GridColumn, string> = {
  name: "exercise name",
  sets: "sets",
  reps: "reps",
  load: "load",
  rpe: "RPE",
  note: "note",
};

export interface GridCellId {
  prescriptionId: Id;
  column: GridColumn;
}

export interface GridCellCallbacks {
  onChange(value: string): void;
  onCommit(): void;
  onRevert(value: string): void;
}

export interface GridCellBindings {
  tabIndex: 0 | -1;
  onFocus(event: FocusEvent<HTMLInputElement>): void;
  onKeyDown(event: KeyboardEvent<HTMLInputElement>): void;
}

export interface UseGridNavResult {
  anchor: GridCellId | null;
  cellProps(prescriptionId: Id, column: GridColumn, callbacks: GridCellCallbacks): GridCellBindings;
}

export interface UseGridNavOptions {
  program: Day[];
}

/** "<exercise name or 'exercise'> — <column label>" (spec's a11y section). */
export function cellAriaLabel(exerciseName: string, column: GridColumn): string {
  return `${exerciseName || "exercise"} — ${GRID_COLUMN_LABELS[column]}`;
}

/** DOM identity for a cell's `data-grid-cell` attribute / querySelector key. */
export function gridCellDomKey(prescriptionId: Id, column: GridColumn): string {
  return `${prescriptionId}:${column}`;
}

interface FlatProgram {
  /** Every exercise id, day-major/exercise-minor (spec's flattened row order). */
  order: Id[];
  dayIdByExercise: Map<Id, Id>;
  firstExerciseByDay: Map<Id, Id>;
}

function flattenProgram(program: Day[]): FlatProgram {
  const order: Id[] = [];
  const dayIdByExercise = new Map<Id, Id>();
  const firstExerciseByDay = new Map<Id, Id>();
  for (const day of program) {
    for (const ex of day.exercises) {
      order.push(ex.id);
      dayIdByExercise.set(ex.id, day.id);
      if (!firstExerciseByDay.has(day.id)) firstExerciseByDay.set(day.id, ex.id);
    }
  }
  return { order, dayIdByExercise, firstExerciseByDay };
}

function firstCellOf(flat: FlatProgram): GridCellId | null {
  const id = flat.order[0];
  return id === undefined ? null : { prescriptionId: id, column: "name" };
}

function focusCell(id: Id, column: GridColumn) {
  const selector = `[data-grid-cell="${gridCellDomKey(id, column)}"]`;
  document.querySelector<HTMLElement>(selector)?.focus();
}

const HANDLED_KEYS = new Set(["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Enter", "Escape"]);

export function useGridNav(options: UseGridNavOptions): UseGridNavResult {
  const { program } = options;
  const flat = useMemo(() => flattenProgram(program), [program]);

  const [anchor, setAnchor] = useState<GridCellId | null>(() => firstCellOf(flat));
  const anchorRef = useRef<GridCellId | null>(anchor);
  const lastDayIdRef = useRef<Id | null>(
    anchor ? (flat.dayIdByExercise.get(anchor.prescriptionId) ?? null) : null,
  );
  // Flips true the first time any cell actually receives focus — gates
  // whether restoration is allowed to steal DOM focus (spec: "the anchor
  // (roving tabIndex) always tracks whatever was restored" but a program
  // swap that happens while the grid was never focused must not yank focus
  // away from e.g. the chat panel).
  const focusedOnceRef = useRef(false);
  // Cell key -> value captured at focus time, for Escape's revert target.
  const focusValuesRef = useRef<Record<string, string>>({});

  function commitAnchor(next: GridCellId | null, flatForLookup: FlatProgram, shouldFocus: boolean) {
    anchorRef.current = next;
    lastDayIdRef.current = next ? (flatForLookup.dayIdByExercise.get(next.prescriptionId) ?? null) : null;
    setAnchor(next);
    if (next && shouldFocus) focusCell(next.prescriptionId, next.column);
  }

  // Restoration (spec "Focus restoration across applyPlanData"): recompute
  // the anchor on EVERY program identity change so tabIndex stays valid even
  // when the grid never had focus, but only call .focus() when it did.
  useEffect(() => {
    const prev = anchorRef.current;
    if (prev === null) {
      commitAnchor(firstCellOf(flat), flat, false);
      return;
    }

    let next: GridCellId | null;
    if (flat.order.includes(prev.prescriptionId)) {
      // Tier 1: same (prescriptionId, column) survives.
      next = { prescriptionId: prev.prescriptionId, column: prev.column };
    } else {
      const dayId = lastDayIdRef.current;
      const firstOfDay = dayId === null ? undefined : flat.firstExerciseByDay.get(dayId);
      // Tier 2: first cell of the same day, else Tier 3: grid's first cell
      // (firstCellOf returns null for Tier 4 — the whole grid is empty).
      next = firstOfDay !== undefined ? { prescriptionId: firstOfDay, column: "name" } : firstCellOf(flat);
    }

    // Restoration may move focus ONLY when it won't steal it: the active
    // element is a grid cell, focus was orphaned by the swap (fell to body),
    // or the coach is on a control explicitly marked data-grid-restore
    // (undo/redo, week chips, add/remove week — swap initiators whose result
    // should return them to the grid). Anything else — the chat composer, the
    // load-type toggle re-rendering under focus on a row patch — keeps focus.
    const active = document.activeElement as HTMLElement | null;
    const allowFocusMove =
      !active ||
      active === document.body ||
      active.hasAttribute("data-grid-cell") ||
      active.closest("[data-grid-restore]") !== null;
    commitAnchor(next, flat, focusedOnceRef.current && allowFocusMove);
    // program is the only externally-driven trigger for restoration; flat is
    // derived from it in lockstep, and the anchor/ref reads are intentionally
    // "current value at effect time", not reactive dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [program]);

  function cellProps(prescriptionId: Id, column: GridColumn, callbacks: GridCellCallbacks): GridCellBindings {
    const tabIndex: 0 | -1 =
      anchor && anchor.prescriptionId === prescriptionId && anchor.column === column ? 0 : -1;

    const onFocus = (event: FocusEvent<HTMLInputElement>) => {
      focusedOnceRef.current = true;
      focusValuesRef.current[gridCellDomKey(prescriptionId, column)] = event.currentTarget.value;
      commitAnchor({ prescriptionId, column }, flat, false);
    };

    const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") return; // undo/redo: untouched.
      if (!HANDLED_KEYS.has(event.key)) return;
      // Modified keys are native text editing (Shift+Arrow selection,
      // Ctrl/Alt/Cmd+Arrow word- and line-navigation) — never grid moves.
      if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;

      // Any handled keydown implies this cell currently holds focus — keep
      // the anchor synced even if no onFocus round-trip preceded it (a
      // hook-only test building a raw keydown event, or a real keydown that
      // arrives before its own focus event settles).
      commitAnchor({ prescriptionId, column }, flat, false);

      switch (event.key) {
        case "ArrowDown":
        case "ArrowUp": {
          event.preventDefault();
          const idx = flat.order.indexOf(prescriptionId);
          if (idx === -1) return;
          const nextIdx = idx + (event.key === "ArrowDown" ? 1 : -1);
          const targetId = flat.order[nextIdx];
          if (targetId === undefined) return; // extreme: no-op, preventDefault already fired.
          commitAnchor({ prescriptionId: targetId, column }, flat, true);
          return;
        }
        case "ArrowRight":
        case "ArrowLeft": {
          const el = event.currentTarget;
          const collapsed = el.selectionStart === el.selectionEnd;
          const atBoundary =
            event.key === "ArrowRight" ? el.selectionStart === el.value.length : el.selectionStart === 0;
          if (!collapsed || !atBoundary) return; // let the caret move natively.
          const colIdx = GRID_COLUMNS.indexOf(column);
          const nextColIdx = colIdx + (event.key === "ArrowRight" ? 1 : -1);
          const nextColumn = GRID_COLUMNS[nextColIdx];
          if (nextColumn === undefined) return; // row extreme: nothing to prevent either.
          event.preventDefault();
          commitAnchor({ prescriptionId, column: nextColumn }, flat, true);
          return;
        }
        case "Enter": {
          event.preventDefault();
          callbacks.onCommit();
          // The committed value is the new Escape baseline — without this, a
          // fresh draft + Escape would roll the UI back PAST the commit,
          // desyncing it from the server.
          focusValuesRef.current[gridCellDomKey(prescriptionId, column)] =
            event.currentTarget.value;
          return;
        }
        case "Escape": {
          event.preventDefault();
          const key = gridCellDomKey(prescriptionId, column);
          const value = focusValuesRef.current[key] ?? event.currentTarget.value;
          callbacks.onRevert(value);
          return;
        }
        default:
          return;
      }
    };

    return { tabIndex, onFocus, onKeyDown };
  }

  return { anchor, cellProps };
}

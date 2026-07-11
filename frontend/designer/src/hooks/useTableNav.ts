// useTableNav — grid keyboard navigation + cell a11y for the P1 multi-week
// table (MesoTable). Sibling of useGridNav (the one-week WeekGrid/DayCard/
// ExerciseRow hook), NOT a shared/generalized abstraction: A5 deletes the
// entire one-week stack (WeekStrip/WeekGrid/DayCard/ExerciseRow +
// useGridNav + its test) in one clean `rm`, and MesoTable's own header
// comment already duplicates ExerciseRow's dirtySinceFocus pattern rather
// than sharing it for the same reason — the shapes genuinely differ (a
// per-row boolean vs a per-cell dirty Set; a 1D (prescriptionId, column)
// identity vs a 2D (rowId, weekId, field) identity over a variable-width
// week axis). Duplication here is small and a proven algorithm re-derived
// over a 2D coordinate, not new design.
//
// Instantiated ONCE inside MesoTable (the hook-owning level, analog of
// WeekGrid) — GridCellEditor/RowNameEditor are module-private and receive
// the result as a required prop; no INERT fallback is needed since they
// never render outside MesoTable.
//
// Cell identity is (rowId, weekId, field) — NEVER an index, and NEVER
// prescription_id: the row-name column and "holes" (a week with no cell for
// a row — add-this-week rows; see MesoTable.tsx's bare <td/> case) have no
// prescription_id. exercise_slot_id/session_slot_id/week.id are fixed-
// lineup block structures (P0) that survive an in-place cell edit, unlike
// prescription_id-keyed identity which would be fine too but isn't
// universally available. DOM lookups go through `document.querySelector`
// against a `data-grid-cell` attribute (see `tableCellDomKey`), exactly
// like useGridNav, so arrow-key moves and focus restoration work
// identically for a hook-only unit test and the real mounted app.
//
// Arrow moves SKID over holes rather than landing on them: useGridNav's
// one-week grid has no gaps (every ExerciseRow field always renders), so its
// "extreme: no-op, preventDefault already fired" precedent only ever had to
// handle running off the end of the row/column. A 2D table isn't that
// simple — a hole (an add-this-week row's missing week) or a skipped cell
// (em-dash + Unskip, no GridCellEditor) can sit in the MIDDLE of a row or
// column, with no `data-grid-cell` node at all. Committing the anchor to
// that coordinate would drop the whole grid out of the tab order (no
// rendered cell left holding tabIndex 0) and leave the anchor pointing at
// nothing. So every arrow move re-checks `cellExists` at keydown time and
// keeps stepping in the same direction, past any hole, to the first
// coordinate that actually renders. If none exists all the way to the edge,
// the anchor is left exactly where it was — vertical moves still
// preventDefault regardless (matching ArrowDown/Up's original unconditional
// behavior), while a horizontal move only preventDefaults once it has
// confirmed at least one adjacent column POSITION exists to attempt (a true
// row-extreme with zero columns left, holes or otherwise, is still a pure
// no-op, exactly as before).
import { useEffect, useMemo, useRef, useState } from "react";
import type { FocusEvent, KeyboardEvent } from "react";
import type { MesoGrid } from "../lib/api";

export type TableColumn = "name" | "sets" | "reps" | "load" | "rpe" | "rest" | "note";

/** Editable per-cell fields, visual order (excludes "name" — that's the
 * leading row-identity column, not a per-week field). */
export const TABLE_FIELDS = ["sets", "reps", "load", "rpe", "rest", "note"] as const;
export type EditableField = (typeof TABLE_FIELDS)[number];

export const TABLE_FIELD_LABELS: Record<TableColumn, string> = {
  name: "exercise name",
  sets: "sets",
  reps: "reps",
  load: "load",
  rpe: "RPE",
  rest: "rest",
  note: "note",
};

export interface TableCellId {
  /** GridRow.exercise_slot_id — vertical identity, never an index. */
  rowId: number;
  /** GridWeek.id, or null for the row-name column. */
  weekId: number | null;
  field: TableColumn;
}

// No onChange here — verified dead surface in useGridNav's GridCellCallbacks
// (never invoked; ExerciseRow's own `changed()` wrapper handles dirtying
// directly, same as MesoTable's GridCellEditor).
export interface TableCellCallbacks {
  onCommit(): void;
  onRevert(value: string): void;
}

export interface TableCellBindings {
  tabIndex: 0 | -1;
  onFocus(event: FocusEvent<HTMLInputElement>): void;
  onKeyDown(event: KeyboardEvent<HTMLInputElement>): void;
}

export interface UseTableNavResult {
  anchor: TableCellId | null;
  cellProps(
    rowId: number,
    weekId: number | null,
    field: TableColumn,
    callbacks: TableCellCallbacks,
  ): TableCellBindings;
}

export interface UseTableNavOptions {
  grid: MesoGrid | null;
}

/** DOM identity for a cell's `data-grid-cell` attribute / querySelector key.
 * `weekId ?? "row"` sentinel means this can never collide with a real week
 * id (a number), e.g. "9:2:sets", "9:row:name". */
export function tableCellDomKey(rowId: number, weekId: number | null, field: TableColumn): string {
  return `${rowId}:${weekId ?? "row"}:${field}`;
}

/** "<row name or 'exercise'> — <week label> — <field label>", or
 * "<name> — exercise name" for the row-name column (no week context). */
export function tableCellAriaLabel(rowName: string, weekLabel: string | null, field: TableColumn): string {
  const name = rowName || "exercise";
  if (field === "name") return `${name} — exercise name`;
  return `${name} — ${weekLabel} — ${TABLE_FIELD_LABELS[field]}`;
}

/** One entry of the horizontal (within-row) axis: the leading name column,
 * then every week's fields in visual order. Identical for every row, so
 * it's derived once from `grid.weeks` — walking it with `.findIndex` drives
 * ArrowRight/Left, including the "no special-case" week-to-week crossing
 * (last field of week N sits immediately before week N+1's first field). */
interface TableColumnPos {
  weekId: number | null;
  field: TableColumn;
}

function buildColumns(grid: MesoGrid | null): TableColumnPos[] {
  const columns: TableColumnPos[] = [{ weekId: null, field: "name" }];
  if (!grid) return columns;
  for (const week of grid.weeks) {
    for (const field of TABLE_FIELDS) {
      columns.push({ weekId: week.id, field });
    }
  }
  return columns;
}

interface FlatTable {
  /** Every row id, day-major/row-minor (server-ordered). */
  rowOrder: number[];
  /** Every week id, index-ordered (server-ordered). */
  weekOrder: number[];
  dayIdByRow: Map<number, number>;
  firstRowByDay: Map<number, number>;
}

function flattenGrid(grid: MesoGrid | null): FlatTable {
  const rowOrder: number[] = [];
  const dayIdByRow = new Map<number, number>();
  const firstRowByDay = new Map<number, number>();
  if (!grid) return { rowOrder, weekOrder: [], dayIdByRow, firstRowByDay };
  for (const day of grid.days) {
    for (const row of day.rows) {
      rowOrder.push(row.exercise_slot_id);
      dayIdByRow.set(row.exercise_slot_id, day.session_slot_id);
      if (!firstRowByDay.has(day.session_slot_id)) firstRowByDay.set(day.session_slot_id, row.exercise_slot_id);
    }
  }
  const weekOrder = grid.weeks.map((w) => w.id);
  return { rowOrder, weekOrder, dayIdByRow, firstRowByDay };
}

function firstCellOf(flat: FlatTable): TableCellId | null {
  const id = flat.rowOrder[0];
  return id === undefined ? null : { rowId: id, weekId: null, field: "name" };
}

function cellSelector(rowId: number, weekId: number | null, field: TableColumn): string {
  return `[data-grid-cell="${tableCellDomKey(rowId, weekId, field)}"]`;
}

function focusCell(rowId: number, weekId: number | null, field: TableColumn) {
  document.querySelector<HTMLElement>(cellSelector(rowId, weekId, field))?.focus();
}

/** Whether a cell actually has a rendered `data-grid-cell` node right now.
 * Checked fresh at keydown time (never precomputed/cached) — cells appear
 * and disappear with skip/unskip and add-this-week, so the DOM is the only
 * source of truth for "is this coordinate landable". Arrow moves use this
 * to skid over holes (see the header comment) instead of committing the
 * anchor to a coordinate nothing renders. */
function cellExists(rowId: number, weekId: number | null, field: TableColumn): boolean {
  return document.querySelector(cellSelector(rowId, weekId, field)) !== null;
}

const HANDLED_KEYS = new Set(["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Enter", "Escape"]);

export function useTableNav(options: UseTableNavOptions): UseTableNavResult {
  const { grid } = options;
  const columns = useMemo(() => buildColumns(grid), [grid]);
  const flat = useMemo(() => flattenGrid(grid), [grid]);

  const [anchor, setAnchor] = useState<TableCellId | null>(() => firstCellOf(flat));
  const anchorRef = useRef<TableCellId | null>(anchor);
  const lastDayIdRef = useRef<number | null>(anchor ? (flat.dayIdByRow.get(anchor.rowId) ?? null) : null);
  // Flips true the first time any cell actually receives focus — gates
  // whether restoration is allowed to steal DOM focus, mirrors useGridNav.
  const focusedOnceRef = useRef(false);
  // Cell key -> value captured at focus time, for Escape's revert target.
  const focusValuesRef = useRef<Record<string, string>>({});

  function commitAnchor(next: TableCellId | null, flatForLookup: FlatTable, shouldFocus: boolean) {
    anchorRef.current = next;
    lastDayIdRef.current = next ? (flatForLookup.dayIdByRow.get(next.rowId) ?? null) : null;
    setAnchor(next);
    if (next && shouldFocus) focusCell(next.rowId, next.weekId, next.field);
  }

  // Restoration: recompute the anchor on EVERY grid identity change so
  // tabIndex stays valid even when the table never had focus, but only call
  // .focus() when it did. Both rowId and weekId can independently disappear
  // (remove-exercise/day vs remove-week), so tier 2 splits in two — see
  // brief §4 for the four-tier rationale (mirrors useGridNav's tiers 1/3/4).
  useEffect(() => {
    const prev = anchorRef.current;
    if (prev === null) {
      commitAnchor(firstCellOf(flat), flat, false);
      return;
    }

    const rowSurvives = flat.rowOrder.includes(prev.rowId);
    const weekSurvives = prev.weekId === null || flat.weekOrder.includes(prev.weekId);

    let next: TableCellId | null;
    if (rowSurvives && weekSurvives) {
      // Tier 1: same (rowId, weekId, field) survives. This checks row/week
      // EXISTENCE (is the id still anywhere in the grid), not whether THIS
      // row still renders a cell at THIS week — a skip or a this-week-only
      // add/remove can hollow out just one coordinate while its row and
      // week both live on elsewhere. Unlike the arrow-key scan above (this
      // PR's #455 review-nit fix), Tier 1 doesn't re-verify against
      // cellExists: doing so would mean deciding what to fall back to
      // (Tier 2b's "first remaining week" isn't guaranteed to exist for
      // this row either), which is restoration-tier redesign, not an
      // arrow-key fix. Tiers 2a/2b/3 land on the name column or the
      // table's first cell, which — row-name always renders regardless of
      // per-week holes — always exist. Left as-is deliberately: revisit
      // with a failing restoration test if Tier 1 is ever observed to
      // strand the anchor in practice.
      next = { rowId: prev.rowId, weekId: prev.weekId, field: prev.field };
    } else if (rowSurvives) {
      // Tier 2b: the week is gone (remove-week) but the row survives — keep
      // the row+field, snap to the first remaining week (simplest rule
      // consistent with tier 2's "first of ..." precedent, not "nearest").
      const firstWeek = flat.weekOrder[0];
      next =
        firstWeek !== undefined
          ? { rowId: prev.rowId, weekId: firstWeek, field: prev.field }
          : { rowId: prev.rowId, weekId: null, field: "name" };
    } else {
      // Row gone (remove-exercise, or its whole day removed).
      const dayId = lastDayIdRef.current;
      const firstOfDay = dayId == null ? undefined : flat.firstRowByDay.get(dayId);
      // Tier 2a: first row of the same day, else Tier 3: table's first cell
      // (firstCellOf returns null for Tier 4 — the whole table is empty).
      next = firstOfDay !== undefined ? { rowId: firstOfDay, weekId: null, field: "name" } : firstCellOf(flat);
    }

    // Restoration may move focus ONLY when it won't steal it: the active
    // element is a grid cell, focus was orphaned by the swap (fell to
    // body), or the coach is on a control explicitly marked
    // data-grid-restore (undo/redo, add-week, make-current, remove-week —
    // swap initiators whose result should return them to the table).
    // Anything else — the chat composer, the load-type toggle re-rendering
    // under focus on a cell patch — keeps focus.
    const active = document.activeElement as HTMLElement | null;
    const allowFocusMove =
      !active ||
      active === document.body ||
      active.hasAttribute("data-grid-cell") ||
      active.closest("[data-grid-restore]") !== null;
    commitAnchor(next, flat, focusedOnceRef.current && allowFocusMove);
    // grid is the only externally-driven trigger for restoration; flat/
    // columns are derived from it in lockstep, and the anchor/ref reads are
    // intentionally "current value at effect time", not reactive deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid]);

  function cellProps(
    rowId: number,
    weekId: number | null,
    field: TableColumn,
    callbacks: TableCellCallbacks,
  ): TableCellBindings {
    const tabIndex: 0 | -1 =
      anchor && anchor.rowId === rowId && anchor.weekId === weekId && anchor.field === field ? 0 : -1;

    const onFocus = (event: FocusEvent<HTMLInputElement>) => {
      focusedOnceRef.current = true;
      focusValuesRef.current[tableCellDomKey(rowId, weekId, field)] = event.currentTarget.value;
      commitAnchor({ rowId, weekId, field }, flat, false);
    };

    const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") return; // undo/redo: untouched.
      if (!HANDLED_KEYS.has(event.key)) return;
      // Modified keys are native text editing (Shift+Arrow selection,
      // Ctrl/Alt/Cmd+Arrow word- and line-navigation) — never grid moves.
      if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;

      // Any handled keydown implies this cell currently holds focus — keep
      // the anchor synced even if no onFocus round-trip preceded it.
      commitAnchor({ rowId, weekId, field }, flat, false);

      switch (event.key) {
        case "ArrowDown":
        case "ArrowUp": {
          event.preventDefault();
          const idx = flat.rowOrder.indexOf(rowId);
          if (idx === -1) return;
          const step = event.key === "ArrowDown" ? 1 : -1;
          // Skid past any row that has no rendered cell at this (weekId,
          // field) — a row's holes are independent of its neighbors' (a
          // missing week or a skipped cell), so the next INDEX isn't
          // necessarily the next LANDABLE row. Stop at the first row that
          // actually renders this column, or fall off the end: either way
          // the key was handled (preventDefault already fired above).
          let nextIdx = idx + step;
          let candidateRowId = flat.rowOrder[nextIdx];
          while (candidateRowId !== undefined && !cellExists(candidateRowId, weekId, field)) {
            nextIdx += step;
            candidateRowId = flat.rowOrder[nextIdx];
          }
          const targetRowId = candidateRowId;
          if (targetRowId === undefined) return; // no rendered cell to the edge: stay put.
          commitAnchor({ rowId: targetRowId, weekId, field }, flat, true);
          return;
        }
        case "ArrowRight":
        case "ArrowLeft": {
          const el = event.currentTarget;
          const collapsed = el.selectionStart === el.selectionEnd;
          const atBoundary =
            event.key === "ArrowRight" ? el.selectionStart === el.value.length : el.selectionStart === 0;
          if (!collapsed || !atBoundary) return; // let the caret move natively.
          const colIdx = columns.findIndex((c) => c.weekId === weekId && c.field === field);
          const step = event.key === "ArrowRight" ? 1 : -1;
          let nextColIdx = colIdx + step;
          let candidateCol = columns[nextColIdx];
          if (candidateCol === undefined) return; // absolute row extreme: no adjacent column at all, nothing to prevent.
          // There IS an adjacent column position, so this key is being
          // handled from here on — preventDefault even if every remaining
          // position turns out to be a hole (below) and the anchor doesn't
          // actually move.
          event.preventDefault();
          // Skid past any column with no rendered cell for THIS row — a
          // hole (an add-this-week row's missing week) or a skipped cell
          // (em-dash + Unskip, no GridCellEditor) both leave no
          // `data-grid-cell` node, so arrowing across one jumps straight to
          // the next editable cell instead of stranding the anchor on a
          // coordinate nothing renders.
          while (candidateCol !== undefined && !cellExists(rowId, candidateCol.weekId, candidateCol.field)) {
            nextColIdx += step;
            candidateCol = columns[nextColIdx];
          }
          const nextCol = candidateCol;
          if (nextCol === undefined) return; // ran out of columns while skidding past holes: stay put, key already handled.
          commitAnchor({ rowId, weekId: nextCol.weekId, field: nextCol.field }, flat, true);
          return;
        }
        case "Enter": {
          event.preventDefault();
          callbacks.onCommit();
          // The committed value is the new Escape baseline — without this, a
          // fresh draft + Escape would roll the UI back PAST the commit,
          // desyncing it from the server.
          focusValuesRef.current[tableCellDomKey(rowId, weekId, field)] = event.currentTarget.value;
          return;
        }
        case "Escape": {
          event.preventDefault();
          const key = tableCellDomKey(rowId, weekId, field);
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

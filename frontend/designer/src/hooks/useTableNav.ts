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
// Phase 2b (spreadsheet keyboard flow) widens both axes to the FULL sheet:
// - Horizontal: the per-row Tempo/Notes/Rest columns join the axis
//   (name → tempo → week 1..N text → notes → rest, the source spreadsheet's
//   column order), and Tab/Shift+Tab walk it unconditionally — wrapping to
//   the next/previous row at the row's ends — where arrows still defer to
//   the caret at a text boundary.
// - Vertical: cell identity gains a LINE — (rowId, weekId, field, line) —
//   so ArrowDown from a week cell's prescription (line 0) steps INTO its
//   sub-line stack (each `cell.lines` entry, then the trailing ghost input
//   that mints the next line), then on to the next row. This is D3's "RPE is
//   a sub-row reached by arrow-down" made literal: the ghost is a real
//   vertical stop, so minting the RPE line never needs the mouse.
// - Enter = commit + move DOWN one stop (the spreadsheet's Enter), and at
//   the last stop of a DAY it appends a new exercise row to that day
//   instead (Enter-adds-row) — unless the current row is still entirely
//   blank, so leaning on Enter can never mint a stack of empty rows. Enter
//   never crosses a day boundary (ArrowDown still does): Enter is the
//   fill/extend key, arrows are pure movement.
//
// Cell identity is (rowId, weekId, field, line) — NEVER an index, and NEVER
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
// Arrow moves SKID over holes rather than landing on them: a hole (an
// add-this-week row's missing week) or a skipped cell (em-dash + Unskip, no
// GridCellEditor) can sit in the MIDDLE of a row or column, with no
// `data-grid-cell` node at all. Committing the anchor to that coordinate
// would drop the whole grid out of the tab order (no rendered cell left
// holding tabIndex 0) and leave the anchor pointing at nothing. So every
// move re-checks `cellExists` at keydown time and keeps stepping in the
// same direction, past any hole, to the first coordinate that actually
// renders. If none exists all the way to the edge, the anchor is left
// exactly where it was — vertical moves still preventDefault regardless
// (matching ArrowDown/Up's original unconditional behavior), while a
// horizontal arrow move only preventDefaults once it has confirmed at least
// one adjacent column POSITION exists to attempt, and Tab preventDefaults
// only once a landable target is found (so tabbing off the table's last
// cell hands focus to the browser's native order — the grid is not a focus
// trap). Sub-line stops skid by LINE too: moving horizontally from line L
// lands on the target cell's largest stop at-or-below it (its ghost when the
// stack is shorter — the spreadsheet's merged-cell feel), never on a line
// that doesn't render.
import { useEffect, useMemo, useRef, useState } from "react";
import type { FocusEvent, KeyboardEvent } from "react";
import type { GridRow, MesoGrid } from "../lib/api";

export type TableColumn = "name" | "tempo" | "text" | "note" | "rest";

/** Editable per-cell fields, visual order (excludes "name" — that's the
 * leading row-identity column, not a per-week field). Phase 2a collapsed the
 * six structured fields (sets/reps/load/rpe/rest/note) to ONE freeform text
 * input per (row, week); 2b then put the sub-line stack and the per-row
 * Tempo/Notes/Rest columns INSIDE the nav axes (see the header) — those are
 * axis positions now, not extra per-week fields, so this stays ["text"]. */
export const TABLE_FIELDS = ["text"] as const;
export type EditableField = (typeof TABLE_FIELDS)[number];

export const TABLE_FIELD_LABELS: Record<TableColumn, string> = {
  name: "exercise name",
  tempo: "tempo",
  text: "prescription",
  note: "notes",
  rest: "rest",
};

export interface TableCellId {
  /** GridRow.exercise_slot_id — vertical identity, never an index. */
  rowId: number;
  /** GridWeek.id, or null for the row-scoped columns (name/tempo/notes/rest). */
  weekId: number | null;
  field: TableColumn;
  /** Sub-line within a week cell's stack (Phase 2b): 0 = the prescription
   * input, >=1 = a sub-line — including the trailing ghost (max existing
   * line + 1, GridCellEditor's nextLine). Always 0 outside week "text". */
  line: number;
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
    line?: number,
  ): TableCellBindings;
  /** Reset a cell's Escape baseline after an out-of-band commit that KEEPS
   * focus — the multi-line stack paste. Mirrors what the Enter handler does
   * for its own commit: without it, Escape after a paste would roll the UI
   * back PAST the committed write, desyncing it from the server. (Blur
   * commits don't need this — the next focus reseeds the baseline.) */
  setRevertBaseline(rowId: number, weekId: number | null, field: TableColumn, value: string, line?: number): void;
}

export interface UseTableNavOptions {
  grid: MesoGrid | null;
  /** Enter-adds-row (Phase 2b): called when Enter fires at the last vertical
   * stop of a day whose row has content — the caller appends a blank
   * exercise row to that day (useGrid.addExercise). Once the day's LAST ROW
   * actually changes on a grid refetch, the hook focuses the new row at the
   * column Enter came from. Return `false` when the append was NOT
   * dispatched (e.g. the grid is busy) so no focus intent is recorded — a
   * dropped dispatch must never move focus when that day's last row later
   * changes for an unrelated reason. Optional so hook-only tests without an
   * append path keep their old "Enter at the bottom stays put" behavior. */
  onAppendRow?(dayId: number): boolean | void;
}

/** DOM identity for a cell's `data-grid-cell` attribute / querySelector key.
 * `weekId ?? "row"` sentinel means this can never collide with a real week
 * id (a number), e.g. "9:2:text", "9:row:name". Line 0 keeps the legacy
 * 3-part key; sub-lines append their line number ("9:2:text:1") so every
 * stop of a cell's stack is independently addressable. */
export function tableCellDomKey(rowId: number, weekId: number | null, field: TableColumn, line = 0): string {
  const base = `${rowId}:${weekId ?? "row"}:${field}`;
  return line === 0 ? base : `${base}:${line}`;
}

/** "<row name or 'exercise'> — <week label> — <field label>", or
 * "<name> — <field label>" for the row-scoped columns (no week context). */
export function tableCellAriaLabel(rowName: string, weekLabel: string | null, field: TableColumn): string {
  const name = rowName || "exercise";
  if (weekLabel === null) return `${name} — ${TABLE_FIELD_LABELS[field]}`;
  return `${name} — ${weekLabel} — ${TABLE_FIELD_LABELS[field]}`;
}

/** One entry of the horizontal (within-row) axis: the leading name column,
 * the per-row Tempo column, every week's fields in visual order, then the
 * per-row Notes and Rest columns — the source spreadsheet's Exercise |
 * Tempo | weeks… | Notes | Rest order (Phase 2b). Identical for every row,
 * so it's derived once from `grid.weeks` — walking it with `.findIndex`
 * drives ArrowRight/Left and Tab, including the "no special-case"
 * week-to-week crossing (week N sits immediately before week N+1). */
interface TableColumnPos {
  weekId: number | null;
  field: TableColumn;
}

function buildColumns(grid: MesoGrid | null): TableColumnPos[] {
  const columns: TableColumnPos[] = [
    { weekId: null, field: "name" },
    { weekId: null, field: "tempo" },
  ];
  if (!grid) return columns;
  for (const week of grid.weeks) {
    for (const field of TABLE_FIELDS) {
      columns.push({ weekId: week.id, field });
    }
  }
  columns.push({ weekId: null, field: "note" });
  columns.push({ weekId: null, field: "rest" });
  return columns;
}

interface FlatTable {
  /** Every row id, day-major/row-minor (server-ordered). */
  rowOrder: number[];
  /** Every week id, index-ordered (server-ordered). */
  weekOrder: number[];
  dayIdByRow: Map<number, number>;
  firstRowByDay: Map<number, number>;
  lastRowByDay: Map<number, number>;
  rowsById: Map<number, GridRow>;
}

function flattenGrid(grid: MesoGrid | null): FlatTable {
  const rowOrder: number[] = [];
  const dayIdByRow = new Map<number, number>();
  const firstRowByDay = new Map<number, number>();
  const lastRowByDay = new Map<number, number>();
  const rowsById = new Map<number, GridRow>();
  if (!grid) return { rowOrder, weekOrder: [], dayIdByRow, firstRowByDay, lastRowByDay, rowsById };
  for (const day of grid.days) {
    for (const row of day.rows) {
      rowOrder.push(row.exercise_slot_id);
      dayIdByRow.set(row.exercise_slot_id, day.session_slot_id);
      rowsById.set(row.exercise_slot_id, row);
      if (!firstRowByDay.has(day.session_slot_id)) firstRowByDay.set(day.session_slot_id, row.exercise_slot_id);
      lastRowByDay.set(day.session_slot_id, row.exercise_slot_id);
    }
  }
  const weekOrder = grid.weeks.map((w) => w.id);
  return { rowOrder, weekOrder, dayIdByRow, firstRowByDay, lastRowByDay, rowsById };
}

function firstCellOf(flat: FlatTable): TableCellId | null {
  const id = flat.rowOrder[0];
  return id === undefined ? null : { rowId: id, weekId: null, field: "name", line: 0 };
}

function cellSelector(rowId: number, weekId: number | null, field: TableColumn, line = 0): string {
  return `[data-grid-cell="${tableCellDomKey(rowId, weekId, field, line)}"]`;
}

function focusCell(rowId: number, weekId: number | null, field: TableColumn, line = 0) {
  document.querySelector<HTMLElement>(cellSelector(rowId, weekId, field, line))?.focus();
}

/** Whether a cell actually has a rendered `data-grid-cell` node right now.
 * Checked fresh at keydown time (never precomputed/cached) — cells appear
 * and disappear with skip/unskip, add-this-week and sub-line minting, so the
 * DOM is the only source of truth for "is this coordinate landable". Moves
 * use this to skid over holes (see the header comment) instead of
 * committing the anchor to a coordinate nothing renders. */
function cellExists(rowId: number, weekId: number | null, field: TableColumn, line = 0): boolean {
  return document.querySelector(cellSelector(rowId, weekId, field, line)) !== null;
}

/** The ordered line stops of one row at one column, top-down as rendered:
 * 0 (the prescription input), each existing `cell.lines` entry, then the
 * ghost (max existing line + 1 — GridCellEditor's nextLine). Row-scoped
 * columns, hole rows and skipped cells contribute just [0]; the DOM check
 * downstream stays the arbiter of whether that stop actually renders. */
function lineStops(row: GridRow | undefined, weekId: number | null, field: TableColumn): number[] {
  if (field !== "text" || weekId === null || !row) return [0];
  const cell = row.cells[String(weekId)];
  if (!cell || cell.skipped) return [0];
  const lineNos = (cell.lines ?? []).map((l) => l.line).sort((a, b) => a - b);
  const ghost = (lineNos[lineNos.length - 1] ?? 0) + 1;
  return [0, ...lineNos, ghost];
}

/** The stop a horizontal move from line `line` should land on in a cell
 * whose stops are `stops` (ascending): the largest stop at-or-below it, so a
 * shorter stack clamps to its ghost/last line rather than skidding the whole
 * cell — the spreadsheet's "the merged cell is still there" feel. */
function nearestLineStop(stops: number[], line: number): number {
  let best = stops[0] ?? 0;
  for (const s of stops) {
    if (s <= line) best = s;
    else break;
  }
  return best;
}

/** The line a horizontal move (arrow or Tab) into `col` should land on:
 * week-text columns clamp to the target cell's nearest stop at-or-below
 * `line` (see nearestLineStop); row-scoped columns are single-line. */
function clampLine(row: GridRow | undefined, col: TableColumnPos, line: number): number {
  if (col.field !== "text" || col.weekId === null) return 0;
  return nearestLineStop(lineStops(row, col.weekId, col.field), line);
}

/** Every (rowId, line) of one column, day-major/row-minor/line-inner — the
 * vertical axis ArrowDown/Up and Enter walk. */
interface VerticalStop {
  rowId: number;
  line: number;
}

function verticalStops(flat: FlatTable, weekId: number | null, field: TableColumn): VerticalStop[] {
  const out: VerticalStop[] = [];
  for (const rowId of flat.rowOrder) {
    for (const line of lineStops(flat.rowsById.get(rowId), weekId, field)) out.push({ rowId, line });
  }
  return out;
}

/** Whether every rendered input of this row is blank RIGHT NOW — read from
 * the DOM, not the grid, so an uncommitted draft (the name the coach just
 * typed, an in-flight optimistic write) counts as content. Guards
 * Enter-adds-row: a fully blank row never appends another. */
function rowDomBlank(rowId: number): boolean {
  const inputs = document.querySelectorAll<HTMLInputElement>(`input[data-grid-cell^="${rowId}:"]`);
  for (const el of Array.from(inputs)) {
    if (el.value.trim() !== "") return false;
  }
  return true;
}

/** The nearest rendered column of `cell`'s own row — forward first, then
 * backward, at line 0. Restoration's post-tier guard uses this when a
 * surviving (row, week) coordinate turns out to be hollow (see the effect
 * below); the leading name column always renders for a live row, so the
 * backward scan terminates there at worst. */
function nearestRenderedInRow(cell: TableCellId, columns: TableColumnPos[]): TableCellId | null {
  const at = columns.findIndex((c) => c.weekId === cell.weekId && c.field === cell.field);
  if (at === -1) return null;
  for (let i = at + 1; i < columns.length; i++) {
    const col = columns[i];
    if (col && cellExists(cell.rowId, col.weekId, col.field)) {
      return { rowId: cell.rowId, weekId: col.weekId, field: col.field, line: 0 };
    }
  }
  for (let i = at - 1; i >= 0; i--) {
    const col = columns[i];
    if (col && cellExists(cell.rowId, col.weekId, col.field)) {
      return { rowId: cell.rowId, weekId: col.weekId, field: col.field, line: 0 };
    }
  }
  return null;
}

const HANDLED_KEYS = new Set(["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Enter", "Escape", "Tab"]);

/** Enter-adds-row in flight: set when Enter fires the append, consumed by
 * the restoration effect once the day's last row actually CHANGES (the
 * refetch landed) — the grid also changes for the commit's own optimistic
 * write first, so "changed last row" is the signal, not "any grid change".
 * `ttl` caps how many grid changes the intent survives (a failed POST never
 * changes the last row, and a stale intent must not steal focus later). */
interface AppendPending {
  dayId: number;
  weekId: number | null;
  field: TableColumn;
  prevLastRow: number | undefined;
  ttl: number;
}

export function useTableNav(options: UseTableNavOptions): UseTableNavResult {
  const { grid, onAppendRow } = options;
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
  const appendPendingRef = useRef<AppendPending | null>(null);

  function commitAnchor(next: TableCellId | null, flatForLookup: FlatTable, shouldFocus: boolean) {
    anchorRef.current = next;
    lastDayIdRef.current = next ? (flatForLookup.dayIdByRow.get(next.rowId) ?? null) : null;
    setAnchor(next);
    if (next && shouldFocus) focusCell(next.rowId, next.weekId, next.field, next.line);
  }

  /** ArrowDown/Up's shared step: the adjacent vertical stop of this column,
   * skidding holes. Returns undefined at the axis edge. */
  function stepVertical(from: TableCellId, step: 1 | -1): TableCellId | undefined {
    const stops = verticalStops(flat, from.weekId, from.field);
    let idx = stops.findIndex((s) => s.rowId === from.rowId && s.line === from.line);
    // The line can vanish mid-flight (an undo dropped the sub-line the
    // anchor sat on): fall back to this row's line 0, which always exists
    // as a stop for a live row.
    if (idx === -1) idx = stops.findIndex((s) => s.rowId === from.rowId && s.line === 0);
    if (idx === -1) return undefined;
    let nextIdx = idx + step;
    let cand = stops[nextIdx];
    while (cand !== undefined && !cellExists(cand.rowId, from.weekId, from.field, cand.line)) {
      nextIdx += step;
      cand = stops[nextIdx];
    }
    if (cand === undefined) return undefined;
    return { rowId: cand.rowId, weekId: from.weekId, field: from.field, line: cand.line };
  }

  // Restoration: recompute the anchor on EVERY grid identity change so
  // tabIndex stays valid even when the table never had focus, but only call
  // .focus() when it did. Both rowId and weekId can independently disappear
  // (remove-exercise/day vs remove-week), so tier 2 splits in two — see
  // brief §4 for the four-tier rationale (mirrors useGridNav's tiers 1/3/4).
  useEffect(() => {
    // Restoration may move focus ONLY when it won't steal it: the active
    // element is a grid cell, focus was orphaned by the swap (fell to
    // body), or the coach is on a control explicitly marked
    // data-grid-restore (undo/redo, add-week, remove-week — swap initiators
    // whose result should return them to the table).
    // Anything else — the chat composer re-rendering under focus on a cell
    // patch — keeps focus.
    const active = document.activeElement as HTMLElement | null;
    const allowFocusMove =
      !active ||
      active === document.body ||
      active.hasAttribute("data-grid-cell") ||
      active.closest("[data-grid-restore]") !== null;

    // Enter-adds-row landing (Phase 2b): once the day's last row CHANGED,
    // the appended row is in this grid — focus it at the column Enter came
    // from (its name column when Enter fired there), falling back through
    // the same guards as normal restoration.
    const pending = appendPendingRef.current;
    if (pending) {
      const lastRow = flat.lastRowByDay.get(pending.dayId);
      if (lastRow !== undefined && lastRow !== pending.prevLastRow) {
        appendPendingRef.current = null;
        let target: TableCellId | null = { rowId: lastRow, weekId: pending.weekId, field: pending.field, line: 0 };
        if (!cellExists(target.rowId, target.weekId, target.field)) {
          target = nearestRenderedInRow(target, columns) ?? firstCellOf(flat);
        }
        if (target) {
          commitAnchor(target, flat, focusedOnceRef.current && allowFocusMove);
          return;
        }
      } else if (lastRow === undefined || --pending.ttl <= 0) {
        // Day gone, or the append never landed (failed POST) — drop the
        // intent rather than letting it steal focus on a later change.
        appendPendingRef.current = null;
      }
    }

    const prev = anchorRef.current;
    if (prev === null) {
      commitAnchor(firstCellOf(flat), flat, false);
      return;
    }

    const rowSurvives = flat.rowOrder.includes(prev.rowId);
    const weekSurvives = prev.weekId === null || flat.weekOrder.includes(prev.weekId);

    let next: TableCellId | null;
    if (rowSurvives && weekSurvives) {
      // Tier 1: same (rowId, weekId, field, line) survives — row/week
      // EXISTENCE only; whether the coordinate still RENDERS is the
      // post-tier guard below's job (a skip can hollow out one coordinate
      // while its row and week both live on).
      next = { rowId: prev.rowId, weekId: prev.weekId, field: prev.field, line: prev.line };
    } else if (rowSurvives) {
      // Tier 2b: the week is gone (remove-week) but the row survives — keep
      // the row+field, snap to the first remaining week (simplest rule
      // consistent with tier 2's "first of ..." precedent, not "nearest").
      const firstWeek = flat.weekOrder[0];
      next =
        firstWeek !== undefined
          ? { rowId: prev.rowId, weekId: firstWeek, field: prev.field, line: 0 }
          : { rowId: prev.rowId, weekId: null, field: "name", line: 0 };
    } else {
      // Row gone (remove-exercise, or its whole day removed).
      const dayId = lastDayIdRef.current;
      const firstOfDay = dayId == null ? undefined : flat.firstRowByDay.get(dayId);
      // Tier 2a: first row of the same day, else Tier 3: table's first cell
      // (firstCellOf returns null for Tier 4 — the whole table is empty).
      next =
        firstOfDay !== undefined ? { rowId: firstOfDay, weekId: null, field: "name", line: 0 } : firstCellOf(flat);
    }

    // Post-tier guard (#455 review): a SURVIVING coordinate can still be
    // hollow — skipping the focused cell (or dropping an add-this-week
    // cell) refetches the grid with that input gone while its row and week
    // both live on; an undo can drop the SUB-LINE the anchor sat on while
    // the cell itself lives on. Committing the anchor there would zero the
    // whole table out of the tab order (every rendered cell gets
    // tabIndex=-1 and focusCell no-ops). This effect runs after React
    // committed the new DOM, so cellExists sees the post-refetch truth:
    // fall back to the cell's line 0 first, then skid to the nearest
    // rendered column of the same row (the name column always renders for
    // a live row), else the table's first cell.
    if (next && !cellExists(next.rowId, next.weekId, next.field, next.line)) {
      if (next.line !== 0 && cellExists(next.rowId, next.weekId, next.field)) {
        next = { ...next, line: 0 };
      } else {
        next = nearestRenderedInRow(next, columns) ?? firstCellOf(flat);
      }
    }

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
    line = 0,
  ): TableCellBindings {
    const tabIndex: 0 | -1 =
      anchor && anchor.rowId === rowId && anchor.weekId === weekId && anchor.field === field && anchor.line === line
        ? 0
        : -1;

    const onFocus = (event: FocusEvent<HTMLInputElement>) => {
      focusedOnceRef.current = true;
      focusValuesRef.current[tableCellDomKey(rowId, weekId, field, line)] = event.currentTarget.value;
      commitAnchor({ rowId, weekId, field, line }, flat, false);
    };

    const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") return; // undo/redo: untouched.
      if (!HANDLED_KEYS.has(event.key)) return;
      // Modified keys are native text editing (Shift+Arrow selection,
      // Ctrl/Alt/Cmd+Arrow word- and line-navigation) or browser chrome
      // (Ctrl+Tab) — never grid moves. Shift is a real modifier for
      // everything except Tab, where it's the reverse direction.
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.shiftKey && event.key !== "Tab") return;

      // Any handled keydown implies this cell currently holds focus — keep
      // the anchor synced even if no onFocus round-trip preceded it.
      commitAnchor({ rowId, weekId, field, line }, flat, false);

      switch (event.key) {
        case "ArrowDown":
        case "ArrowUp": {
          event.preventDefault();
          const next = stepVertical({ rowId, weekId, field, line }, event.key === "ArrowDown" ? 1 : -1);
          if (next === undefined) return; // no rendered stop to the edge: stay put.
          commitAnchor(next, flat, true);
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
          // coordinate nothing renders. A sub-line move clamps to each
          // candidate's nearest stop (see nearestLineStop).
          while (candidateCol !== undefined) {
            const candLine = clampLine(flat.rowsById.get(rowId), candidateCol, line);
            if (cellExists(rowId, candidateCol.weekId, candidateCol.field, candLine)) {
              commitAnchor({ rowId, weekId: candidateCol.weekId, field: candidateCol.field, line: candLine }, flat, true);
              return;
            }
            nextColIdx += step;
            candidateCol = columns[nextColIdx];
          }
          return; // ran out of columns while skidding past holes: stay put, key already handled.
        }
        case "Tab": {
          // Spreadsheet Tab: next/previous column unconditionally (no caret
          // gate — Tab is never text editing), wrapping to the next/
          // previous row's first/last column at the row's ends. Only
          // preventDefault once a landable target is found, so tabbing off
          // the table's edge falls back to the browser's native order.
          const step = event.shiftKey ? -1 : 1;
          const colIdx = columns.findIndex((c) => c.weekId === weekId && c.field === field);
          const rowIdx = flat.rowOrder.indexOf(rowId);
          if (colIdx === -1 || rowIdx === -1) return;
          let r = rowIdx;
          let c = colIdx + step;
          let ln = line;
          for (;;) {
            if (c < 0) {
              r -= 1;
              c = columns.length - 1;
              ln = 0;
            } else if (c >= columns.length) {
              r += 1;
              c = 0;
              ln = 0;
            }
            const rowAt = flat.rowOrder[r];
            const col = columns[c];
            if (rowAt === undefined || col === undefined) return; // ran off the table: native Tab leaves the grid.
            const candLine = clampLine(flat.rowsById.get(rowAt), col, ln);
            if (cellExists(rowAt, col.weekId, col.field, candLine)) {
              event.preventDefault();
              commitAnchor({ rowId: rowAt, weekId: col.weekId, field: col.field, line: candLine }, flat, true);
              return;
            }
            c += step;
          }
        }
        case "Enter": {
          event.preventDefault();
          callbacks.onCommit();
          // The committed value is the new Escape baseline — without this, a
          // fresh draft + Escape would roll the UI back PAST the commit,
          // desyncing it from the server.
          focusValuesRef.current[tableCellDomKey(rowId, weekId, field, line)] = event.currentTarget.value;
          // Spreadsheet Enter (Phase 2b): commit + move DOWN one stop —
          // through this row's own sub-line stack first (D3's arrow-down RPE
          // row), then the next row of the SAME day. At the day's last stop,
          // append a new exercise row instead (Enter-adds-row) — unless this
          // row is still entirely blank, so Enter can never mint a stack of
          // empty rows. Enter never crosses into the next day; that's
          // ArrowDown's job.
          const next = stepVertical({ rowId, weekId, field, line }, 1);
          const dayId = flat.dayIdByRow.get(rowId);
          if (next !== undefined && flat.dayIdByRow.get(next.rowId) === dayId) {
            commitAnchor(next, flat, true);
            return;
          }
          if (dayId === undefined || !onAppendRow) return;
          if (rowDomBlank(rowId)) return;
          const pending: AppendPending = {
            dayId,
            weekId,
            field,
            prevLastRow: flat.lastRowByDay.get(dayId),
            ttl: 4,
          };
          if (onAppendRow(dayId) === false) return; // dropped (busy): record no intent.
          appendPendingRef.current = pending;
          return;
        }
        case "Escape": {
          event.preventDefault();
          const key = tableCellDomKey(rowId, weekId, field, line);
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

  function setRevertBaseline(rowId: number, weekId: number | null, field: TableColumn, value: string, line = 0) {
    focusValuesRef.current[tableCellDomKey(rowId, weekId, field, line)] = value;
  }

  return { anchor, cellProps, setRevertBaseline };
}

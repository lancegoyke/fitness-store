// MesoTable (P1 multi-week table) — one <table> per training day, exercise
// rows down the side, WEEK COLUMNS across the top. THE coach's editing
// surface — issue #455 phase A5 deleted the one-week-at-a-time view
// (WeekStrip/WeekGrid/DayCard/ExerciseRow) and every hook that only existed
// to feed it (usePlanData/useAutosave/useDeletes/useUndoRedo/useReorder/
// useOneRmEditor/useGridNav); this file's comments below still reference
// those retired files by name as historical "ported from" / "mirrors"
// context for where a given pattern originated, not because they still
// exist in the tree.
//
// Per-cell edits commit on blur/Enter, carrying forward ExerciseRow's
// dirtySinceFocus pattern (CONTRACT.md "ExerciseRow"). Phase 2a (spreadsheet
// parity) collapsed the cell's six structured inputs (sets/reps/load+
// load_type/rpe/rest/note) to ONE freeform text input (`cell.text`, via
// useGrid.patchCell) plus one input per sub-line of the cell's stack
// (`cell.lines`, upserted by (week × line) via onWriteCellLine) and a
// trailing ghost input that mints the next sub-line on its first non-blank
// commit. Tempo/Notes/Rest moved off the cell onto per-ROW columns
// (row.tempo/note/rest, committed via onPatchRowColumns), matching the
// source spreadsheet's Exercise | Tempo | weeks… | Notes | Rest layout.
//
// Keyboard grid navigation (issue #455 A1) is owned by useTableNav
// (../hooks/useTableNav), a sibling of the one-week path's useGridNav —
// instantiated ONCE here, below, and threaded into GridCellEditor/
// RowNameEditor/CellSubLineInput/RowColumnInput as a required prop (they're
// module-private, so there's no INERT-fallback case to support). Phase 2b
// (spreadsheet keyboard flow) widened its axes to the full sheet: sub-lines
// are vertical stops (D3's arrow-down RPE row, ghost included), Tempo/
// Notes/Rest are horizontal columns, Tab walks the row, Enter commits +
// moves down and appends a row at a day's last stop (wired to onAddExercise
// via useTableNav's onAppendRow), and the prescription input carries the
// stack copy/paste handlers (Ctrl-C with no selection copies the whole
// stack; multi-line paste replaces it — the duplicate-forward primitive).
//
// Drag reordering (issue #455 A2) — row + day — is owned by useTableReorder
// (a sibling of DesignerRoot's instantiation, NOT this file): MesoTable only
// wires up dnd-kit's DndContext/sensors/DragOverlay and the two drag
// handles, translating dnd-kit's real DragEndEvent into the pure
// TableDragEndEvent shape and forwarding it to the optional `onDragEnd`
// prop, mirroring WeekGrid.tsx's onDragEnd/handleDragEnd split exactly.
// Cross-day row moves are OUT of scope for this phase (see
// useTableReorder.ts's header) — enforced at the collision-filter layer
// below (filterTableDragCandidates) so a row drag never even collides with
// another day's rows. No live CSS.Transform on a <tr> or a day block (a
// transformed row inside border-collapse + a sticky first column, or a
// transformed block inside .meso-table-scroll's overflow-x:auto, are both
// known cross-browser glitches, unverifiable in jsdom) — a DragOverlay ghost
// plus `.is-dragging` opacity only.
//
// Phase 2a RETIRED two whole control clusters from this file: the per-ROW
// %1RM badge/editor (RowOneRmEditor — a % load is just text now, no typed
// load to resolve) and the one-week swap badge/menu (a substitution is
// sub-line text, written like any other line). skip/unskip, fill-across-
// weeks, add-this-week and move-to-day all stay. (The per-cell group
// adjust badge went with the group subsystem itself.)
import { useEffect, useRef, useState } from "react";
import type { ClipboardEvent } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  pointerWithin,
  rectIntersection,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { CollisionDetection, DragEndEvent, DragStartEvent, KeyboardCoordinateGetter } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import type { GridCell, GridDay, GridHistory, GridRow, GridWeek, MesoGrid } from "../lib/api";
import type { GridCellPatch, GridRowPatch, Id } from "../hooks/useGrid";
import { useTableNav, tableCellDomKey, tableCellAriaLabel } from "../hooks/useTableNav";
import type { UseTableNavResult } from "../hooks/useTableNav";
import type { TableDragData, TableDragEndEvent } from "../hooks/useTableReorder";
import { TABLE_DAY_DRAG_PREFIX, tableDayDragId, tableRowDragId, tableRowDragPrefix } from "../lib/tableDragIds";

export interface MesoTableProps {
  grid: MesoGrid | null;
  history: GridHistory;
  busy: boolean;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  // Phase 2a: upsert one freeform (week × line) sub-line of a row's stack —
  // addressed by slot/week/line, not pk, since the line may not exist yet
  // (useGrid.writeCellLine). Fire-and-forget, like onPatchCell.
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string): void;
  // Phase 2a (D2): the per-exercise Tempo/Notes/Rest row columns
  // (useGrid.patchRowColumns). Fire-and-forget, like onPatchCell.
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string): void;
  // Issue #455 phase A2.5: the per-ROW "Move to…" menu's structural verb —
  // fire-and-forget, mirroring onAddExercise/onRemoveExercise etc. below
  // (useGrid.moveExerciseToDay awaits its own POST + refetch internally).
  onMoveExerciseToDay(exerciseSlotId: Id, targetDay: GridDay): void;
  onAddExercise(day: GridDay): void;
  onRemoveExercise(exerciseSlotId: Id): void;
  onAddDay(): void;
  onRemoveDay(day: GridDay): void;
  onAddWeek(): void;
  onRemoveWeek(weekId: Id): void;
  onSetCurrentWeek(weekId: Id): void;
  onUndo(): void;
  onRedo(): void;
  // P2 exceptions: one-week skip + text-stack fill-across-weeks + a
  // this-week-only add — CONTRACT.md "MesoTable.tsx — new props".
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
  // Issue #455 phase A2 (drag reordering): optional, with a no-op fallback
  // in handleDragEnd below — mirrors WeekGrid.tsx's onDragEnd prop, so
  // MesoTable.test.tsx's existing baseProps() (which never sets it) keeps
  // passing untouched.
  onDragEnd?(event: TableDragEndEvent): void;
}

/** The single arm/confirm slot — mirrors usePlanData's PendingDelete
 * (one thing armed at a time), but kept local to MesoTable since useGrid's
 * remove verbs fire the mutation directly with no confirm step of their own. */
type ArmedKind = "exercise" | "day" | "week";
type Armed = { type: ArmedKind; id: Id } | null;

// Issue #455 phase A2: sortable ids are "day-<sessionSlotId>" and
// "row-<daySlotId>-<exerciseSlotId>" (id-string encoded, mirroring
// WeekGrid.tsx's "day-"/"ex-" prefix convention) — built EXCLUSIVELY via
// tableDayDragId/tableRowDragId (../lib/tableDragIds), the single source of
// truth for this encoding (Codex #455 A2 review finding 1). A day drag
// targets only day containers; a row drag targets only ROW containers of
// its OWN day — cross-day row moves are OUT of scope for A2 (decisions
// 5/7), enforced here at the collision-filter layer (and independently
// again inside useTableReorder's onDragEnd, off TableDragData — never off
// these strings). Unlike WeekGrid's exercise-active filter (which keeps day
// containers too, for the one-week grid's exercise-over-day append path),
// the table has no cross-type drop target at all in A2.
export function filterTableDragCandidates<T extends { id: unknown }>(activeId: unknown, containers: T[]): T[] {
  const activeIdStr = String(activeId);
  if (activeIdStr.startsWith(TABLE_DAY_DRAG_PREFIX)) {
    return containers.filter((c) => String(c.id).startsWith(TABLE_DAY_DRAG_PREFIX));
  }
  const daySlotId = activeIdStr.split("-")[1] ?? "";
  return containers.filter((c) => String(c.id).startsWith(tableRowDragPrefix(daySlotId)));
}

// Same type/scope filtering at the collision layer as
// filterTableDragCandidates above. INTERSECTION-based on purpose, with NO
// closest-center fallback: closestCenter always returns the nearest
// candidate even when the drop lands nowhere near it, which would turn an
// unsupported cross-day drop (candidates are same-day only) into a phantom
// same-day reorder against whichever row happened to be nearest (Codex
// #455 A2 review). Outside every candidate → no collision → `over` stays
// null → the drop no-ops. pointerWithin first (precise for real pointer
// drags), rectIntersection as the fallback (keyboard drags move the overlay
// rect with no pointer coordinates).
export const tableCollisionDetection: CollisionDetection = (args) => {
  const droppableContainers = filterTableDragCandidates(args.active.id, args.droppableContainers);
  const within = pointerWithin({ ...args, droppableContainers });
  if (within.length > 0) return within;
  return rectIntersection({ ...args, droppableContainers });
};

// Ported verbatim-adapted from WeekGrid.tsx's typedKeyboardCoordinates:
// DroppableContainersMap is a real Map subclass — a spread/assign clone
// borrows its prototype WITHOUT Map internal slots, and .get() then throws
// "called on incompatible receiver". Delegate every member to the original
// map (methods bound to it), overriding only getEnabled with the filter.
export const tableKeyboardCoordinates: KeyboardCoordinateGetter = (event, args) => {
  const containers = args.context.droppableContainers;
  const filtered = new Proxy(containers, {
    get(target, prop) {
      if (prop === "getEnabled") {
        return () => filterTableDragCandidates(args.context.active?.id ?? "", target.getEnabled());
      }
      const value = Reflect.get(target, prop, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  return sortableKeyboardCoordinates(event, {
    ...args,
    context: { ...args.context, droppableContainers: filtered },
  });
};

interface CellSubLineInputProps {
  cellId: number;
  rowId: number;
  weekId: number;
  line: number;
  text: string;
  /** The trailing "next line" input — commits only non-blank (a blank ghost
   * has nothing to create), and remounts empty via its parent's key once the
   * optimistic upsert promotes its text to a real `cell.lines` entry. */
  ghost?: boolean;
  tableNav: UseTableNavResult;
  onWrite(line: number, text: string): void;
}

/** One freeform sub-line of a cell's stack (Phase 2a) — same dirty-tracking
 * commit-on-blur/Enter + Escape-revert shape as GridCellEditor's main text
 * input. Phase 2b put sub-lines INSIDE useTableNav's axes (each line is a
 * vertical stop at (rowId, weekId, "text", line) — including the ghost, so
 * D3's RPE row is literally "arrow down and type"), so Enter/Escape and the
 * arrows all come from cellProps now instead of a local onKeyDown.
 * Blanking an EXISTING line commits "" — the line clears in place (the row
 * stays), mirroring the server's blank-text upsert. */
function CellSubLineInput({ cellId, rowId, weekId, line, text, ghost, tableNav, onWrite }: CellSubLineInputProps) {
  const [draft, setDraft] = useState(text);
  const dirtyRef = useRef(false);

  useEffect(() => {
    setDraft(text);
    dirtyRef.current = false;
  }, [text]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    if (ghost && draft.trim() === "") return;
    onWrite(line, draft);
  }

  const navProps = tableNav.cellProps(
    rowId,
    weekId,
    "text",
    {
      onCommit: commitIfDirty,
      onRevert: (value) => {
        dirtyRef.current = false;
        setDraft(value);
      },
    },
    line,
  );

  return (
    <input
      className={`meso-cell meso-line-input${ghost ? " meso-line-input--ghost" : ""}`}
      data-testid={ghost ? `cell-line-new-${cellId}` : `cell-line-${cellId}-${line}`}
      data-grid-cell={tableCellDomKey(rowId, weekId, "text", line)}
      aria-label={ghost ? "Add a line" : `Line ${line}`}
      placeholder={ghost ? "+ line" : "—"}
      value={draft}
      onChange={(e) => {
        dirtyRef.current = true;
        setDraft(e.target.value);
      }}
      onBlur={commitIfDirty}
      {...navProps}
    />
  );
}

interface GridCellEditorProps {
  cell: GridCell;
  row: GridRow;
  week: GridWeek;
  tableNav: UseTableNavResult;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string): void;
}

/** Phase 2a text-first cell: ONE freeform text input for the prescription
 * line (`cell.text`, committed via onPatchCell — the pk path), then one
 * CellSubLineInput per existing sub-line (`cell.lines`, upserted by
 * (week × line) via onWriteCellLine), then a trailing ghost input that mints
 * the NEXT sub-line (max existing line + 1, or 1) on its first non-blank
 * commit. Dirty-tracking commit-on-blur/Enter + Escape-revert carries
 * forward from the retired six-field editor unchanged in kind.
 *
 * Phase 2b adds stack copy/paste on the prescription input (the cell-level
 * duplicate-forward primitive — copy a cell, arrow to another week, paste):
 * Ctrl-C with NOTHING selected copies the whole stack (line 0 + non-blank
 * sub-lines, newline-joined) — a real text selection keeps native copy —
 * and pasting MULTI-LINE text replaces the whole stack (line 0 via
 * onPatchCell, the rest via onWriteCellLine, any longer existing lines
 * blanked so the result equals the source). Single-line paste stays native
 * caret insertion into the draft. */
function GridCellEditor({ cell, row, week, tableNav, onPatchCell, onWriteCellLine }: GridCellEditorProps) {
  const [draft, setDraft] = useState(cell.text);
  const dirtyRef = useRef(false);

  // Resync the draft whenever the source of truth changes — our own commit's
  // optimistic update, or an external refetch (undo/redo, another coach
  // action) — never while the coach is mid-edit, since this only runs when
  // the value actually changes.
  useEffect(() => {
    setDraft(cell.text);
    dirtyRef.current = false;
  }, [cell.text]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onPatchCell(cell.prescription_id, { text: draft });
  }

  function revert(value: string) {
    dirtyRef.current = false;
    setDraft(value);
  }

  const navProps = tableNav.cellProps(row.exercise_slot_id, week.id, "text", {
    onCommit: commitIfDirty,
    onRevert: revert,
  });

  const cellId = cell.prescription_id;
  const lines = cell.lines ?? [];
  const nextLine = lines.reduce((max, l) => Math.max(max, l.line), 0) + 1;

  function onCopy(e: ClipboardEvent<HTMLInputElement>) {
    const el = e.currentTarget;
    if (el.selectionStart !== el.selectionEnd) return; // real selection: native copy wins.
    e.preventDefault();
    const stack = [draft, ...lines.filter((l) => l.text.trim() !== "").map((l) => l.text)].join("\n");
    e.clipboardData.setData("text/plain", stack);
  }

  function onPaste(e: ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text/plain");
    if (!pasted.includes("\n")) return; // single line: native caret insertion into the draft.
    e.preventDefault();
    const parts = pasted.replace(/\r\n/g, "\n").split("\n");
    const head = parts[0] ?? "";
    const rest = parts.slice(1);
    while (rest.length && (rest[rest.length - 1] ?? "").trim() === "") rest.pop();
    dirtyRef.current = false;
    setDraft(head);
    onPatchCell(cellId, { text: head });
    // The pasted head is committed while focus stays here — it's the new
    // Escape baseline (same rule as the Enter handler's), or Escape would
    // roll the UI back past the commit.
    tableNav.setRevertBaseline(row.exercise_slot_id, week.id, "text", head);
    rest.forEach((text, i) => onWriteCellLine(row.exercise_slot_id, week.id, i + 1, text));
    // Blank any existing line beyond the pasted stack so the result equals
    // the source cell (a cleared line stays rendered in place — Phase 2a's
    // blank-upsert semantics — rather than carrying stale text).
    for (const l of lines) {
      if (l.line > rest.length && l.text !== "") onWriteCellLine(row.exercise_slot_id, week.id, l.line, "");
    }
  }

  return (
    <div className="meso-table-cell-editor">
      <input
        className="meso-cell meso-text-input"
        data-testid={`cell-text-${cellId}`}
        data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "text")}
        aria-label={tableCellAriaLabel(row.name, week.label, "text")}
        placeholder="—"
        value={draft}
        onChange={(e) => {
          dirtyRef.current = true;
          setDraft(e.target.value);
        }}
        onBlur={commitIfDirty}
        onCopy={onCopy}
        onPaste={onPaste}
        {...navProps}
      />
      {lines.map((l) => (
        <CellSubLineInput
          key={l.line}
          cellId={cellId}
          rowId={row.exercise_slot_id}
          weekId={week.id}
          line={l.line}
          text={l.text}
          tableNav={tableNav}
          onWrite={(line, text) => onWriteCellLine(row.exercise_slot_id, week.id, line, text)}
        />
      ))}
      <CellSubLineInput
        key={`ghost-${nextLine}`}
        cellId={cellId}
        rowId={row.exercise_slot_id}
        weekId={week.id}
        line={nextLine}
        text=""
        ghost
        tableNav={tableNav}
        onWrite={(line, text) => onWriteCellLine(row.exercise_slot_id, week.id, line, text)}
      />
    </div>
  );
}

interface RowColumnInputProps {
  row: GridRow;
  field: "tempo" | "rest" | "note";
  label: string;
  tableNav: UseTableNavResult;
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
}

/** Phase 2a (D2): one per-exercise row column (Tempo / Notes / Rest) — a row
 * attribute off the block-shared ExerciseSlot, NOT a per-week cell value.
 * Same dirty-tracking commit-on-blur/Enter + Escape-revert shape as
 * CellSubLineInput above. Phase 2b put these columns INSIDE useTableNav's
 * horizontal axis (name → tempo → weeks… → notes → rest, the source
 * spreadsheet's order), so Enter/Escape and the arrows come from cellProps
 * now instead of a local onKeyDown. */
function RowColumnInput({ row, field, label, tableNav, onPatchRowColumns }: RowColumnInputProps) {
  const synced = row[field];
  const [draft, setDraft] = useState(synced);
  const dirtyRef = useRef(false);

  useEffect(() => {
    setDraft(synced);
    dirtyRef.current = false;
  }, [synced]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onPatchRowColumns(row.exercise_slot_id, { [field]: draft });
  }

  const navProps = tableNav.cellProps(row.exercise_slot_id, null, field, {
    onCommit: commitIfDirty,
    onRevert: (value) => {
      dirtyRef.current = false;
      setDraft(value);
    },
  });

  return (
    <input
      className="meso-cell meso-row-col-input"
      data-testid={`row-${field}-${row.exercise_slot_id}`}
      data-grid-cell={tableCellDomKey(row.exercise_slot_id, null, field)}
      aria-label={`${row.name || "exercise"} — ${label}`}
      placeholder="—"
      value={draft}
      onChange={(e) => {
        dirtyRef.current = true;
        setDraft(e.target.value);
      }}
      onBlur={commitIfDirty}
      {...navProps}
    />
  );
}

interface CellActionsProps {
  cell: GridCell;
  busy: boolean;
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
}

/** P2 exceptions control cluster for a non-skipped cell — skip /
 * fill-across-weeks (arm -> confirm, mirroring the remove-exercise|day|week
 * arm/confirm pattern above, but scoped locally to this one cell rather than
 * the table-wide `armed` slot since several cells can each have their own
 * fill-confirm open at once). Phase 2a retired the swap control — a
 * substitution is sub-line text now, typed like any other line. */
function CellActions({ cell, busy, onSkipCell, onFillAcrossWeeks }: CellActionsProps) {
  const [fillArmed, setFillArmed] = useState(false);
  const cellId = cell.prescription_id;

  return (
    <div className="meso-table-cell-actions">
      <button
        type="button"
        data-testid={`cell-skip-${cellId}`}
        className="meso-cell-action-btn"
        disabled={busy}
        aria-label="Skip this week"
        title="Skip this week"
        onClick={() => onSkipCell(cellId, true)}
      >
        Skip
      </button>

      {!fillArmed && (
        <button
          type="button"
          data-testid={`cell-fill-${cellId}`}
          className="meso-cell-action-btn"
          disabled={busy}
          aria-label="Fill across weeks"
          title="Copy this week's prescription (all lines) to every other week"
          onClick={() => setFillArmed(true)}
        >
          Fill →
        </button>
      )}
      {fillArmed && (
        <span className="meso-confirm-pair">
          <button
            type="button"
            data-testid={`cell-fill-confirm-${cellId}`}
            className="meso-confirm-btn"
            disabled={busy}
            aria-label="Confirm fill across weeks"
            onClick={() => {
              onFillAcrossWeeks(cellId);
              setFillArmed(false);
            }}
          >
            Confirm?
          </button>
          <button
            type="button"
            data-testid={`cell-fill-cancel-${cellId}`}
            className="meso-cancel-btn"
            disabled={busy}
            aria-label="Cancel fill across weeks"
            onClick={() => setFillArmed(false)}
          >
            Cancel
          </button>
        </span>
      )}
    </div>
  );
}

interface RowNameEditorProps {
  row: GridRow;
  tableNav: UseTableNavResult;
  onRename(exerciseSlotId: Id, name: string): void;
}

function RowNameEditor({ row, tableNav, onRename }: RowNameEditorProps) {
  const [value, setValue] = useState(row.name);
  const dirtyRef = useRef(false);

  useEffect(() => {
    setValue(row.name);
    dirtyRef.current = false;
  }, [row.name]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onRename(row.exercise_slot_id, value);
  }

  // Mirrors GridCellEditor's revertField/ExerciseRow's revert: writes the
  // focus-time value directly (bypassing the dirtying onChange path) and
  // clears the dirty flag so a subsequent blur doesn't re-commit the draft
  // the coach just backed out of.
  function revert(newValue: string) {
    dirtyRef.current = false;
    setValue(newValue);
  }

  const navProps = tableNav.cellProps(row.exercise_slot_id, null, "name", {
    onCommit: commitIfDirty,
    onRevert: revert,
  });

  return (
    <input
      className="meso-cell meso-ex-name-input"
      data-testid={`row-name-${row.exercise_slot_id}`}
      data-grid-cell={tableCellDomKey(row.exercise_slot_id, null, "name")}
      aria-label={tableCellAriaLabel(row.name, null, "name")}
      value={value}
      onChange={(e) => {
        dirtyRef.current = true;
        setValue(e.target.value);
      }}
      onBlur={commitIfDirty}
      {...navProps}
    />
  );
}

/** The row's live cell key for the grid's CURRENT week (`grid.weeks.find(w
 * => w.current)`, falling back to `weeks[0]` — same "current week" notion
 * useGrid.ts's own local `currentWeekId` helper uses for every structural
 * verb), or undefined if the grid carries no weeks at all. Shared by
 * RowMoveToDaySelect below to gate the row's own visibility and to look up
 * each target day's current-week session id — `prescription_move`'s
 * block-wide re-point can only key off THIS week (see useGrid.moveExerciseToDay's
 * header). */
function currentWeekKey(weeks: GridWeek[]): string | undefined {
  const id = (weeks.find((w) => w.current) ?? weeks[0])?.id;
  return id == null ? undefined : String(id);
}

interface RowMoveToDaySelectProps {
  row: GridRow;
  day: GridDay;
  days: GridDay[];
  weekKey: string | undefined;
  busy: boolean;
  onMoveExerciseToDay(exerciseSlotId: Id, targetDay: GridDay): void;
}

/** Issue #455 phase A2.5 — a menu-based cross-day move, closing the parity
 * gap A2's drag scope deliberately left out (separate <table> containers +
 * sticky columns = high dnd-kit risk; see useTableReorder.ts's header). Row-
 * name column, 2nd line, alongside RowOneRmEditor — only rendered on a
 * multi-day grid, and only when this row has a live cell for the CURRENT
 * week (the only week the server's block-wide re-point can key off). Local
 * `value` state, mirroring every other row-local toggle in this file
 * (CellActions/AddThisWeekControl) — resets to the placeholder itself on
 * every choice rather than depending on a parent re-render to force it back. */
function RowMoveToDaySelect({ row, day, days, weekKey, busy, onMoveExerciseToDay }: RowMoveToDaySelectProps) {
  const [value, setValue] = useState("");

  if (days.length <= 1) return null;
  if (weekKey == null || !row.cells[weekKey]) return null;

  const otherDays = days.filter((d) => d.session_slot_id !== day.session_slot_id);
  const id = row.exercise_slot_id;

  return (
    <select
      data-testid={`row-move-day-${id}`}
      className="meso-move-day-select"
      aria-label={`Move ${row.name || "exercise"} to another day`}
      value={value}
      disabled={busy}
      onChange={(e) => {
        const raw = e.target.value;
        setValue("");
        if (!raw) return;
        const target = otherDays.find((d) => String(d.session_slot_id) === raw);
        if (target) onMoveExerciseToDay(id, target);
      }}
    >
      <option value="" disabled>
        Move to…
      </option>
      {otherDays.map((d) => (
        <option key={d.session_slot_id} value={String(d.session_slot_id)} disabled={d.session_ids[weekKey] == null}>
          {d.name ? `D${d.day_number} · ${d.name}` : `Day ${d.day_number}`}
        </option>
      ))}
    </select>
  );
}

interface AddThisWeekControlProps {
  day: GridDay;
  weeks: GridWeek[];
  busy: boolean;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
}

/** P2: alongside the existing block-wide "+ Add exercise" — a toggle that
 * reveals a week picker (one button per live week), for adding an exercise
 * to just one week instead of the whole block. Local open/closed state,
 * mirroring CellActions' swap toggle above (independent per day). */
function AddThisWeekControl({ day, weeks, busy, onAddExerciseThisWeek }: AddThisWeekControlProps) {
  const [open, setOpen] = useState(false);
  const slotId = day.session_slot_id;

  return (
    <div className="meso-table-add-this-week">
      <button
        type="button"
        data-hover="add"
        className="meso-add-row"
        data-testid={`add-this-week-${slotId}`}
        disabled={busy}
        aria-label="Add exercise for this week only"
        onClick={() => setOpen((v) => !v)}
      >
        + Add this week only
      </button>
      {open && (
        <div className="meso-week-picker">
          {weeks.map((week) => (
            <button
              type="button"
              key={week.id}
              data-testid={`add-this-week-${slotId}-${week.id}`}
              className="meso-week-strip-btn"
              disabled={busy}
              onClick={() => {
                onAddExerciseThisWeek(day, week.id);
                setOpen(false);
              }}
            >
              {week.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

interface TableRowProps {
  row: GridRow;
  day: GridDay;
  days: GridDay[];
  weeks: GridWeek[];
  busy: boolean;
  tableNav: UseTableNavResult;
  rowArmed: boolean;
  onArmRow(): void;
  onConfirmRemoveRow(): void;
  onCancelRemoveRow(): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string): void;
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string): void;
  onMoveExerciseToDay(exerciseSlotId: Id, targetDay: GridDay): void;
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
}

/** Issue #455 phase A2: one exercise row — now a dnd-kit sortable item
 * within its day's own row SortableContext. Drag LISTENERS are bound only
 * to the handle button (dnd-kit's documented "drag handle" pattern, mirrors
 * ExerciseRow.tsx) — a click/drag anywhere else in the row (a cell input, a
 * badge) never starts a drag. No live CSS.Transform on the <tr> (see this
 * file's header) — only the `.is-dragging` opacity class. */
function TableRow({
  row,
  day,
  days,
  weeks,
  busy,
  tableNav,
  rowArmed,
  onArmRow,
  onConfirmRemoveRow,
  onCancelRemoveRow,
  onPatchCell,
  onWriteCellLine,
  onPatchRowColumns,
  onRenameExercise,
  onMoveExerciseToDay,
  onSkipCell,
  onFillAcrossWeeks,
}: TableRowProps) {
  const weekKey = currentWeekKey(weeks);
  const showMoveToDay = days.length > 1;
  const dragData: TableDragData = {
    type: "row",
    daySlotId: day.session_slot_id,
    exerciseSlotId: row.exercise_slot_id,
  };
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useSortable({
    id: tableRowDragId(day.session_slot_id, row.exercise_slot_id),
    data: dragData,
    disabled: busy,
  });

  return (
    <tr
      ref={setNodeRef}
      className={isDragging ? "is-dragging" : undefined}
      data-testid={`meso-row-${row.exercise_slot_id}`}
    >
      <td className="meso-table-row-name-col">
        <div className="meso-table-row-name-row">
          <button
            type="button"
            ref={setActivatorNodeRef}
            data-testid={`row-drag-${row.exercise_slot_id}`}
            className="meso-drag-handle"
            aria-label={`Reorder ${row.name || "exercise"}`}
            disabled={busy}
            {...attributes}
            {...listeners}
          >
            ⠿
          </button>
          <RowNameEditor row={row} tableNav={tableNav} onRename={onRenameExercise} />
          {!rowArmed && (
            <button
              type="button"
              data-testid={`remove-exercise-${row.exercise_slot_id}`}
              className="meso-remove-x meso-remove-x--sm"
              disabled={busy}
              aria-label="Remove exercise"
              title="Remove exercise"
              onClick={onArmRow}
            >
              ×
            </button>
          )}
          {rowArmed && (
            <span className="meso-confirm-pair">
              <button
                type="button"
                data-testid={`confirm-remove-exercise-${row.exercise_slot_id}`}
                className="meso-confirm-btn"
                disabled={busy}
                aria-label="Confirm remove exercise"
                onClick={onConfirmRemoveRow}
              >
                Confirm?
              </button>
              <button
                type="button"
                data-testid={`cancel-remove-exercise-${row.exercise_slot_id}`}
                className="meso-cancel-btn"
                disabled={busy}
                aria-label="Cancel remove exercise"
                onClick={onCancelRemoveRow}
              >
                Cancel
              </button>
            </span>
          )}
        </div>
        {showMoveToDay && (
          <div className="meso-table-row-tags meso-ex-tags">
            <RowMoveToDaySelect
              row={row}
              day={day}
              days={days}
              weekKey={weekKey}
              busy={busy}
              onMoveExerciseToDay={onMoveExerciseToDay}
            />
          </div>
        )}
      </td>
      <td className="meso-table-row-col meso-table-row-col--tempo">
        <RowColumnInput row={row} field="tempo" label="tempo" tableNav={tableNav} onPatchRowColumns={onPatchRowColumns} />
      </td>
      {weeks.map((week) => {
        const cell = row.cells[String(week.id)];
        const testId = `cell-${row.exercise_slot_id}-${week.id}`;
        if (!cell) return <td key={week.id} data-testid={testId} />;
        return (
          <td key={week.id} data-testid={testId} className="meso-table-cell">
            {cell.skipped ? (
              <>
                <span className="meso-table-skipped" data-testid={`cell-skipped-${cell.prescription_id}`}>
                  —
                </span>
                <button
                  type="button"
                  data-testid={`cell-unskip-${cell.prescription_id}`}
                  className="meso-cell-action-btn"
                  disabled={busy}
                  aria-label="Unskip this week"
                  title="Unskip this week"
                  onClick={() => onSkipCell(cell.prescription_id, false)}
                >
                  Unskip
                </button>
              </>
            ) : (
              <>
                <GridCellEditor
                  cell={cell}
                  row={row}
                  week={week}
                  tableNav={tableNav}
                  onPatchCell={onPatchCell}
                  onWriteCellLine={onWriteCellLine}
                />
                <CellActions
                  cell={cell}
                  busy={busy}
                  onSkipCell={onSkipCell}
                  onFillAcrossWeeks={onFillAcrossWeeks}
                />
              </>
            )}
          </td>
        );
      })}
      <td className="meso-table-row-col meso-table-row-col--note">
        <RowColumnInput row={row} field="note" label="notes" tableNav={tableNav} onPatchRowColumns={onPatchRowColumns} />
      </td>
      <td className="meso-table-row-col meso-table-row-col--rest">
        <RowColumnInput row={row} field="rest" label="rest" tableNav={tableNav} onPatchRowColumns={onPatchRowColumns} />
      </td>
    </tr>
  );
}

interface TableDayBlockProps {
  day: GridDay;
  days: GridDay[];
  weeks: GridWeek[];
  busy: boolean;
  // True only for the first day block — the one that renders the week
  // lifecycle controls (see WeekColumnHeader.showControls).
  showWeekControls: boolean;
  tableNav: UseTableNavResult;
  isArmed(type: ArmedKind, id: Id): boolean;
  arm(type: ArmedKind, id: Id): void;
  disarm(): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string): void;
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string): void;
  onMoveExerciseToDay(exerciseSlotId: Id, targetDay: GridDay): void;
  onAddExercise(day: GridDay): void;
  onRemoveExercise(exerciseSlotId: Id): void;
  onRemoveDay(day: GridDay): void;
  onSetCurrentWeek(weekId: Id): void;
  onRemoveWeek(weekId: Id): void;
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
}

/** Issue #455 phase A2: one training day's table — now also a dnd-kit
 * sortable item within the block's own day-strip SortableContext (mirrors
 * DayCard.tsx). Same no-live-transform rule as TableRow above — only
 * `.is-dragging` opacity, no CSS.Transform on the block itself. */
function TableDayBlock({
  day,
  days,
  weeks,
  busy,
  showWeekControls,
  tableNav,
  isArmed,
  arm,
  disarm,
  onPatchCell,
  onWriteCellLine,
  onPatchRowColumns,
  onRenameExercise,
  onMoveExerciseToDay,
  onAddExercise,
  onRemoveExercise,
  onRemoveDay,
  onSetCurrentWeek,
  onRemoveWeek,
  onSkipCell,
  onFillAcrossWeeks,
  onAddExerciseThisWeek,
}: TableDayBlockProps) {
  const dayArmed = isArmed("day", day.session_slot_id);
  const dragData: TableDragData = { type: "day", sessionSlotId: day.session_slot_id };
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useSortable({
    id: tableDayDragId(day.session_slot_id),
    data: dragData,
    disabled: busy,
  });
  const dayHandleLabel = `Reorder ${day.name || `Day ${day.day_number}`}`;

  return (
    <div className={`meso-table-day${isDragging ? " is-dragging" : ""}`} ref={setNodeRef}>
      <div className="meso-table-day-header">
        <button
          type="button"
          ref={setActivatorNodeRef}
          data-testid={`day-drag-${day.session_slot_id}`}
          className="meso-drag-handle"
          aria-label={dayHandleLabel}
          disabled={busy}
          {...attributes}
          {...listeners}
        >
          ⠿
        </button>
        <div className="meso-day-name">{day.name}</div>
        {day.bias && <div className="meso-day-bias">{day.bias}</div>}
        <div className="meso-flex-spacer" />
        {!dayArmed && (
          <button
            type="button"
            data-testid={`remove-day-${day.session_slot_id}`}
            className="meso-remove-x"
            disabled={busy}
            aria-label="Remove this day"
            title="Remove this day"
            onClick={() => arm("day", day.session_slot_id)}
          >
            ×
          </button>
        )}
        {dayArmed && (
          <span className="meso-confirm-pair">
            <button
              type="button"
              data-testid={`confirm-remove-day-${day.session_slot_id}`}
              className="meso-confirm-btn"
              disabled={busy}
              aria-label="Confirm remove day"
              onClick={() => {
                onRemoveDay(day);
                disarm();
              }}
            >
              Confirm?
            </button>
            <button
              type="button"
              data-testid={`cancel-remove-day-${day.session_slot_id}`}
              className="meso-cancel-btn"
              disabled={busy}
              aria-label="Cancel remove day"
              onClick={disarm}
            >
              Cancel
            </button>
          </span>
        )}
      </div>

      <div className="meso-table-scroll">
        <table className="meso-table" data-testid={`meso-day-table-${day.session_slot_id}`}>
          <thead>
            <tr>
              <th className="meso-table-exercise-col">Exercise</th>
              <th className="meso-table-row-col-th">Tempo</th>
              {weeks.map((week) => (
                <WeekColumnHeader
                  key={week.id}
                  week={week}
                  armed={isArmed("week", week.id)}
                  busy={busy}
                  showControls={showWeekControls}
                  onArm={() => arm("week", week.id)}
                  onDisarm={disarm}
                  onSetCurrentWeek={onSetCurrentWeek}
                  onRemoveWeek={onRemoveWeek}
                />
              ))}
              <th className="meso-table-row-col-th">Notes</th>
              <th className="meso-table-row-col-th">Rest</th>
            </tr>
          </thead>
          <tbody>
            <SortableContext
              items={day.rows.map((r) => tableRowDragId(day.session_slot_id, r.exercise_slot_id))}
              strategy={verticalListSortingStrategy}
            >
              {day.rows.map((row) => (
                <TableRow
                  key={row.exercise_slot_id}
                  row={row}
                  day={day}
                  days={days}
                  weeks={weeks}
                  busy={busy}
                  tableNav={tableNav}
                  rowArmed={isArmed("exercise", row.exercise_slot_id)}
                  onArmRow={() => arm("exercise", row.exercise_slot_id)}
                  onConfirmRemoveRow={() => {
                    onRemoveExercise(row.exercise_slot_id);
                    disarm();
                  }}
                  onCancelRemoveRow={disarm}
                  onPatchCell={onPatchCell}
                  onWriteCellLine={onWriteCellLine}
                  onPatchRowColumns={onPatchRowColumns}
                  onRenameExercise={onRenameExercise}
                  onMoveExerciseToDay={onMoveExerciseToDay}
                  onSkipCell={onSkipCell}
                  onFillAcrossWeeks={onFillAcrossWeeks}
                />
              ))}
            </SortableContext>
          </tbody>
        </table>
      </div>

      <div className="meso-table-add-row-group">
        <button
          type="button"
          data-hover="add"
          className="meso-add-row"
          data-testid={`add-exercise-${day.session_slot_id}`}
          disabled={busy}
          onClick={() => onAddExercise(day)}
        >
          + Add exercise
        </button>
        <AddThisWeekControl day={day} weeks={weeks} busy={busy} onAddExerciseThisWeek={onAddExerciseThisWeek} />
      </div>
    </div>
  );
}

export function MesoTable(props: MesoTableProps) {
  const {
    grid,
    history,
    busy,
    onPatchCell,
    onWriteCellLine,
    onPatchRowColumns,
    onRenameExercise,
    onMoveExerciseToDay,
    onAddExercise,
    onRemoveExercise,
    onAddDay,
    onRemoveDay,
    onAddWeek,
    onRemoveWeek,
    onSetCurrentWeek,
    onUndo,
    onRedo,
    onSkipCell,
    onFillAcrossWeeks,
    onAddExerciseThisWeek,
    onDragEnd,
  } = props;

  const [armed, setArmed] = useState<Armed>(null);
  const isArmed = (type: ArmedKind, id: Id) => !!armed && armed.type === type && armed.id === id;
  const arm = (type: ArmedKind, id: Id) => setArmed({ type, id });
  const disarm = () => setArmed(null);

  // Rules of Hooks: called unconditionally, before the `!grid` early return
  // below — useTableNav tolerates a null grid the same way (anchor stays
  // null, no throw). Phase 2b: Enter at the last stop of a day appends a
  // blank exercise row to THAT day (Enter-adds-row) — same verb as the day's
  // "+ Add exercise" button, same busy gate. Returning false on a dropped
  // dispatch keeps the hook from recording a focus intent for an append
  // that never happened.
  const tableNav = useTableNav({
    grid,
    onAppendRow: (dayId) => {
      const day = grid?.days.find((d) => d.session_slot_id === dayId);
      if (!day || busy) return false;
      onAddExercise(day);
      return true;
    },
  });

  // Issue #455 phase A2 (drag reordering): PointerSensor gets a small
  // activation distance so a plain click into a cell input doesn't start a
  // drag; KeyboardSensor rides the handle buttons' tab-order focus
  // (Space/Enter lifts, arrows move, Space/Enter drops, Escape cancels) —
  // mirrors WeekGrid.tsx's sensors exactly, pointed at this file's own
  // tableKeyboardCoordinates.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: tableKeyboardCoordinates }),
  );

  // DragOverlay ghost label — set from the lifted item's own data on drag
  // start, cleared on drop. No live sibling reflow: rows/days snap to their
  // new position only once refetchGrid resolves, matching every other
  // structural verb's UX (skip/add-exercise etc.).
  const [activeDragLabel, setActiveDragLabel] = useState<string | null>(null);

  function handleDragStart(event: DragStartEvent) {
    if (!grid) return;
    const data = event.active.data.current as TableDragData | undefined;
    if (!data) return;
    if (data.type === "row") {
      const activeDay = grid.days.find((d) => d.session_slot_id === data.daySlotId);
      const activeRow = activeDay?.rows.find((r) => r.exercise_slot_id === data.exerciseSlotId);
      setActiveDragLabel(activeRow?.name || "exercise");
    } else {
      const activeDay = grid.days.find((d) => d.session_slot_id === data.sessionSlotId);
      setActiveDragLabel(activeDay ? activeDay.name || `Day ${activeDay.day_number}` : "day");
    }
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveDragLabel(null);
    if (!onDragEnd) return;
    const { active, over } = event;
    onDragEnd({
      active: { id: active.id, data: { current: active.data.current as TableDragData } },
      over: over ? { id: over.id, data: { current: over.data.current as TableDragData } } : null,
    });
  }

  if (!grid) return null;

  return (
    <div className="meso-table-view" data-testid="meso-table-view">
      <div className="meso-table-toolbar">
        <button
          type="button"
          data-testid="grid-undo"
          data-hover="rail"
          data-grid-restore=""
          className="meso-week-strip-btn"
          disabled={busy || !history.can_undo}
          aria-label="Undo"
          title={history.undo_label ? "Undo: " + history.undo_label : "Undo"}
          onClick={onUndo}
        >
          ↺ Undo
        </button>
        <button
          type="button"
          data-testid="grid-redo"
          data-hover="rail"
          data-grid-restore=""
          className="meso-week-strip-btn"
          disabled={busy || !history.can_redo}
          aria-label="Redo"
          title={history.redo_label ? "Redo: " + history.redo_label : "Redo"}
          onClick={onRedo}
        >
          Redo ↻
        </button>
        <div className="meso-flex-spacer" />
        <button
          type="button"
          data-testid="add-week"
          data-hover="add"
          data-grid-restore=""
          className="meso-week-strip-btn meso-week-strip-btn--dashed"
          disabled={busy}
          onClick={onAddWeek}
        >
          + Add week
        </button>
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={tableCollisionDetection}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <SortableContext items={grid.days.map((d) => tableDayDragId(d.session_slot_id))} strategy={verticalListSortingStrategy}>
          {grid.days.map((day, dayIndex) => (
            <TableDayBlock
              key={day.session_slot_id}
              day={day}
              days={grid.days}
              weeks={grid.weeks}
              busy={busy}
              showWeekControls={dayIndex === 0}
              tableNav={tableNav}
              isArmed={isArmed}
              arm={arm}
              disarm={disarm}
              onPatchCell={onPatchCell}
              onWriteCellLine={onWriteCellLine}
              onPatchRowColumns={onPatchRowColumns}
              onRenameExercise={onRenameExercise}
              onMoveExerciseToDay={onMoveExerciseToDay}
              onAddExercise={onAddExercise}
              onRemoveExercise={onRemoveExercise}
              onRemoveDay={onRemoveDay}
              onSetCurrentWeek={onSetCurrentWeek}
              onRemoveWeek={onRemoveWeek}
              onSkipCell={onSkipCell}
              onFillAcrossWeeks={onFillAcrossWeeks}
              onAddExerciseThisWeek={onAddExerciseThisWeek}
            />
          ))}
        </SortableContext>
        <DragOverlay>
          {activeDragLabel ? <div className="meso-table-drag-ghost">{activeDragLabel}</div> : null}
        </DragOverlay>
      </DndContext>

      <button
        type="button"
        data-hover="add"
        className="meso-add-day-btn"
        data-testid="add-day"
        disabled={busy}
        onClick={onAddDay}
      >
        + Add day
      </button>
    </div>
  );
}

interface WeekColumnHeaderProps {
  week: GridWeek;
  armed: boolean;
  busy: boolean;
  // The week is the same across every day's table, so its lifecycle controls
  // (make-current / remove) render only once — on the first day block
  // (designer-simplify). Other day tables show just the label + current mark.
  showControls: boolean;
  onArm(): void;
  onDisarm(): void;
  onSetCurrentWeek(weekId: Id): void;
  onRemoveWeek(weekId: Id): void;
}

function WeekColumnHeader({ week, armed, busy, showControls, onArm, onDisarm, onSetCurrentWeek, onRemoveWeek }: WeekColumnHeaderProps) {
  return (
    <th
      data-testid={`week-col-${week.id}`}
      aria-current={week.current ? "true" : undefined}
      className={`meso-table-week-col${week.current ? " meso-table-week-col--current" : ""}`}
    >
      <div className="meso-table-week-label">
        <span>{week.label}</span>
        {week.deload && (
          <span aria-label="Deload week" title="Deload week" className="meso-table-deload-marker">
            ▽
          </span>
        )}
      </div>
      {showControls && !week.current && (
        <div className="meso-table-week-controls">
          <button
            type="button"
            data-testid={`make-current-${week.id}`}
            data-grid-restore=""
            className="meso-week-strip-btn meso-week-strip-btn--accent"
            disabled={busy}
            title="Make this the athlete's week — their home and today's session anchor on it"
            onClick={() => onSetCurrentWeek(week.id)}
          >
            Make current
          </button>
          {!armed && (
            <button
              type="button"
              data-testid={`remove-week-${week.id}`}
              data-grid-restore=""
              className="meso-week-strip-btn"
              disabled={busy}
              aria-label="Remove this week"
              title="Remove this week"
              onClick={onArm}
            >
              Remove
            </button>
          )}
          {armed && (
            <span className="meso-week-strip-confirm">
              <button
                type="button"
                data-testid={`confirm-remove-week-${week.id}`}
                data-grid-restore=""
                className="meso-week-strip-btn meso-week-strip-btn--confirm"
                disabled={busy}
                aria-label="Confirm remove week"
                onClick={() => {
                  onRemoveWeek(week.id);
                  onDisarm();
                }}
              >
                Confirm?
              </button>
              <button
                type="button"
                data-testid={`cancel-remove-week-${week.id}`}
                data-grid-restore=""
                className="meso-week-strip-btn"
                disabled={busy}
                aria-label="Cancel remove week"
                onClick={onDisarm}
              >
                Cancel
              </button>
            </span>
          )}
        </div>
      )}
    </th>
  );
}

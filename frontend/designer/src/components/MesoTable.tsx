// MesoTable (P1 multi-week table) — one <table> per training day, exercise
// rows down the side, WEEK COLUMNS across the top. Becomes the coach's
// default editing surface; WeekStrip/WeekGrid/DayCard/ExerciseRow (the
// one-week-at-a-time view) stay reachable as a transitional fallback (they
// are NOT deleted in this PR).
//
// Per-cell fields commit on blur/Enter, carrying forward ExerciseRow's
// dirtySinceFocus pattern (CONTRACT.md "ExerciseRow") — but scoped PER CELL,
// tracking which of a cell's several fields were actually typed into, since
// useGrid.patchCell's endpoint takes a partial patch (only the dirtied
// fields), not a whole-row POST like useAutosave.persistRow. The load_type
// toggle is the one exception: like ExerciseRow's toggleLoadType, it commits
// immediately on click (an atomic flip, not a per-keystroke draft).
//
// Keyboard grid navigation (issue #455 A1) is owned by useTableNav
// (../hooks/useTableNav), a sibling of the one-week path's useGridNav —
// instantiated ONCE here, below, and threaded into GridCellEditor/
// RowNameEditor as a required prop (they're module-private, so there's no
// INERT-fallback case to support).
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
// P1: deferred (see docs/archive/meso/fixed-selection-plan.md) — in-cell
// group override editor / one-rm editor, agent-chat wiring, coachmarks.
// Swap/skip/add-this-week WRITE UX is P2 — this file only ever DISPLAYS
// swap_name/skipped.
import { useEffect, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { CollisionDetection, DragEndEvent, DragStartEvent, KeyboardCoordinateGetter } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import type { GridCell, GridDay, GridHistory, GridRow, GridWeek, GroupIdentity, MesoGrid } from "../lib/api";
import type { GridCellPatch, Id } from "../hooks/useGrid";
import { useTableNav, tableCellDomKey, tableCellAriaLabel } from "../hooks/useTableNav";
import type { EditableField, UseTableNavResult } from "../hooks/useTableNav";
import type { TableDragData, TableDragEndEvent } from "../hooks/useTableReorder";

export interface MesoTableProps {
  grid: MesoGrid | null;
  history: GridHistory;
  busy: boolean;
  unit: string;
  // P5 group: the plan's group identity (null for an individual plan). When
  // it has members, every non-skipped cell gains a per-athlete adjust badge
  // (mirroring ExerciseRow's group "+ adjust"/`ex.adj` badge on the one-week
  // path); clicking it hands (row, cell) up so DesignerRoot can open the
  // shared override editor scoped to that cell.
  group: GroupIdentity | null;
  onOpenOverride(row: GridRow, cell: GridCell): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string): void;
  onAddExercise(day: GridDay): void;
  onRemoveExercise(exerciseSlotId: Id): void;
  onAddDay(): void;
  onRemoveDay(day: GridDay): void;
  onAddWeek(): void;
  onRemoveWeek(weekId: Id): void;
  onSetCurrentWeek(weekId: Id): void;
  onUndo(): void;
  onRedo(): void;
  // P2 exceptions: one-week skip/swap + numbers fill-across-weeks + a
  // this-week-only add — CONTRACT.md "MesoTable.tsx — new props".
  onSkipCell(cellId: number, skipped: boolean): void;
  onSwapCell(cellId: number, swapName: string): void;
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
// WeekGrid.tsx's "day-"/"ex-" prefix convention). A day drag targets only
// day containers; a row drag targets only ROW containers of its OWN day —
// cross-day row moves are OUT of scope for A2 (decisions 5/7), enforced
// here at the collision-filter layer (and independently again inside
// useTableReorder's onDragEnd). Unlike WeekGrid's exercise-active filter
// (which keeps day containers too, for the one-week grid's exercise-over-
// day append path), the table has no cross-type drop target at all in A2.
export function filterTableDragCandidates<T extends { id: unknown }>(activeId: unknown, containers: T[]): T[] {
  const activeIdStr = String(activeId);
  if (activeIdStr.startsWith("day-")) {
    return containers.filter((c) => String(c.id).startsWith("day-"));
  }
  const daySlotId = activeIdStr.split("-")[1];
  return containers.filter((c) => String(c.id).startsWith(`row-${daySlotId}-`));
}

// Same type/scope filtering at the collision layer as
// filterTableDragCandidates above — a lifted day block's center can stay
// closest to its OWN slot (blocks are tall), so `over` never reaches the
// neighbor even when the keyboard getter proposes its coordinates.
export const tableCollisionDetection: CollisionDetection = (args) =>
  closestCenter({
    ...args,
    droppableContainers: filterTableDragCandidates(args.active.id, args.droppableContainers),
  });

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

function draftFrom(cell: GridCell): Record<EditableField, string> {
  return { sets: cell.sets, reps: cell.reps, load: cell.load, rpe: cell.rpe, rest: cell.rest, note: cell.note };
}

interface GridCellEditorProps {
  cell: GridCell;
  unit: string;
  row: GridRow;
  week: GridWeek;
  tableNav: UseTableNavResult;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
}

function GridCellEditor({ cell, unit, row, week, tableNav, onPatchCell }: GridCellEditorProps) {
  const [draft, setDraft] = useState<Record<EditableField, string>>(() => draftFrom(cell));
  // Per-cell (not per-field) dirty set: on commit, only the fields the coach
  // actually typed into are sent — an unconditional blur commit would
  // autosave (and record a no-op undo action for) every field merely tabbed
  // through.
  const dirtyRef = useRef<Set<EditableField>>(new Set());

  // Resync the draft whenever the source of truth changes — our own commit's
  // optimistic update, or an external refetch (undo/redo, another coach
  // action) — never while the coach is mid-edit, since this only runs when
  // these values actually change.
  useEffect(() => {
    setDraft(draftFrom(cell));
    dirtyRef.current = new Set();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cell.sets, cell.reps, cell.load, cell.rpe, cell.rest, cell.note]);

  function changed(field: EditableField, value: string) {
    dirtyRef.current.add(field);
    setDraft((prev) => ({ ...prev, [field]: value }));
  }

  function commitIfDirty() {
    if (dirtyRef.current.size === 0) return;
    const patch: GridCellPatch = {};
    for (const field of dirtyRef.current) {
      patch[field] = draft[field];
    }
    dirtyRef.current = new Set();
    onPatchCell(cell.prescription_id, patch);
  }

  // Escape reverts ONLY the focused field's draft — a per-cell dirty Set
  // needs per-field removal (unlike ExerciseRow's per-row dirtySinceFocus
  // boolean), so a second, still-dirty field on the same cell survives an
  // Escape on its sibling untouched.
  function revertField(field: EditableField, value: string) {
    dirtyRef.current.delete(field);
    setDraft((prev) => ({ ...prev, [field]: value }));
  }

  function toggleLoadType() {
    onPatchCell(cell.prescription_id, { load_type: cell.load_type === "pct" ? "abs" : "pct" });
  }

  // Every field's useTableNav wiring: same callback shape, keyed by field.
  function fieldNavProps(field: EditableField) {
    return tableNav.cellProps(row.exercise_slot_id, week.id, field, {
      onCommit: commitIfDirty,
      onRevert: (value) => revertField(field, value),
    });
  }

  const cellId = cell.prescription_id;

  return (
    <div className="meso-table-cell-editor">
      <div className="meso-table-cell-setsreps">
        <input
          className="meso-cell meso-num-input"
          data-testid={`cell-sets-${cellId}`}
          data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "sets")}
          aria-label={tableCellAriaLabel(row.name, week.label, "sets")}
          value={draft.sets}
          onChange={(e) => changed("sets", e.target.value)}
          onBlur={commitIfDirty}
          {...fieldNavProps("sets")}
        />
        <span className="meso-x-sep">×</span>
        <input
          className="meso-cell meso-num-input"
          data-testid={`cell-reps-${cellId}`}
          data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "reps")}
          aria-label={tableCellAriaLabel(row.name, week.label, "reps")}
          value={draft.reps}
          onChange={(e) => changed("reps", e.target.value)}
          onBlur={commitIfDirty}
          {...fieldNavProps("reps")}
        />
      </div>
      <div className="meso-table-cell-load">
        <input
          className="meso-cell meso-num-input"
          data-testid={`cell-load-${cellId}`}
          data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "load")}
          aria-label={tableCellAriaLabel(row.name, week.label, "load")}
          value={draft.load}
          onChange={(e) => changed("load", e.target.value)}
          onBlur={commitIfDirty}
          {...fieldNavProps("load")}
        />
        <button
          type="button"
          data-testid={`cell-loadtype-${cellId}`}
          className="meso-load-toggle"
          title={cell.load_type === "pct" ? "Load is % of 1RM — tap for absolute" : "Load is absolute — tap for % of 1RM"}
          aria-label={cell.load_type === "pct" ? "Load type: percent of 1RM" : "Load type: absolute"}
          onClick={toggleLoadType}
        >
          {cell.load_type === "pct" ? "%" : unit}
        </button>
      </div>
      <input
        className="meso-cell meso-num-input"
        data-testid={`cell-rpe-${cellId}`}
        data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "rpe")}
        aria-label={tableCellAriaLabel(row.name, week.label, "rpe")}
        value={draft.rpe}
        onChange={(e) => changed("rpe", e.target.value)}
        onBlur={commitIfDirty}
        {...fieldNavProps("rpe")}
      />
      <input
        className="meso-cell meso-num-input"
        data-testid={`cell-rest-${cellId}`}
        data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "rest")}
        aria-label={tableCellAriaLabel(row.name, week.label, "rest")}
        value={draft.rest}
        onChange={(e) => changed("rest", e.target.value)}
        onBlur={commitIfDirty}
        {...fieldNavProps("rest")}
      />
      <input
        className="meso-note"
        data-testid={`cell-note-${cellId}`}
        data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "note")}
        aria-label={tableCellAriaLabel(row.name, week.label, "note")}
        placeholder="—"
        value={draft.note}
        onChange={(e) => changed("note", e.target.value)}
        onBlur={commitIfDirty}
        {...fieldNavProps("note")}
      />
    </div>
  );
}

interface CellActionsProps {
  cell: GridCell;
  busy: boolean;
  onSkipCell(cellId: number, skipped: boolean): void;
  onSwapCell(cellId: number, swapName: string): void;
  onFillAcrossWeeks(cellId: number): void;
}

/** P2 exceptions control cluster for a non-skipped cell — skip / swap
 * (toggle -> inline input -> save, mirroring RowNameEditor/GridCellEditor's
 * commit-on-Enter idiom) / fill-across-weeks (arm -> confirm, mirroring the
 * remove-exercise|day|week arm/confirm pattern above, but scoped locally to
 * this one cell rather than the table-wide `armed` slot since several cells
 * can each have their own swap input or fill-confirm open at once). */
function CellActions({ cell, busy, onSkipCell, onSwapCell, onFillAcrossWeeks }: CellActionsProps) {
  const [swapOpen, setSwapOpen] = useState(false);
  const [swapValue, setSwapValue] = useState("");
  const [fillArmed, setFillArmed] = useState(false);
  const cellId = cell.prescription_id;

  function openSwap() {
    setSwapValue(cell.swap_name);
    setSwapOpen(true);
  }

  function submitSwap() {
    onSwapCell(cellId, swapValue);
    setSwapOpen(false);
  }

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

      {!swapOpen && (
        <button
          type="button"
          data-testid={`cell-swap-btn-${cellId}`}
          className="meso-cell-action-btn"
          disabled={busy}
          aria-label="Swap exercise this week"
          title="Swap exercise this week"
          onClick={openSwap}
        >
          Swap
        </button>
      )}
      {swapOpen && (
        <span className="meso-cell-swap-editor">
          <input
            className="meso-cell meso-swap-input"
            data-testid={`cell-swap-input-${cellId}`}
            aria-label="Swap exercise name"
            value={swapValue}
            onChange={(e) => setSwapValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submitSwap();
              }
            }}
          />
          <button
            type="button"
            data-testid={`cell-swap-save-${cellId}`}
            className="meso-confirm-btn"
            disabled={busy}
            aria-label="Save swap"
            onClick={submitSwap}
          >
            Save
          </button>
        </span>
      )}

      {!fillArmed && (
        <button
          type="button"
          data-testid={`cell-fill-${cellId}`}
          className="meso-cell-action-btn"
          disabled={busy}
          aria-label="Fill numbers across weeks"
          title="Copy this week's numbers to every other week"
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
  weeks: GridWeek[];
  busy: boolean;
  unit: string;
  showAdjust: boolean;
  tableNav: UseTableNavResult;
  rowArmed: boolean;
  onArmRow(): void;
  onConfirmRemoveRow(): void;
  onCancelRemoveRow(): void;
  onOpenOverride(row: GridRow, cell: GridCell): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string): void;
  onSkipCell(cellId: number, skipped: boolean): void;
  onSwapCell(cellId: number, swapName: string): void;
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
  weeks,
  busy,
  unit,
  showAdjust,
  tableNav,
  rowArmed,
  onArmRow,
  onConfirmRemoveRow,
  onCancelRemoveRow,
  onOpenOverride,
  onPatchCell,
  onRenameExercise,
  onSkipCell,
  onSwapCell,
  onFillAcrossWeeks,
}: TableRowProps) {
  const dragData: TableDragData = {
    type: "row",
    daySlotId: day.session_slot_id,
    exerciseSlotId: row.exercise_slot_id,
  };
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useSortable({
    id: `row-${day.session_slot_id}-${row.exercise_slot_id}`,
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
                <GridCellEditor cell={cell} unit={unit} row={row} week={week} tableNav={tableNav} onPatchCell={onPatchCell} />
                {cell.swap_display && (
                  <span
                    className="meso-table-swap-badge"
                    data-testid={`cell-swap-${cell.prescription_id}`}
                    title={"Swapped for " + cell.swap_display + " this week"}
                  >
                    {cell.swap_display}
                    <button
                      type="button"
                      data-testid={`cell-swap-clear-${cell.prescription_id}`}
                      className="meso-swap-badge-clear"
                      disabled={busy}
                      aria-label="Clear swap"
                      title="Clear swap"
                      onClick={() => onSwapCell(cell.prescription_id, "")}
                    >
                      ×
                    </button>
                  </span>
                )}
                {showAdjust && (
                  <button
                    type="button"
                    data-testid={`cell-override-badge-${cell.prescription_id}`}
                    data-hover="brighten"
                    className="meso-adjust-badge"
                    onClick={() => onOpenOverride(row, cell)}
                    title={
                      cell.adj
                        ? (cell.adjusts || []).map((a) => (a.name || "") + ": " + (a.label || "")).join("\n")
                        : "Set a per-athlete adjust"
                    }
                  >
                    {cell.adj ? cell.adj : <span className="meso-adjust-empty">+ adjust</span>}
                  </button>
                )}
                <CellActions
                  cell={cell}
                  busy={busy}
                  onSkipCell={onSkipCell}
                  onSwapCell={onSwapCell}
                  onFillAcrossWeeks={onFillAcrossWeeks}
                />
              </>
            )}
          </td>
        );
      })}
    </tr>
  );
}

interface TableDayBlockProps {
  day: GridDay;
  weeks: GridWeek[];
  busy: boolean;
  unit: string;
  showAdjust: boolean;
  tableNav: UseTableNavResult;
  isArmed(type: ArmedKind, id: Id): boolean;
  arm(type: ArmedKind, id: Id): void;
  disarm(): void;
  onOpenOverride(row: GridRow, cell: GridCell): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string): void;
  onAddExercise(day: GridDay): void;
  onRemoveExercise(exerciseSlotId: Id): void;
  onRemoveDay(day: GridDay): void;
  onSetCurrentWeek(weekId: Id): void;
  onRemoveWeek(weekId: Id): void;
  onSkipCell(cellId: number, skipped: boolean): void;
  onSwapCell(cellId: number, swapName: string): void;
  onFillAcrossWeeks(cellId: number): void;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
}

/** Issue #455 phase A2: one training day's table — now also a dnd-kit
 * sortable item within the block's own day-strip SortableContext (mirrors
 * DayCard.tsx). Same no-live-transform rule as TableRow above — only
 * `.is-dragging` opacity, no CSS.Transform on the block itself. */
function TableDayBlock({
  day,
  weeks,
  busy,
  unit,
  showAdjust,
  tableNav,
  isArmed,
  arm,
  disarm,
  onOpenOverride,
  onPatchCell,
  onRenameExercise,
  onAddExercise,
  onRemoveExercise,
  onRemoveDay,
  onSetCurrentWeek,
  onRemoveWeek,
  onSkipCell,
  onSwapCell,
  onFillAcrossWeeks,
  onAddExerciseThisWeek,
}: TableDayBlockProps) {
  const dayArmed = isArmed("day", day.session_slot_id);
  const dragData: TableDragData = { type: "day", sessionSlotId: day.session_slot_id };
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useSortable({
    id: `day-${day.session_slot_id}`,
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
              {weeks.map((week) => (
                <WeekColumnHeader
                  key={week.id}
                  week={week}
                  armed={isArmed("week", week.id)}
                  busy={busy}
                  onArm={() => arm("week", week.id)}
                  onDisarm={disarm}
                  onSetCurrentWeek={onSetCurrentWeek}
                  onRemoveWeek={onRemoveWeek}
                />
              ))}
            </tr>
          </thead>
          <tbody>
            <SortableContext
              items={day.rows.map((r) => `row-${day.session_slot_id}-${r.exercise_slot_id}`)}
              strategy={verticalListSortingStrategy}
            >
              {day.rows.map((row) => (
                <TableRow
                  key={row.exercise_slot_id}
                  row={row}
                  day={day}
                  weeks={weeks}
                  busy={busy}
                  unit={unit}
                  showAdjust={showAdjust}
                  tableNav={tableNav}
                  rowArmed={isArmed("exercise", row.exercise_slot_id)}
                  onArmRow={() => arm("exercise", row.exercise_slot_id)}
                  onConfirmRemoveRow={() => {
                    onRemoveExercise(row.exercise_slot_id);
                    disarm();
                  }}
                  onCancelRemoveRow={disarm}
                  onOpenOverride={onOpenOverride}
                  onPatchCell={onPatchCell}
                  onRenameExercise={onRenameExercise}
                  onSkipCell={onSkipCell}
                  onSwapCell={onSwapCell}
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
    unit,
    group,
    onOpenOverride,
    onPatchCell,
    onRenameExercise,
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
    onSwapCell,
    onFillAcrossWeeks,
    onAddExerciseThisWeek,
    onDragEnd,
  } = props;

  const [armed, setArmed] = useState<Armed>(null);
  const isArmed = (type: ArmedKind, id: Id) => !!armed && armed.type === type && armed.id === id;
  const arm = (type: ArmedKind, id: Id) => setArmed({ type, id });
  const disarm = () => setArmed(null);

  // P5 group: the per-cell adjust badge only exists on a GROUP plan with
  // members — an individual plan carries no `group`, so no cell ever shows it.
  const showAdjust = !!(group && group.members.length);

  // Rules of Hooks: called unconditionally, before the `!grid` early return
  // below — useTableNav tolerates a null grid the same way (anchor stays
  // null, no throw).
  const tableNav = useTableNav({ grid });

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
        <SortableContext items={grid.days.map((d) => `day-${d.session_slot_id}`)} strategy={verticalListSortingStrategy}>
          {grid.days.map((day) => (
            <TableDayBlock
              key={day.session_slot_id}
              day={day}
              weeks={grid.weeks}
              busy={busy}
              unit={unit}
              showAdjust={showAdjust}
              tableNav={tableNav}
              isArmed={isArmed}
              arm={arm}
              disarm={disarm}
              onOpenOverride={onOpenOverride}
              onPatchCell={onPatchCell}
              onRenameExercise={onRenameExercise}
              onAddExercise={onAddExercise}
              onRemoveExercise={onRemoveExercise}
              onRemoveDay={onRemoveDay}
              onSetCurrentWeek={onSetCurrentWeek}
              onRemoveWeek={onRemoveWeek}
              onSkipCell={onSkipCell}
              onSwapCell={onSwapCell}
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
  onArm(): void;
  onDisarm(): void;
  onSetCurrentWeek(weekId: Id): void;
  onRemoveWeek(weekId: Id): void;
}

function WeekColumnHeader({ week, armed, busy, onArm, onDisarm, onSetCurrentWeek, onRemoveWeek }: WeekColumnHeaderProps) {
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
      {!week.current && (
        <div className="meso-table-week-controls">
          <button
            type="button"
            data-testid={`make-current-${week.id}`}
            data-grid-restore=""
            className="meso-week-strip-btn meso-week-strip-btn--accent"
            disabled={busy}
            title="Make this the live week — delivery will send it"
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

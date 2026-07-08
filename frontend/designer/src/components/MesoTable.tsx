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
// P1: deferred (see docs/meso/fixed-selection-plan.md) — dnd-kit drag
// reordering, keyboard grid-nav (useGridNav), in-cell group override editor /
// one-rm editor, agent-chat wiring, coachmarks. Swap/skip/add-this-week
// WRITE UX is P2 — this file only ever DISPLAYS swap_name/skipped.
import { useEffect, useRef, useState } from "react";
import type { GridCell, GridDay, GridHistory, GridRow, GridWeek, MesoGrid } from "../lib/api";
import type { GridCellPatch, Id } from "../hooks/useGrid";

export interface MesoTableProps {
  grid: MesoGrid | null;
  history: GridHistory;
  busy: boolean;
  unit: string;
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
}

/** The single arm/confirm slot — mirrors usePlanData's PendingDelete
 * (one thing armed at a time), but kept local to MesoTable since useGrid's
 * remove verbs fire the mutation directly with no confirm step of their own. */
type ArmedKind = "exercise" | "day" | "week";
type Armed = { type: ArmedKind; id: Id } | null;

const EDITABLE_FIELDS = ["sets", "reps", "load", "rpe", "rest", "note"] as const;
type EditableField = (typeof EDITABLE_FIELDS)[number];

function draftFrom(cell: GridCell): Record<EditableField, string> {
  return { sets: cell.sets, reps: cell.reps, load: cell.load, rpe: cell.rpe, rest: cell.rest, note: cell.note };
}

interface GridCellEditorProps {
  cell: GridCell;
  unit: string;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
}

function GridCellEditor({ cell, unit, onPatchCell }: GridCellEditorProps) {
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

  function onKeyDownField(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitIfDirty();
    }
  }

  function toggleLoadType() {
    onPatchCell(cell.prescription_id, { load_type: cell.load_type === "pct" ? "abs" : "pct" });
  }

  const cellId = cell.prescription_id;

  return (
    <div className="meso-table-cell-editor">
      <div className="meso-table-cell-setsreps">
        <input
          className="meso-cell meso-num-input"
          data-testid={`cell-sets-${cellId}`}
          aria-label="sets"
          value={draft.sets}
          onChange={(e) => changed("sets", e.target.value)}
          onBlur={commitIfDirty}
          onKeyDown={onKeyDownField}
        />
        <span className="meso-x-sep">×</span>
        <input
          className="meso-cell meso-num-input"
          data-testid={`cell-reps-${cellId}`}
          aria-label="reps"
          value={draft.reps}
          onChange={(e) => changed("reps", e.target.value)}
          onBlur={commitIfDirty}
          onKeyDown={onKeyDownField}
        />
      </div>
      <div className="meso-table-cell-load">
        <input
          className="meso-cell meso-num-input"
          data-testid={`cell-load-${cellId}`}
          aria-label="load"
          value={draft.load}
          onChange={(e) => changed("load", e.target.value)}
          onBlur={commitIfDirty}
          onKeyDown={onKeyDownField}
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
        aria-label="RPE"
        value={draft.rpe}
        onChange={(e) => changed("rpe", e.target.value)}
        onBlur={commitIfDirty}
        onKeyDown={onKeyDownField}
      />
      <input
        className="meso-cell meso-num-input"
        data-testid={`cell-rest-${cellId}`}
        aria-label="rest"
        value={draft.rest}
        onChange={(e) => changed("rest", e.target.value)}
        onBlur={commitIfDirty}
        onKeyDown={onKeyDownField}
      />
      <input
        className="meso-note"
        data-testid={`cell-note-${cellId}`}
        aria-label="note"
        placeholder="—"
        value={draft.note}
        onChange={(e) => changed("note", e.target.value)}
        onBlur={commitIfDirty}
        onKeyDown={onKeyDownField}
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
  onRename(exerciseSlotId: Id, name: string): void;
}

function RowNameEditor({ row, onRename }: RowNameEditorProps) {
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

  return (
    <input
      className="meso-cell meso-ex-name-input"
      data-testid={`row-name-${row.exercise_slot_id}`}
      aria-label="exercise name"
      value={value}
      onChange={(e) => {
        dirtyRef.current = true;
        setValue(e.target.value);
      }}
      onBlur={commitIfDirty}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commitIfDirty();
        }
      }}
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

export function MesoTable(props: MesoTableProps) {
  const {
    grid,
    history,
    busy,
    unit,
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
  } = props;

  const [armed, setArmed] = useState<Armed>(null);
  const isArmed = (type: ArmedKind, id: Id) => !!armed && armed.type === type && armed.id === id;
  const disarm = () => setArmed(null);

  if (!grid) return null;

  return (
    <div className="meso-table-view" data-testid="meso-table-view">
      <div className="meso-table-toolbar">
        <button
          type="button"
          data-testid="grid-undo"
          data-hover="rail"
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
          className="meso-week-strip-btn meso-week-strip-btn--dashed"
          disabled={busy}
          onClick={onAddWeek}
        >
          + Add week
        </button>
      </div>

      {grid.days.map((day) => {
        const dayArmed = isArmed("day", day.session_slot_id);
        return (
          <div className="meso-table-day" key={day.session_slot_id}>
            <div className="meso-table-day-header">
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
                  onClick={() => setArmed({ type: "day", id: day.session_slot_id })}
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
                    {grid.weeks.map((week) => (
                      <WeekColumnHeader
                        key={week.id}
                        week={week}
                        armed={isArmed("week", week.id)}
                        busy={busy}
                        onArm={() => setArmed({ type: "week", id: week.id })}
                        onDisarm={disarm}
                        onSetCurrentWeek={onSetCurrentWeek}
                        onRemoveWeek={onRemoveWeek}
                      />
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {day.rows.map((row) => {
                    const rowArmed = isArmed("exercise", row.exercise_slot_id);
                    return (
                      <tr key={row.exercise_slot_id} data-testid={`meso-row-${row.exercise_slot_id}`}>
                        <td className="meso-table-row-name-col">
                          <RowNameEditor row={row} onRename={onRenameExercise} />
                          {!rowArmed && (
                            <button
                              type="button"
                              data-testid={`remove-exercise-${row.exercise_slot_id}`}
                              className="meso-remove-x meso-remove-x--sm"
                              disabled={busy}
                              aria-label="Remove exercise"
                              title="Remove exercise"
                              onClick={() => setArmed({ type: "exercise", id: row.exercise_slot_id })}
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
                                onClick={() => {
                                  onRemoveExercise(row.exercise_slot_id);
                                  disarm();
                                }}
                              >
                                Confirm?
                              </button>
                              <button
                                type="button"
                                data-testid={`cancel-remove-exercise-${row.exercise_slot_id}`}
                                className="meso-cancel-btn"
                                disabled={busy}
                                aria-label="Cancel remove exercise"
                                onClick={disarm}
                              >
                                Cancel
                              </button>
                            </span>
                          )}
                        </td>
                        {grid.weeks.map((week) => {
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
                                  <GridCellEditor cell={cell} unit={unit} onPatchCell={onPatchCell} />
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
                  })}
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
              <AddThisWeekControl
                day={day}
                weeks={grid.weeks}
                busy={busy}
                onAddExerciseThisWeek={onAddExerciseThisWeek}
              />
            </div>
          </div>
        );
      })}

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

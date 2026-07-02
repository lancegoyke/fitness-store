// ExerciseRow (CONTRACT.md "ExerciseRow") — ported 1:1 from designer.html's
// exercise grid row (lines ~410-458): controlled cells (onChange ->
// onFieldChange, onBlur -> onCommit), load-type toggle, group adjust badge,
// individual %1RM badge/inline editor, remove ×.
import { useRef } from "react";
import { loadSuffix } from "../lib/grid";
import type { Exercise } from "../lib/api";
import type { OneRmEditorState } from "../hooks/useOneRmEditor";
import { cellAriaLabel, gridCellDomKey } from "../hooks/useGridNav";
import type { GridCellBindings, GridCellCallbacks, GridColumn, UseGridNavResult } from "../hooks/useGridNav";

// Phase 3 (grid keyboard navigation): a cell with no gridNav wired up (or no
// gridNav prop at all) falls back to a harmless, non-tabbable, inert
// binding — never crashes, never intercepts a key. a11y (data-grid-cell /
// aria-label) stays unconditional regardless (see below).
const INERT_GRID_BINDINGS: GridCellBindings = { tabIndex: -1, onFocus: () => {}, onKeyDown: () => {} };

export interface ExerciseRowProps {
  ex: Exercise;
  dayIndex: number;
  exIndex: number;
  isGroup: boolean;
  unit: string;
  oneRmOpenForRow: boolean;
  oneRmEditorState: OneRmEditorState | null;
  // CONTRACT.md's prop list omits `deleting`, but its own prose says the
  // remove × is `disabled={deleting}` — and the source (designer.html line
  // ~440) binds `:disabled="deleting"` on that button. Resolved toward the
  // source: ExerciseRow takes a `deleting` prop (see ExerciseRow.test.tsx's
  // note).
  deleting: boolean;
  // Phase 3: optional passthrough from WeekGrid (via DayCard). Absent in
  // DayCard.test.tsx's existing fixtures — see useGridNav.test.tsx's header.
  gridNav?: UseGridNavResult;
  onFieldChange(field: keyof Exercise, value: string): void;
  onCommit(): void;
  onRemove(): void;
  onToggleLoadType(): void;
  onOpenOverride(): void;
  onOpenOneRm(): void;
  onOneRmChange(value: string): void;
  onOneRmSave(): void;
  onOneRmCancel(): void;
}

export function ExerciseRow(props: ExerciseRowProps) {
  const {
    ex,
    isGroup,
    unit,
    oneRmOpenForRow,
    oneRmEditorState,
    deleting,
    gridNav,
    onFieldChange,
    onCommit,
    onRemove,
    onToggleLoadType,
    onOpenOverride,
    onOpenOneRm,
    onOneRmChange,
    onOneRmSave,
    onOneRmCancel,
  } = props;

  const showOneRm = !isGroup && ex.load_type === "pct";

  // Native @change parity: commit on blur only if the coach actually typed in
  // a cell since focusing it — an unconditional blur commit would autosave
  // (and record a no-op undo action for) every cell merely tabbed through.
  const dirtySinceFocus = useRef(false);
  const changed = (field: keyof Exercise, value: string) => {
    dirtySinceFocus.current = true;
    onFieldChange(field, value);
  };
  const commitIfDirty = () => {
    if (!dirtySinceFocus.current) return;
    dirtySinceFocus.current = false;
    onCommit();
  };
  // Phase 3 Escape: revert writes through the RAW onFieldChange (bypassing
  // `changed`'s dirtying) and clears the dirty flag, so a subsequent blur
  // doesn't re-commit the draft the coach just backed out of.
  const revert = (field: keyof Exercise, value: string) => {
    dirtySinceFocus.current = false;
    onFieldChange(field, value);
  };

  // Every grid cell's gridNav wiring: same callback shape, keyed by column.
  // `column` doubles as the Exercise field name (GridColumn's members are a
  // subset of keyof Exercise by construction).
  function gridCellBindings(column: GridColumn): GridCellBindings {
    const callbacks: GridCellCallbacks = {
      onChange: (value) => changed(column, value),
      onCommit: commitIfDirty,
      onRevert: (value) => revert(column, value),
    };
    return gridNav ? gridNav.cellProps(ex.id, column, callbacks) : INERT_GRID_BINDINGS;
  }

  return (
    <div className="meso-ex-row">
      <div className="meso-ex-name-col">
        <input
          className="meso-cell meso-ex-name-input"
          data-testid={`exercise-name-${ex.id}`}
          data-grid-cell={gridCellDomKey(ex.id, "name")}
          aria-label={cellAriaLabel(ex.name, "name")}
          value={ex.name}
          onChange={(e) => changed("name", e.target.value)}
          onBlur={commitIfDirty}
          {...gridCellBindings("name")}
        />
        <div className="meso-ex-tags">
          {ex.tag && <span className="meso-tag-chip">{ex.tag}</span>}
          {ex.last && <span className="meso-last-chip">{"last: " + ex.last}</span>}

          {showOneRm && !oneRmOpenForRow && (
            <button
              type="button"
              data-testid={`one-rm-badge-${ex.id}`}
              data-hover="brighten"
              className={`meso-onerm-badge${ex.one_rm ? " meso-onerm-badge--set" : ""}`}
              onClick={onOpenOneRm}
              title={
                ex.one_rm
                  ? ex.one_rm_source === "manual"
                    ? "Athlete's 1RM (manually set) — tap to edit"
                    : "Athlete's estimated 1RM, from their logged history — tap to edit"
                  : "Set this athlete's 1RM so the % target resolves to a load"
              }
            >
              {ex.one_rm
                ? (ex.one_rm_source === "manual" ? "1RM: " : "1RM ≈ ") + ex.one_rm + (unit ? " " + unit : "")
                : "+ set 1RM"}
            </button>
          )}
          {showOneRm && oneRmOpenForRow && oneRmEditorState && (
            <span className="meso-onerm-editor">
              <input
                data-testid={`one-rm-input-${ex.id}`}
                className="meso-onerm-input"
                type="text"
                inputMode="decimal"
                placeholder={unit}
                value={oneRmEditorState.value}
                onChange={(e) => onOneRmChange(e.target.value)}
                onKeyDown={(e) => {
                  // Parity with the Alpine editor's @keydown.enter.prevent /
                  // @keydown.escape.prevent.
                  if (e.key === "Enter") {
                    e.preventDefault();
                    onOneRmSave();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    onOneRmCancel();
                  }
                }}
              />
              <button
                type="button"
                data-testid={`one-rm-save-${ex.id}`}
                data-hover="brighten"
                className="meso-onerm-save"
                disabled={oneRmEditorState.saving}
                onClick={onOneRmSave}
              >
                save
              </button>
              <button
                type="button"
                data-testid={`one-rm-cancel-${ex.id}`}
                data-hover="brighten"
                className="meso-onerm-cancel"
                disabled={oneRmEditorState.saving}
                title="Cancel"
                onClick={onOneRmCancel}
              >
                ×
              </button>
              {oneRmEditorState.error && (
                <span data-testid={`one-rm-error-${ex.id}`} className="meso-onerm-error">
                  {oneRmEditorState.error}
                </span>
              )}
            </span>
          )}

          {isGroup && (
            <button
              type="button"
              data-testid={`override-badge-${ex.id}`}
              data-hover="brighten"
              className="meso-adjust-badge"
              onClick={onOpenOverride}
              title={ex.adj ? (ex.adjusts || []).map((a) => a.name + ": " + a.label).join("\n") : "Set a per-athlete adjust"}
            >
              {ex.adj ? ex.adj : <span className="meso-adjust-empty">+ adjust</span>}
            </button>
          )}

          <button
            type="button"
            data-testid={`exercise-remove-${ex.id}`}
            data-hover="rail"
            className="meso-remove-x meso-remove-x--sm"
            aria-label="Remove exercise"
            title="Remove exercise"
            disabled={deleting}
            onClick={onRemove}
          >
            ×
          </button>
        </div>
      </div>

      <div className="meso-ex-setsreps">
        <input
          className="meso-cell meso-num-input meso-num-input--sets"
          data-testid={`exercise-sets-${ex.id}`}
          data-grid-cell={gridCellDomKey(ex.id, "sets")}
          aria-label={cellAriaLabel(ex.name, "sets")}
          value={ex.sets}
          onChange={(e) => changed("sets", e.target.value)}
          onBlur={commitIfDirty}
          {...gridCellBindings("sets")}
        />
        <span className="meso-x-sep">×</span>
        <input
          className="meso-cell meso-num-input meso-num-input--reps"
          data-testid={`exercise-reps-${ex.id}`}
          data-grid-cell={gridCellDomKey(ex.id, "reps")}
          aria-label={cellAriaLabel(ex.name, "reps")}
          value={ex.reps}
          onChange={(e) => changed("reps", e.target.value)}
          onBlur={commitIfDirty}
          {...gridCellBindings("reps")}
        />
      </div>

      <div className="meso-ex-load">
        <input
          className="meso-cell meso-num-input meso-num-input--load"
          data-testid={`exercise-load-${ex.id}`}
          data-grid-cell={gridCellDomKey(ex.id, "load")}
          aria-label={cellAriaLabel(ex.name, "load")}
          value={ex.load}
          onChange={(e) => changed("load", e.target.value)}
          onBlur={commitIfDirty}
          {...gridCellBindings("load")}
        />
        <button
          type="button"
          data-testid={`exercise-load-type-${ex.id}`}
          className="meso-load-toggle"
          title={ex.load_type === "pct" ? "Load is % of 1RM — tap for absolute" : "Load is absolute — tap for % of 1RM"}
          aria-label={ex.load_type === "pct" ? "Load type: percent of 1RM" : "Load type: absolute"}
          onClick={onToggleLoadType}
        >
          {loadSuffix(ex, unit)}
        </button>
      </div>

      <div className="meso-ex-rpe">
        <input
          className="meso-cell meso-num-input meso-num-input--rpe"
          data-testid={`exercise-rpe-${ex.id}`}
          data-grid-cell={gridCellDomKey(ex.id, "rpe")}
          aria-label={cellAriaLabel(ex.name, "rpe")}
          value={ex.rpe ?? ""}
          onChange={(e) => changed("rpe", e.target.value)}
          onBlur={commitIfDirty}
          {...gridCellBindings("rpe")}
        />
      </div>

      <div className="meso-ex-note">
        <input
          className="meso-note"
          data-testid={`exercise-note-${ex.id}`}
          data-grid-cell={gridCellDomKey(ex.id, "note")}
          aria-label={cellAriaLabel(ex.name, "note")}
          value={ex.note ?? ""}
          placeholder="—"
          onChange={(e) => changed("note", e.target.value)}
          onBlur={commitIfDirty}
          {...gridCellBindings("note")}
        />
      </div>
    </div>
  );
}

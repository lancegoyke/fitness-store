// ExerciseRow (CONTRACT.md "ExerciseRow") — ported 1:1 from designer.html's
// exercise grid row (lines ~410-458): controlled cells (onChange ->
// onFieldChange, onBlur -> onCommit), load-type toggle, group adjust badge,
// individual %1RM badge/inline editor, remove ×.
import { loadSuffix } from "../lib/grid";
import type { Exercise } from "../lib/api";
import type { OneRmEditorState } from "../hooks/useOneRmEditor";

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

  return (
    <div className="meso-ex-row">
      <div className="meso-ex-name-col">
        <input
          className="meso-cell meso-ex-name-input"
          data-testid={`exercise-name-${ex.id}`}
          value={ex.name}
          onChange={(e) => onFieldChange("name", e.target.value)}
          onBlur={onCommit}
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
          value={ex.sets}
          onChange={(e) => onFieldChange("sets", e.target.value)}
          onBlur={onCommit}
        />
        <span className="meso-x-sep">×</span>
        <input
          className="meso-cell meso-num-input meso-num-input--reps"
          data-testid={`exercise-reps-${ex.id}`}
          value={ex.reps}
          onChange={(e) => onFieldChange("reps", e.target.value)}
          onBlur={onCommit}
        />
      </div>

      <div className="meso-ex-load">
        <input
          className="meso-cell meso-num-input meso-num-input--load"
          data-testid={`exercise-load-${ex.id}`}
          value={ex.load}
          onChange={(e) => onFieldChange("load", e.target.value)}
          onBlur={onCommit}
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
          value={ex.rpe ?? ""}
          onChange={(e) => onFieldChange("rpe", e.target.value)}
          onBlur={onCommit}
        />
      </div>

      <div className="meso-ex-note">
        <input
          className="meso-note"
          data-testid={`exercise-note-${ex.id}`}
          value={ex.note ?? ""}
          placeholder="—"
          onChange={(e) => onFieldChange("note", e.target.value)}
          onBlur={onCommit}
        />
      </div>
    </div>
  );
}

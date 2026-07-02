// DayCard (CONTRACT.md "DayCard") — ported 1:1 from designer.html's day card
// (lines ~391-461): day header (name/bias/count + remove-day arm/confirm/
// cancel), column headers, one ExerciseRow per exercise, "+ Add exercise".
import { ExerciseRow } from "./ExerciseRow";
import type { Day, Exercise } from "../lib/api";
import type { PendingDelete } from "../hooks/usePlanData";
import type { OneRmEditorState } from "../hooks/useOneRmEditor";

export interface DayCardProps {
  day: Day;
  dayIndex: number;
  isGroup: boolean;
  unit: string;
  pendingDelete: PendingDelete | null;
  deleting: boolean;
  onRequestRemoveDay(di: number): void;
  onConfirmPendingDelete(): void;
  onCancelPendingDelete(): void;
  onAddExercise(dayIndex: number): void;
  onFieldChange(dayIndex: number, exIndex: number, field: keyof Exercise, value: string): void;
  onCommit(dayIndex: number, exIndex: number): void;
  onRemoveExercise(dayIndex: number, exIndex: number): void;
  onToggleLoadType(ex: Exercise): void;
  onOpenOverride(ex: Exercise): void;
  onOpenOneRm(ex: Exercise): void;
  onOneRmChange(value: string): void;
  onOneRmSave(): void;
  onOneRmCancel(): void;
  oneRmOpenForRow(ex: Exercise): boolean;
  oneRmEditorState: OneRmEditorState | null;
}

export function DayCard(props: DayCardProps) {
  const {
    day,
    dayIndex,
    isGroup,
    unit,
    pendingDelete,
    deleting,
    onRequestRemoveDay,
    onConfirmPendingDelete,
    onCancelPendingDelete,
    onAddExercise,
    onFieldChange,
    onCommit,
    onRemoveExercise,
    onToggleLoadType,
    onOpenOverride,
    onOpenOneRm,
    onOneRmChange,
    onOneRmSave,
    onOneRmCancel,
    oneRmOpenForRow,
    oneRmEditorState,
  } = props;

  const armed = !!pendingDelete && pendingDelete.type === "day" && pendingDelete.di === dayIndex;

  return (
    <div className="meso-day-card">
      <div className="meso-day-header">
        <div className="meso-day-badge">{day.n}</div>
        <div className="meso-day-name">{day.name}</div>
        {day.bias && <div className="meso-day-bias">{day.bias}</div>}
        <div className="meso-flex-spacer" />
        <div className="meso-day-count">
          {day.exercises.length + (day.exercises.length === 1 ? " exercise" : " exercises")}
        </div>
        {!armed && (
          <button
            type="button"
            data-testid={`remove-day-${day.id}`}
            data-hover="rail"
            className="meso-remove-x"
            aria-label="Remove this day"
            title="Remove this day"
            disabled={deleting}
            onClick={() => onRequestRemoveDay(dayIndex)}
          >
            ×
          </button>
        )}
        {armed && (
          <span className="meso-flex meso-confirm-pair">
            <button
              type="button"
              data-testid={`confirm-remove-day-${day.id}`}
              className="meso-confirm-btn"
              disabled={deleting}
              aria-label="Confirm remove day"
              onClick={onConfirmPendingDelete}
            >
              Confirm?
            </button>
            <button
              type="button"
              data-testid={`cancel-remove-day-${day.id}`}
              className="meso-cancel-btn"
              disabled={deleting}
              aria-label="Cancel remove day"
              onClick={onCancelPendingDelete}
            >
              Cancel
            </button>
          </span>
        )}
      </div>
      <div className="meso-col-headers">
        <div>Exercise</div>
        <div className="meso-col-center">Sets × Reps</div>
        <div className="meso-col-center">Load</div>
        <div className="meso-col-center">RPE</div>
        <div>Notes</div>
      </div>
      {day.exercises.map((ex, xi) => (
        <ExerciseRow
          key={ex.id}
          ex={ex}
          dayIndex={dayIndex}
          exIndex={xi}
          isGroup={isGroup}
          unit={unit}
          deleting={deleting}
          oneRmOpenForRow={oneRmOpenForRow(ex)}
          oneRmEditorState={oneRmEditorState}
          onFieldChange={(field, value) => onFieldChange(dayIndex, xi, field, value)}
          onCommit={() => onCommit(dayIndex, xi)}
          onRemove={() => onRemoveExercise(dayIndex, xi)}
          onToggleLoadType={() => onToggleLoadType(ex)}
          onOpenOverride={() => onOpenOverride(ex)}
          onOpenOneRm={() => onOpenOneRm(ex)}
          onOneRmChange={onOneRmChange}
          onOneRmSave={onOneRmSave}
          onOneRmCancel={onOneRmCancel}
        />
      ))}
      <button
        type="button"
        data-hover="add"
        className="meso-add-row"
        data-testid={`add-exercise-${day.id}`}
        onClick={() => onAddExercise(dayIndex)}
      >
        + Add exercise
      </button>
    </div>
  );
}

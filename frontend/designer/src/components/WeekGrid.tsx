// WeekGrid (CONTRACT.md "WeekGrid / DayCard / ExerciseRow") — ported 1:1 from
// designer.html's week view (lines ~334-466). Mounts WeekStrip at the top,
// the grid coachmark, the group-mode banner, one DayCard per program day,
// and "+ Add day". See WeekGrid.test.tsx's "contract gap" note: the prop
// list below includes the DayCard/ExerciseRow passthrough + the coachmark
// hook slice that CONTRACT.md's prose says are wired in but its formal prop
// list omitted.
import { DayCard } from "./DayCard";
import { WeekStrip } from "./WeekStrip";
import type { Day, Exercise, HistoryState, Week } from "../lib/api";
import type { Id, PendingDelete } from "../hooks/usePlanData";
import type { OneRmEditorState } from "../hooks/useOneRmEditor";
import { useGridNav } from "../hooks/useGridNav";

export interface WeekGridProps {
  program: Day[];
  isGroup: boolean;
  unit: string;
  pendingDelete: PendingDelete | null;
  deleting: boolean;
  onRequestRemoveDay(di: number): void;
  onConfirmPendingDelete(): void;
  onCancelPendingDelete(): void;
  onAddDay(): void;
  // DayCard / ExerciseRow passthrough. `onAddExercise` isn't exercised by any
  // WeekGrid-level spec (WeekGrid.test.tsx's baseProps omits it, same gap as
  // onUndo/onRedo below) — optional here with a no-op fallback so DayCard
  // still gets a real (required) callback either way.
  onAddExercise?(dayIndex: number): void;
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
  // Coachmark hook slice (useCoachmarks).
  coachmarkVisible(key: string): boolean;
  dismissCoachmark(key: string): void;
  // WeekStrip passthrough.
  weeks: Week[];
  viewedWeekId: Id | null;
  viewedIsCurrent: boolean;
  history: HistoryState;
  undoing: boolean;
  onSwitchWeek(weekId: Id): void;
  onAddWeek(): void;
  onMakeCurrent(weekId: Id): void;
  onRequestRemoveWeek(weekId: Id): void;
  onUndo?(): void;
  onRedo?(): void;
}

export function WeekGrid(props: WeekGridProps) {
  const {
    program,
    isGroup,
    unit,
    pendingDelete,
    deleting,
    onRequestRemoveDay,
    onConfirmPendingDelete,
    onCancelPendingDelete,
    onAddDay,
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
    coachmarkVisible,
    dismissCoachmark,
    weeks,
    viewedWeekId,
    viewedIsCurrent,
    history,
    undoing,
    onSwitchWeek,
    onAddWeek,
    onMakeCurrent,
    onRequestRemoveWeek,
    onUndo,
    onRedo,
  } = props;

  // Phase 3: instantiated ONCE here (WeekGrid already receives `program`) —
  // DayCard/ExerciseRow only ever see the result as an optional passthrough
  // prop (see useGridNav.test.tsx's header).
  const gridNav = useGridNav({ program });

  return (
    <div>
      <WeekStrip
        weeks={weeks}
        viewedWeekId={viewedWeekId}
        viewedIsCurrent={viewedIsCurrent}
        pendingDelete={pendingDelete}
        deleting={deleting}
        history={history}
        undoing={undoing}
        onSwitchWeek={onSwitchWeek}
        onAddWeek={onAddWeek}
        onMakeCurrent={onMakeCurrent}
        onRequestRemoveWeek={onRequestRemoveWeek}
        onCancelPendingDelete={onCancelPendingDelete}
        onConfirmPendingDelete={onConfirmPendingDelete}
        onUndo={onUndo}
        onRedo={onRedo}
      />

      {coachmarkVisible("grid") && (
        <div className="meso-flex meso-coachmark">
          <div className="meso-coachmark-body">
            <div className="meso-coachmark-title">The week grid</div>
            <div className="meso-coachmark-text">
              Tap any cell — sets, reps, load, RPE, or notes — to edit it. Every change autosaves.
            </div>
          </div>
          <button
            type="button"
            data-hover="rail"
            className="meso-coachmark-dismiss"
            aria-label="Dismiss tip"
            onClick={() => dismissCoachmark("grid")}
          >
            ×
          </button>
        </div>
      )}

      {isGroup && (
        <div className="meso-group-banner">
          <span className="meso-group-banner-title">Shared program · per-athlete auto-adjusts</span>
          <span className="meso-group-banner-text">
            Every member trains off this program — tap a row's adjust to tune one athlete's load, swap, or volume.
          </span>
        </div>
      )}

      {program.map((day, di) => (
        <DayCard
          key={day.id}
          day={day}
          dayIndex={di}
          isGroup={isGroup}
          unit={unit}
          pendingDelete={pendingDelete}
          deleting={deleting}
          onRequestRemoveDay={onRequestRemoveDay}
          onConfirmPendingDelete={onConfirmPendingDelete}
          onCancelPendingDelete={onCancelPendingDelete}
          onAddExercise={onAddExercise ?? (() => {})}
          gridNav={gridNav}
          onFieldChange={onFieldChange}
          onCommit={onCommit}
          onRemoveExercise={onRemoveExercise}
          onToggleLoadType={onToggleLoadType}
          onOpenOverride={onOpenOverride}
          onOpenOneRm={onOpenOneRm}
          onOneRmChange={onOneRmChange}
          onOneRmSave={onOneRmSave}
          onOneRmCancel={onOneRmCancel}
          oneRmOpenForRow={oneRmOpenForRow}
          oneRmEditorState={oneRmEditorState}
        />
      ))}

      <button type="button" data-hover="add" className="meso-add-day-btn" data-testid="add-day-button" onClick={onAddDay}>
        + Add day
      </button>
    </div>
  );
}

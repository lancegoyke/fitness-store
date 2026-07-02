// WeekGrid (CONTRACT.md "WeekGrid / DayCard / ExerciseRow") — ported 1:1 from
// designer.html's week view (lines ~334-466). Mounts WeekStrip at the top,
// the grid coachmark, the group-mode banner, one DayCard per program day,
// and "+ Add day". See WeekGrid.test.tsx's "contract gap" note: the prop
// list below includes the DayCard/ExerciseRow passthrough + the coachmark
// hook slice that CONTRACT.md's prose says are wired in but its formal prop
// list omitted.
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { DragEndEvent } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy } from "@dnd-kit/sortable";
import type { CollisionDetection, KeyboardCoordinateGetter } from "@dnd-kit/core";
import { DayCard } from "./DayCard";
import { WeekStrip } from "./WeekStrip";
import type { Day, Exercise, HistoryState, Week } from "../lib/api";
import type { Id, PendingDelete } from "../hooks/usePlanData";
import type { OneRmEditorState } from "../hooks/useOneRmEditor";
import { useGridNav } from "../hooks/useGridNav";
import type { ReorderDragData, ReorderDragEndEvent } from "../hooks/useReorder";

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
  // Phase 4 (dnd-kit reordering): useReorder is instantiated in
  // DesignerRoot (where usePlanData's pieces are in scope, like every other
  // hook here) and its handler passed down — optional with a no-op fallback
  // so WeekGrid.test.tsx's existing baseProps() (which never sets it) keeps
  // passing untouched, same pattern as `onAddExercise` above.
  onDragEnd?(event: ReorderDragEndEvent): void | Promise<void>;
  reordering?: boolean;
}

// Keyboard drags with nested sortables: the stock getter proposes the
// closest droppable of ANY type, so a lifted day card keeps targeting
// exercise rows and can never reach the neighboring day (browser-verified).
// Filter the candidates to the active drag's own kind (day-* vs ex-*).
export function filterContainersByActiveType<T extends { id: unknown }>(
  activeId: unknown,
  containers: T[],
): T[] {
  if (String(activeId).startsWith("day-")) {
    // A dragged day only ever lands relative to another day.
    return containers.filter((c) => String(c.id).startsWith("day-"));
  }
  // A dragged exercise keeps day containers too: exercise-over-day is the
  // append/empty-day drop path, and it must stay reachable by keyboard.
  return containers.filter(
    (c) => String(c.id).startsWith("ex-") || String(c.id).startsWith("day-"),
  );
}

// Same type-filtering at the collision layer: a lifted day card's center can
// stay closest to its OWN slot (cards are tall), so `over` never reaches the
// neighbor even when the keyboard getter proposes its coordinates. Day drags
// collide only with day containers; exercise drags keep the full set (rows +
// day cards, for the append-to-day path).
export const typedCollisionDetection: CollisionDetection = (args) => {
  if (String(args.active.id).startsWith("day-")) {
    return closestCenter({
      ...args,
      droppableContainers: filterContainersByActiveType(args.active.id, args.droppableContainers),
    });
  }
  return closestCenter(args);
};

export const typedKeyboardCoordinates: KeyboardCoordinateGetter = (event, args) => {
  const containers = args.context.droppableContainers;
  const filtered = {
    ...args.context,
    droppableContainers: Object.assign(Object.create(Object.getPrototypeOf(containers) ?? {}), containers, {
      getEnabled: () =>
        filterContainersByActiveType(args.context.active?.id ?? "", containers.getEnabled()),
    }),
  };
  return sortableKeyboardCoordinates(event, { ...args, context: filtered });
};

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
    onDragEnd,
    reordering,
  } = props;

  // Phase 3: instantiated ONCE here (WeekGrid already receives `program`) —
  // DayCard/ExerciseRow only ever see the result as an optional passthrough
  // prop (see useGridNav.test.tsx's header).
  const gridNav = useGridNav({ program });

  // Phase 4 (dnd-kit reordering): PointerSensor gets a small activation
  // distance so a plain click into a cell input doesn't start a drag;
  // KeyboardSensor rides the handle buttons' tab-order focus (Space/Enter
  // lifts, arrows move, Space/Enter drops, Escape cancels).
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: typedKeyboardCoordinates }),
  );

  function handleDragEnd(event: DragEndEvent) {
    if (!onDragEnd) return;
    const { active, over } = event;
    void onDragEnd({
      active: { id: active.id, data: { current: active.data.current as ReorderDragData } },
      over: over ? { id: over.id, data: { current: over.data.current as ReorderDragData } } : null,
    });
  }

  return (
    <div aria-busy={reordering || undefined}>
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

      <DndContext sensors={sensors} collisionDetection={typedCollisionDetection} onDragEnd={handleDragEnd}>
        <SortableContext
          items={program.map((day) => `day-${day.id}`)}
          strategy={verticalListSortingStrategy}
        >
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
        </SortableContext>
      </DndContext>

      <button type="button" data-hover="add" className="meso-add-day-btn" data-testid="add-day-button" onClick={onAddDay}>
        + Add day
      </button>
    </div>
  );
}

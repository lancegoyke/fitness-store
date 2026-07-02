// useReorder (Phase 4, dnd-kit reordering) — owns the three drag-and-drop
// verbs (within-day exercise reorder, cross-day exercise move, day-strip
// reorder) per useReorder.test.tsx's header comment, which is the binding
// API contract. Mirrors useDeletes' deletingRef idiom for the shared
// in-flight guard, and usePlanData's switchWeek for the failure re-fetch.
import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { arrayMove } from "@dnd-kit/sortable";
import { apiPost } from "../lib/api";
import type { Day, PlanEnvelope } from "../lib/api";
import type { Id } from "./usePlanData";

/** Carried by every sortable item via dnd-kit's `useSortable({ id, data })` —
 * see contract decision 2 (useReorder.test.tsx). */
export type ReorderDragData =
  | { type: "exercise"; dayId: Id; prescriptionId: Id }
  | { type: "day"; sessionId: Id };

/** Modeled on dnd-kit's real `DragEndEvent` — WeekGrid's `onDragEnd` adapts
 * the real event into this shape before calling the hook's handler. */
export interface ReorderDragEndEvent {
  active: { id: Id; data: { current: ReorderDragData } };
  over: { id: Id; data: { current: ReorderDragData } } | null;
}

export interface UseReorderOptions {
  planId: Id;
  csrf: string;
  viewedWeekId: Id | null;
  program: Day[];
  setProgram: Dispatch<SetStateAction<Day[]>>;
  applyPlanData: (data: PlanEnvelope) => void;
}

export function useReorder(options: UseReorderOptions) {
  const { planId, csrf, viewedWeekId, program, setProgram, applyPlanData } = options;

  // One shared in-flight guard across all three drop shapes (mirrors
  // useDeletes' deletingRef) — checked SYNCHRONOUSLY at the top of
  // onDragEnd so a second drop while one POST is pending is a full no-op.
  const reorderingRef = useRef(false);
  const [reordering, setReordering] = useState(false);

  const setReorderingBoth = useCallback((value: boolean) => {
    reorderingRef.current = value;
    setReordering(value);
  }, []);

  // The CURRENT viewed week, readable at async-resolve time — mirrors
  // usePlanData's viewedWeekIdRef idiom exactly. A reorder POSTed on week A
  // that resolves after the coach has switched to week B must not apply
  // (or re-fetch) week A's data.
  const viewedWeekIdRef = useRef<Id | null>(viewedWeekId);
  viewedWeekIdRef.current = viewedWeekId;

  // Failure path: console.error, then (only if the week captured at drop
  // time is STILL the viewed week) GET it (mirrors usePlanData's
  // switchWeek — no request body/options) and applyPlanData the reply. A
  // failed re-fetch is swallowed with its own console.error. If the coach
  // switched weeks before this runs, the re-fetch is skipped entirely — the
  // now-unviewed week's data isn't worth fetching.
  const refetchWeek = useCallback(
    async (err: unknown, weekAtDrop: Id | null) => {
      console.error("Reorder failed", err);
      if (viewedWeekIdRef.current !== weekAtDrop) return;
      try {
        const res = await fetch(`/meso/api/plan/${planId}/week/${weekAtDrop}/`);
        if (!res.ok) throw new Error("Request failed: " + res.status);
        applyPlanData((await res.json()) as PlanEnvelope);
      } catch (refetchErr) {
        console.error("Reorder refetch failed", refetchErr);
      }
    },
    [planId, applyPlanData],
  );

  const postReorder = useCallback(
    async (url: string, body: unknown) => {
      // Snapshot synchronously at drop time — compared against the ref's
      // (possibly-changed) value once the POST resolves.
      const weekAtDrop = viewedWeekIdRef.current;
      try {
        const data = await apiPost<PlanEnvelope>(url, body, csrf);
        if (viewedWeekIdRef.current === weekAtDrop) {
          applyPlanData(data);
        }
      } catch (err) {
        await refetchWeek(err, weekAtDrop);
      } finally {
        setReorderingBoth(false);
      }
    },
    [csrf, applyPlanData, refetchWeek, setReorderingBoth],
  );

  function withinDayReorder(
    activeData: { type: "exercise"; dayId: Id; prescriptionId: Id },
    overData: { type: "exercise"; dayId: Id; prescriptionId: Id },
  ) {
    const dayIndex = program.findIndex((d) => d.id === activeData.dayId);
    const day = program[dayIndex];
    if (!day) return;
    const oldIndex = day.exercises.findIndex((e) => e.id === activeData.prescriptionId);
    const newIndex = day.exercises.findIndex((e) => e.id === overData.prescriptionId);
    if (oldIndex === -1 || newIndex === -1) return;
    const reordered = arrayMove(day.exercises, oldIndex, newIndex);

    setReorderingBoth(true);
    setProgram((prev) => prev.map((d, di) => (di === dayIndex ? { ...d, exercises: reordered } : d)));
    return postReorder(`/meso/api/plan/${planId}/session/${day.id}/reorder/`, {
      order: reordered.map((e) => e.id),
    });
  }

  function crossDayMove(
    activeData: { type: "exercise"; dayId: Id; prescriptionId: Id },
    overData: { type: "exercise"; dayId: Id; prescriptionId: Id },
  ) {
    const sourceIndex = program.findIndex((d) => d.id === activeData.dayId);
    const targetIndex = program.findIndex((d) => d.id === overData.dayId);
    const sourceDay = program[sourceIndex];
    const targetDay = program[targetIndex];
    if (!sourceDay || !targetDay) return;
    const movedIndex = sourceDay.exercises.findIndex((e) => e.id === activeData.prescriptionId);
    const moved = sourceDay.exercises[movedIndex];
    if (!moved) return;
    // The target's CURRENT (pre-insertion, moved-row-excluded) index of the
    // over row — matches the backend's own insertion semantics exactly.
    const insertIndex = targetDay.exercises.findIndex((e) => e.id === overData.prescriptionId);
    if (insertIndex === -1) return;

    const newSourceExercises = sourceDay.exercises.filter((_, i) => i !== movedIndex);
    const newTargetExercises = [...targetDay.exercises];
    newTargetExercises.splice(insertIndex, 0, moved);

    setReorderingBoth(true);
    setProgram((prev) =>
      prev.map((d, di) => {
        if (di === sourceIndex) return { ...d, exercises: newSourceExercises };
        if (di === targetIndex) return { ...d, exercises: newTargetExercises };
        return d;
      }),
    );
    return postReorder(`/meso/api/plan/${planId}/prescription/${moved.id}/move/`, {
      session_id: targetDay.id,
      index: insertIndex,
    });
  }

  function dayReorder(activeData: { type: "day"; sessionId: Id }, overData: { type: "day"; sessionId: Id }) {
    const oldIndex = program.findIndex((d) => d.id === activeData.sessionId);
    const newIndex = program.findIndex((d) => d.id === overData.sessionId);
    if (oldIndex === -1 || newIndex === -1) return;
    const reordered = arrayMove(program, oldIndex, newIndex);

    setReorderingBoth(true);
    setProgram(reordered);
    return postReorder(`/meso/api/plan/${planId}/week/${viewedWeekId}/reorder/`, {
      order: reordered.map((d) => d.id),
    });
  }

  // Dropping an exercise onto a DAY's own droppable (not a specific row) —
  // an empty day, or past a day's last row where the day card's droppable
  // (not a row's) is closestCenter's pick — appends it to the end of that
  // day. Same-day: a session-reorder with the row moved to the end (no-op
  // if it's already last). Cross-day: prescription-move with `index` = the
  // target's pre-insertion `exercises.length` (the backend clamps, so
  // index === length is a valid append).
  function appendToDay(
    activeData: { type: "exercise"; dayId: Id; prescriptionId: Id },
    overData: { type: "day"; sessionId: Id },
  ) {
    const sourceIndex = program.findIndex((d) => d.id === activeData.dayId);
    const targetIndex = program.findIndex((d) => d.id === overData.sessionId);
    const sourceDay = program[sourceIndex];
    const targetDay = program[targetIndex];
    if (!sourceDay || !targetDay) return;
    const movedIndex = sourceDay.exercises.findIndex((e) => e.id === activeData.prescriptionId);
    const moved = sourceDay.exercises[movedIndex];
    if (!moved) return;

    if (sourceIndex === targetIndex) {
      if (movedIndex === sourceDay.exercises.length - 1) return; // already last: no-op
      const reordered = [...sourceDay.exercises.filter((_, i) => i !== movedIndex), moved];

      setReorderingBoth(true);
      setProgram((prev) => prev.map((d, di) => (di === sourceIndex ? { ...d, exercises: reordered } : d)));
      return postReorder(`/meso/api/plan/${planId}/session/${sourceDay.id}/reorder/`, {
        order: reordered.map((e) => e.id),
      });
    }

    const insertIndex = targetDay.exercises.length; // pre-insertion length == append index
    const newSourceExercises = sourceDay.exercises.filter((_, i) => i !== movedIndex);
    const newTargetExercises = [...targetDay.exercises, moved];

    setReorderingBoth(true);
    setProgram((prev) =>
      prev.map((d, di) => {
        if (di === sourceIndex) return { ...d, exercises: newSourceExercises };
        if (di === targetIndex) return { ...d, exercises: newTargetExercises };
        return d;
      }),
    );
    return postReorder(`/meso/api/plan/${planId}/prescription/${moved.id}/move/`, {
      session_id: targetDay.id,
      index: insertIndex,
    });
  }

  const onDragEnd = useCallback(
    (event: ReorderDragEndEvent): void | Promise<void> => {
      if (reorderingRef.current) return;
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      const activeData = active.data.current;
      const overData = over.data.current;

      if (activeData.type === "exercise" && overData.type === "exercise") {
        if (activeData.dayId === overData.dayId) {
          return withinDayReorder(activeData, overData);
        }
        return crossDayMove(activeData, overData);
      }

      if (activeData.type === "day" && overData.type === "day") {
        return dayReorder(activeData, overData);
      }

      // dnd-kit's closestCenter routinely resolves a day-strip drag onto an
      // exercise row's droppable (rows' centers sit closer to the pointer
      // than the day card's) — resolve to that row's owning day instead of
      // discarding the drop.
      if (activeData.type === "day" && overData.type === "exercise") {
        const resolvedSessionId = overData.dayId;
        if (resolvedSessionId === activeData.sessionId) return; // same day: no-op
        return dayReorder(activeData, { type: "day", sessionId: resolvedSessionId });
      }

      // An exercise dropped on a day's own droppable (no specific row under
      // the pointer) appends it to the end of that day.
      if (activeData.type === "exercise" && overData.type === "day") {
        return appendToDay(activeData, overData);
      }
    },
    [program, planId, viewedWeekId, setProgram, postReorder, setReorderingBoth],
  );

  return { reordering, onDragEnd };
}

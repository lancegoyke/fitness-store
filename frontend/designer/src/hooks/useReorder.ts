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

  // Failure path: console.error, then GET the viewed week (mirrors
  // usePlanData's switchWeek — no request body/options) and applyPlanData
  // the reply. A failed re-fetch is swallowed with its own console.error.
  const refetchWeek = useCallback(
    async (err: unknown) => {
      console.error("Reorder failed", err);
      try {
        const res = await fetch(`/meso/api/plan/${planId}/week/${viewedWeekId}/`);
        if (!res.ok) throw new Error("Request failed: " + res.status);
        applyPlanData((await res.json()) as PlanEnvelope);
      } catch (refetchErr) {
        console.error("Reorder refetch failed", refetchErr);
      }
    },
    [planId, viewedWeekId, applyPlanData],
  );

  const postReorder = useCallback(
    async (url: string, body: unknown) => {
      try {
        const data = await apiPost<PlanEnvelope>(url, body, csrf);
        applyPlanData(data);
      } catch (err) {
        await refetchWeek(err);
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

  const onDragEnd = useCallback(
    (event: ReorderDragEndEvent): void | Promise<void> => {
      if (reorderingRef.current) return;
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      const activeData = active.data.current;
      const overData = over.data.current;
      if (activeData.type !== overData.type) return;

      if (activeData.type === "exercise" && overData.type === "exercise") {
        if (activeData.dayId === overData.dayId) {
          return withinDayReorder(activeData, overData);
        }
        return crossDayMove(activeData, overData);
      }

      if (activeData.type === "day" && overData.type === "day") {
        return dayReorder(activeData, overData);
      }
    },
    [program, planId, viewedWeekId, setProgram, postReorder, setReorderingBoth],
  );

  return { reordering, onDragEnd };
}

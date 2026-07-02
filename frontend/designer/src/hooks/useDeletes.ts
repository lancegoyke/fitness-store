// useDeletes — the Phase-0 delete verbs (CONTRACT.md "useDeletes"), ported
// from createMeso()'s removeExercise/requestRemoveDay/requestRemoveWeek/
// cancelPendingDelete/confirmPendingDelete (app/store_project/static/js/meso.js).
// `pendingDelete`/`setPendingDelete` are lifted from usePlanData (rule 1's
// corollary) rather than owned here.
import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { apiPost } from "../lib/api";
import type { Day, Id, PendingDelete, Week } from "./usePlanData";
import type { PlanEnvelope } from "../lib/api";

export interface UseDeletesOptions {
  planId: Id;
  csrf: string;
  program: Day[];
  weeks: Week[];
  pendingDelete: PendingDelete | null;
  setPendingDelete: Dispatch<SetStateAction<PendingDelete | null>>;
  applyPlanData: (data: PlanEnvelope) => void;
}

export function useDeletes(options: UseDeletesOptions) {
  const { planId, csrf, program, pendingDelete, setPendingDelete, applyPlanData } = options;

  // One shared in-flight guard across removeExercise + confirmPendingDelete,
  // set synchronously before the awaited fetch so a double-click can't race.
  const deletingRef = useRef(false);
  const [deleting, setDeleting] = useState(false);

  const setDeletingBoth = useCallback((value: boolean) => {
    deletingRef.current = value;
    setDeleting(value);
  }, []);

  const removeExercise = useCallback(
    async (di: number, xi: number) => {
      if (deletingRef.current) return;
      const day = program[di];
      const ex = day?.exercises[xi];
      if (!day || !ex) return;
      setDeletingBoth(true);
      try {
        const data = await apiPost<PlanEnvelope>(
          `/meso/api/plan/${planId}/prescription/${ex.id}/delete/`,
          null,
          csrf,
        );
        applyPlanData(data);
      } catch (err) {
        console.error("Remove exercise failed", err);
      } finally {
        setDeletingBoth(false);
      }
    },
    [program, planId, csrf, applyPlanData, setDeletingBoth],
  );

  const requestRemoveDay = useCallback(
    (di: number) => setPendingDelete({ type: "day", di }),
    [setPendingDelete],
  );

  const requestRemoveWeek = useCallback(
    (weekId: Id) => setPendingDelete({ type: "week", weekId }),
    [setPendingDelete],
  );

  const cancelPendingDelete = useCallback(() => {
    setPendingDelete(null);
  }, [setPendingDelete]);

  const confirmPendingDelete = useCallback(async () => {
    if (deletingRef.current || !pendingDelete) return;
    const pending = pendingDelete;
    setDeletingBoth(true);
    try {
      if (pending.type === "day") {
        const day = program[pending.di];
        if (!day) return;
        const data = await apiPost<PlanEnvelope>(
          `/meso/api/plan/${planId}/session/${day.id}/delete/`,
          null,
          csrf,
        );
        applyPlanData(data);
      } else if (pending.type === "week") {
        const data = await apiPost<PlanEnvelope>(
          `/meso/api/plan/${planId}/week/${pending.weekId}/delete/`,
          null,
          csrf,
        );
        applyPlanData(data);
      }
    } catch (err) {
      console.error("Remove failed", err);
    } finally {
      setDeletingBoth(false);
      setPendingDelete(null);
    }
  }, [pendingDelete, program, planId, csrf, applyPlanData, setDeletingBoth, setPendingDelete]);

  return {
    deleting,
    removeExercise,
    requestRemoveDay,
    requestRemoveWeek,
    cancelPendingDelete,
    confirmPendingDelete,
  };
}

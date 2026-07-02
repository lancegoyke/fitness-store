// useAutosave — fire-and-forget row autosave + load-type toggle
// (CONTRACT.md "useAutosave"), ported from createMeso()'s persistRow/
// toggleLoadType (app/store_project/static/js/meso.js).
import { useCallback } from "react";
import { apiPost } from "../lib/api";
import type { Exercise, HistoryCarrier } from "../lib/api";
import type { Id } from "./usePlanData";

export interface UseAutosaveOptions {
  planId: Id;
  csrf: string;
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void;
  adoptHistory: (data: HistoryCarrier) => void;
}

export function useAutosave(options: UseAutosaveOptions) {
  const { planId, csrf, patchExercise, adoptHistory } = options;

  const persistRow = useCallback(
    (ex: Exercise) => {
      if (!ex || ex.id == null) return;
      apiPost(
        `/meso/api/plan/${planId}/prescription/${ex.id}/`,
        {
          name: ex.name ?? "",
          sets: ex.sets ?? "",
          reps: ex.reps ?? "",
          load: ex.load ?? "",
          load_type: ex.load_type ?? "abs",
          rpe: ex.rpe ?? "",
          note: ex.note ?? "",
        },
        csrf,
      )
        .then((data) => adoptHistory(data as HistoryCarrier))
        .catch((err) => console.error("Autosave failed", err));
    },
    [planId, csrf, adoptHistory],
  );

  const toggleLoadType = useCallback(
    (ex: Exercise) => {
      const load_type = ex.load_type === "pct" ? "abs" : "pct";
      patchExercise(ex.id, { load_type });
      persistRow({ ...ex, load_type });
    },
    [patchExercise, persistRow],
  );

  return { persistRow, toggleLoadType };
}

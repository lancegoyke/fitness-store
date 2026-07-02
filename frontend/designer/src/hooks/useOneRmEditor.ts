// useOneRmEditor — the individual %1RM inline editor (CONTRACT.md
// "useOneRmEditor"), ported from createMeso()'s openOneRm/closeOneRm/
// saveOneRm (app/store_project/static/js/meso.js).
import { useCallback, useState } from "react";
import { apiPost } from "../lib/api";
import { parseOneRm } from "../lib/oneRm";
import type { Exercise, HistoryCarrier } from "../lib/api";
import type { Id } from "./usePlanData";

export interface OneRmEditorState {
  ex: Exercise;
  value: string;
  saving: boolean;
  error: string;
}

export interface UseOneRmEditorOptions {
  planId: Id;
  csrf: string;
  isGroup: boolean;
  adoptHistory: (data: HistoryCarrier) => void;
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void;
}

export function useOneRmEditor(options: UseOneRmEditorOptions) {
  const { planId, csrf, isGroup, patchExercise } = options;
  const [oneRm, setOneRm] = useState<OneRmEditorState | null>(null);

  const openOneRm = useCallback(
    (ex: Exercise) => {
      if (isGroup || !ex || ex.load_type !== "pct") return;
      setOneRm({ ex, value: ex.one_rm || "", saving: false, error: "" });
    },
    [isGroup],
  );

  const updateValue = useCallback((value: string) => {
    setOneRm((prev) => (prev ? { ...prev, value } : prev));
  }, []);

  const closeOneRm = useCallback(() => {
    setOneRm((prev) => {
      if (prev && prev.saving) return prev;
      return null;
    });
  }, []);

  const saveOneRm = useCallback(async () => {
    if (!oneRm || oneRm.saving) return;
    const parsed = parseOneRm(oneRm.value);
    if (!parsed.ok) {
      setOneRm((prev) =>
        prev ? { ...prev, error: "Enter a positive number, or leave blank to clear." } : prev,
      );
      return;
    }
    const { ex } = oneRm;
    setOneRm((prev) => (prev ? { ...prev, saving: true, error: "" } : prev));
    try {
      const data = await apiPost<{ one_rm?: string; source?: string }>(
        `/meso/api/plan/${planId}/prescription/${ex.id}/one-rm/`,
        { value: parsed.value },
        csrf,
      );
      patchExercise(ex.id, { one_rm: data.one_rm || "", one_rm_source: data.source || "" });
      setOneRm(null);
    } catch (err) {
      console.error("1RM save failed", err);
      setOneRm((prev) =>
        prev ? { ...prev, saving: false, error: "Couldn't save that 1RM. Please try again." } : prev,
      );
    }
  }, [oneRm, planId, csrf, patchExercise]);

  return { oneRm, openOneRm, updateValue, closeOneRm, saveOneRm };
}

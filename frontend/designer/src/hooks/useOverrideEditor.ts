// useOverrideEditor — the group per-athlete adjust editor (CONTRACT.md
// "useOverrideEditor"), ported from createMeso()'s openOverride/
// selectOverrideMember/closeOverride/saveOverride/clearOverride/submitOverride
// (app/store_project/static/js/meso.js).
import { useCallback, useState } from "react";
import { apiPost } from "../lib/api";
import { overrideDraft, overrideHasExisting, parseOverrideLoadPct } from "../lib/override";
import type { Exercise, GroupIdentity, GroupMember, HistoryCarrier } from "../lib/api";
import type { OverrideDraft } from "../lib/override";
import type { Id } from "./useGrid";

export interface OverrideEditorState {
  ex: Exercise;
  members: GroupMember[];
  memberId: string;
  draft: OverrideDraft;
  saving: boolean;
  error: string;
}

export interface UseOverrideEditorOptions {
  planId: Id;
  csrf: string;
  group: GroupIdentity | null;
  adoptHistory: (data: HistoryCarrier) => void;
  patchExercise: (exId: Id, patch: Partial<Exercise>) => void;
}

export function useOverrideEditor(options: UseOverrideEditorOptions) {
  const { planId, csrf, group, adoptHistory, patchExercise } = options;
  const [override, setOverride] = useState<OverrideEditorState | null>(null);

  const openOverride = useCallback(
    (ex: Exercise) => {
      const members = group?.members ?? [];
      if (!members.length) return;
      const adjusted = (ex.adjusts || []).find((a) => members.some((m) => m.id === a.id));
      const memberId = adjusted ? adjusted.id : members[0]!.id;
      setOverride({
        ex,
        members,
        memberId,
        draft: overrideDraft(ex, memberId),
        saving: false,
        error: "",
      });
    },
    [group],
  );

  const selectOverrideMember = useCallback((memberId: string) => {
    setOverride((prev) => {
      if (!prev) return prev;
      return { ...prev, memberId, draft: overrideDraft(prev.ex, memberId), error: "" };
    });
  }, []);

  const updateDraft = useCallback((patch: Partial<OverrideDraft>) => {
    setOverride((prev) => {
      if (!prev) return prev;
      return { ...prev, draft: { ...prev.draft, ...patch } };
    });
  }, []);

  const closeOverride = useCallback(() => {
    setOverride((prev) => {
      if (prev && prev.saving) return prev;
      return null;
    });
  }, []);

  const submitOverride = useCallback(
    async (ex: Exercise, body: Record<string, unknown>) => {
      setOverride((prev) => (prev ? { ...prev, saving: true, error: "" } : prev));
      try {
        const data = await apiPost<{
          adj?: string | null;
          adjusts?: Exercise["adjusts"];
          history?: HistoryCarrier["history"];
        }>(`/meso/api/plan/${planId}/prescription/${ex.id}/override/`, body, csrf);
        patchExercise(ex.id, { adj: data.adj ?? null, adjusts: data.adjusts ?? [] });
        adoptHistory(data);
        setOverride(null);
      } catch (err) {
        console.error("Override save failed", err);
        setOverride((prev) =>
          prev ? { ...prev, saving: false, error: "Couldn't save that adjust. Please try again." } : prev,
        );
      }
    },
    [planId, csrf, patchExercise, adoptHistory],
  );

  const saveOverride = useCallback(async () => {
    if (!override || override.saving) return;
    const parsed = parseOverrideLoadPct(override.draft.load_pct);
    if (!parsed.ok) {
      setOverride((prev) =>
        prev ? { ...prev, error: "Load % must be a whole number from 1 to 200." } : prev,
      );
      return;
    }
    const { ex, memberId, draft } = override;
    await submitOverride(ex, {
      athlete: memberId,
      swap: draft.swap.trim(),
      load_pct: parsed.value,
      sets: draft.sets.trim(),
      reps: draft.reps.trim(),
      note: draft.note.trim(),
    });
  }, [override, submitOverride]);

  const clearOverride = useCallback(async () => {
    if (!override || override.saving) return;
    const { ex, memberId } = override;
    await submitOverride(ex, { athlete: memberId, clear: true });
  }, [override, submitOverride]);

  const overrideHasExistingValue = override
    ? overrideHasExisting(override.ex, override.memberId)
    : false;

  return {
    override,
    overrideHasExisting: overrideHasExistingValue,
    openOverride,
    selectOverrideMember,
    updateDraft,
    closeOverride,
    saveOverride,
    clearOverride,
  };
}

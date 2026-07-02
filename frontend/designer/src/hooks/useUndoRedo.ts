// useUndoRedo — undo/redo + the window keydown wiring (CONTRACT.md
// "useUndoRedo"), ported from createMeso()'s undo/redo/handleUndoKey
// (app/store_project/static/js/meso.js). The keyboard listener lives inside
// this hook (not DesignerRoot) for its whole lifetime.
import { useCallback, useEffect, useRef, useState } from "react";
import { apiPost } from "../lib/api";
import { undoKeyIntent } from "../lib/keys";
import type { HistoryState, PlanEnvelope } from "../lib/api";
import type { Id } from "./usePlanData";

export interface UseUndoRedoOptions {
  planId: Id;
  csrf: string;
  viewedWeekId: Id | null;
  history: HistoryState;
  applyPlanData: (data: PlanEnvelope) => void;
}

export function useUndoRedo(options: UseUndoRedoOptions) {
  const { planId, csrf, viewedWeekId, history, applyPlanData } = options;

  const undoingRef = useRef(false);
  const [undoing, setUndoing] = useState(false);

  const setUndoingBoth = useCallback((value: boolean) => {
    undoingRef.current = value;
    setUndoing(value);
  }, []);

  const undo = useCallback(async () => {
    if (undoingRef.current || !history.can_undo) return;
    setUndoingBoth(true);
    try {
      const data = await apiPost<PlanEnvelope>(
        `/meso/api/plan/${planId}/undo/`,
        { week_id: viewedWeekId },
        csrf,
      );
      applyPlanData(data);
    } catch (err) {
      console.error("Undo failed", err);
    } finally {
      setUndoingBoth(false);
    }
  }, [history.can_undo, planId, viewedWeekId, csrf, applyPlanData, setUndoingBoth]);

  const redo = useCallback(async () => {
    if (undoingRef.current || !history.can_redo) return;
    setUndoingBoth(true);
    try {
      const data = await apiPost<PlanEnvelope>(
        `/meso/api/plan/${planId}/redo/`,
        { week_id: viewedWeekId },
        csrf,
      );
      applyPlanData(data);
    } catch (err) {
      console.error("Redo failed", err);
    } finally {
      setUndoingBoth(false);
    }
  }, [history.can_redo, planId, viewedWeekId, csrf, applyPlanData, setUndoingBoth]);

  const undoRef = useRef(undo);
  undoRef.current = undo;
  const redoRef = useRef(redo);
  redoRef.current = redo;

  useEffect(() => {
    function handler(event: KeyboardEvent) {
      const intent = undoKeyIntent({
        key: event.key,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        shiftKey: event.shiftKey,
        target: event.target as { tagName?: string; isContentEditable?: boolean } | null,
      });
      if (intent === null) return;
      event.preventDefault();
      if (intent === "redo") {
        void redoRef.current();
      } else {
        void undoRef.current();
      }
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return { undoing, undo, redo };
}

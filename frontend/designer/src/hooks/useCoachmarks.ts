// useCoachmarks — first-run coachmark dismissal state (CONTRACT.md
// "useCoachmarks"), ported from createMeso()'s loadCoachmarks/coachmarkVisible/
// dismissCoachmark (app/store_project/static/js/meso.js).
import { useCallback, useState } from "react";
import { COACHMARK_KEYS, dismiss, readDismissed } from "../lib/coachmarks";

function initialDismissed(): Record<string, boolean> {
  const next: Record<string, boolean> = {};
  for (const key of COACHMARK_KEYS) {
    if (readDismissed(key)) next[key] = true;
  }
  return next;
}

export function useCoachmarks() {
  const [dismissed, setDismissed] = useState<Record<string, boolean>>(initialDismissed);

  const coachmarkVisible = useCallback(
    (key: string) => !dismissed[key],
    [dismissed],
  );

  const dismissCoachmark = useCallback((key: string) => {
    setDismissed((prev) => ({ ...prev, [key]: true }));
    dismiss(key);
  }, []);

  return { dismissed, coachmarkVisible, dismissCoachmark };
}

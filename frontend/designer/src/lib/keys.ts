// Undo/redo keyboard decision logic, ported from createMeso()'s
// handleUndoKey (meso.js). The original both decided the intent AND called
// `event.preventDefault()` + `this.undo()`/`this.redo()`; this pure version
// only decides — the hook (useUndoRedo) calls preventDefault() and
// undo()/redo() when the intent isn't null, so a no-op keystroke (a plain
// "z", an unrelated modified key, a form-field target) never touches the
// event at all.

export interface UndoKeyLikeEvent {
  key?: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  target?: { tagName?: string; isContentEditable?: boolean } | null;
}

export type UndoKeyIntent = "undo" | "redo" | null;

/**
 * Ctrl/Cmd+Z → "undo"; Shift+Ctrl/Cmd+Z → "redo"; everything else
 * (including keystrokes from an input/textarea/select/contenteditable
 * target, where native field undo should win) → null.
 */
export function undoKeyIntent(event: UndoKeyLikeEvent): UndoKeyIntent {
  const key = (event.key || "").toLowerCase();
  if (key !== "z" || !(event.ctrlKey || event.metaKey)) return null;

  const target = event.target || {};
  const tag = (target.tagName || "").toUpperCase();
  if (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  ) {
    return null;
  }

  return event.shiftKey ? "redo" : "undo";
}

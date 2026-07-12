// useUndoKeyboard — issue #455 phase A5: the global Ctrl/Cmd+Z window
// keydown wiring, extracted from the retired useUndoRedo so the one
// surviving undo/redo owner (useGrid) doesn't need its own copy. Owns NO
// state of its own — mirrors useTableReorder's "owns no state" precedent —
// just registers one window listener for its caller's lifetime and calls
// whichever undo()/redo() it's given.
import { useEffect } from "react";
import { undoKeyIntent } from "../lib/keys";

/**
 * Registers a window keydown listener (for the caller's lifetime) that
 * routes Ctrl/Cmd+Z to `undo` and Shift+Ctrl/Cmd+Z to `redo`, via
 * `lib/keys.ts`'s `undoKeyIntent` — a keystroke from a form field (native
 * field undo should win there) or any other key is left alone. Calls
 * `event.preventDefault()` only on an actual undo/redo keystroke, mirroring
 * the retired useUndoRedo's handler exactly.
 */
export function useUndoKeyboard(undo: () => void, redo: () => void): void {
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
        redo();
      } else {
        undo();
      }
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [undo, redo]);
}

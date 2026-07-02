// Ported from frontend/meso_undo.test.js's "handleUndoKey" describe-block,
// adapted to the pure decision function (no preventDefault/undo/redo call
// here — those are the hook's job when the intent isn't null).
import { describe, expect, it } from "vitest";
import { undoKeyIntent, type UndoKeyLikeEvent } from "./keys";

// A keydown-ish event. Real Ctrl+Z arrives with key "z"; real Shift+Ctrl+Z
// arrives with key "Z" — the shifted cases below use the uppercase form on
// purpose (mirrors meso_undo.test.js's keyEvent()).
function keyEvent(overrides: Partial<UndoKeyLikeEvent> = {}): UndoKeyLikeEvent {
  return {
    key: "z",
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    target: { tagName: "DIV", isContentEditable: false },
    ...overrides,
  };
}

describe("undoKeyIntent", () => {
  it("Ctrl+Z → undo", () => {
    expect(undoKeyIntent(keyEvent({ ctrlKey: true }))).toBe("undo");
  });

  it("Cmd+Z → undo", () => {
    expect(undoKeyIntent(keyEvent({ metaKey: true }))).toBe("undo");
  });

  it("Shift+Ctrl+Z → redo", () => {
    expect(undoKeyIntent(keyEvent({ key: "Z", ctrlKey: true, shiftKey: true }))).toBe("redo");
  });

  it("Shift+Cmd+Z → redo", () => {
    expect(undoKeyIntent(keyEvent({ key: "Z", metaKey: true, shiftKey: true }))).toBe("redo");
  });

  it("a plain z → null", () => {
    expect(undoKeyIntent(keyEvent())).toBe(null);
  });

  it("other modified keys → null", () => {
    expect(undoKeyIntent(keyEvent({ key: "s", ctrlKey: true }))).toBe(null);
  });

  it("ignores keystrokes from form fields, where native undo must win", () => {
    for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
      expect(
        undoKeyIntent(keyEvent({ ctrlKey: true, target: { tagName, isContentEditable: false } })),
      ).toBe(null);
    }
    expect(
      undoKeyIntent(
        keyEvent({ ctrlKey: true, target: { tagName: "DIV", isContentEditable: true } }),
      ),
    ).toBe(null);
  });
});

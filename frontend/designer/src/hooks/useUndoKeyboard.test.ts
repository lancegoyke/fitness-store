// Specs for useUndoKeyboard (issue #455 phase A5) — the keyboard-only
// assertions ported out of the retired useUndoRedo.test.ts's "keyboard
// wiring" describe block before that hook/file was deleted. The
// undo()/redo() POST-and-apply behavior itself has no home here anymore —
// useGrid's undo/redo (useGrid.test.ts) are the only surviving undo/redo
// owner; this hook only ever routes a keystroke to whichever callbacks
// it's given.
import { act, renderHook, cleanup } from "@testing-library/react";
import { useUndoKeyboard } from "./useUndoKeyboard";

function dispatch(opts: Partial<KeyboardEventInit> = {}, targetEl: Element = document.body) {
  const event = new KeyboardEvent("keydown", { key: "z", bubbles: true, cancelable: true, ...opts });
  const spy = vi.spyOn(event, "preventDefault");
  targetEl.dispatchEvent(event);
  return spy;
}

afterEach(() => {
  cleanup();
});

describe("useUndoKeyboard", () => {
  it("Ctrl+Z calls undo() and preventDefault", async () => {
    const undo = vi.fn();
    const redo = vi.fn();
    renderHook(() => useUndoKeyboard(undo, redo));
    let spy!: ReturnType<typeof vi.fn>;
    await act(async () => {
      spy = dispatch({ ctrlKey: true }) as unknown as ReturnType<typeof vi.fn>;
    });
    expect(undo).toHaveBeenCalledTimes(1);
    expect(redo).not.toHaveBeenCalled();
    expect(spy).toHaveBeenCalled();
  });

  it("Cmd+Z calls undo()", async () => {
    const undo = vi.fn();
    const redo = vi.fn();
    renderHook(() => useUndoKeyboard(undo, redo));
    await act(async () => {
      dispatch({ metaKey: true });
    });
    expect(undo).toHaveBeenCalledTimes(1);
  });

  it("Shift+Ctrl+Z calls redo()", async () => {
    const undo = vi.fn();
    const redo = vi.fn();
    renderHook(() => useUndoKeyboard(undo, redo));
    await act(async () => {
      dispatch({ key: "Z", ctrlKey: true, shiftKey: true });
    });
    expect(redo).toHaveBeenCalledTimes(1);
    expect(undo).not.toHaveBeenCalled();
  });

  it("a plain z does nothing and does not preventDefault", async () => {
    const undo = vi.fn();
    const redo = vi.fn();
    renderHook(() => useUndoKeyboard(undo, redo));
    const spy = dispatch();
    expect(undo).not.toHaveBeenCalled();
    expect(redo).not.toHaveBeenCalled();
    expect(spy).not.toHaveBeenCalled();
  });

  it("ignores keystrokes from form fields (native undo should win there)", async () => {
    const undo = vi.fn();
    const redo = vi.fn();
    renderHook(() => useUndoKeyboard(undo, redo));
    const input = document.createElement("input");
    document.body.appendChild(input);
    await act(async () => {
      dispatch({ ctrlKey: true }, input);
    });
    expect(undo).not.toHaveBeenCalled();
    document.body.removeChild(input);
  });

  it("unregisters the listener on unmount", async () => {
    const undo = vi.fn();
    const redo = vi.fn();
    const { unmount } = renderHook(() => useUndoKeyboard(undo, redo));
    unmount();
    await act(async () => {
      dispatch({ ctrlKey: true });
    });
    expect(undo).not.toHaveBeenCalled();
  });

  it("re-registers against fresh callbacks when undo/redo identity changes", async () => {
    const undoA = vi.fn();
    const redoA = vi.fn();
    const { rerender } = renderHook(({ undo, redo }) => useUndoKeyboard(undo, redo), {
      initialProps: { undo: undoA, redo: redoA },
    });
    const undoB = vi.fn();
    const redoB = vi.fn();
    rerender({ undo: undoB, redo: redoB });
    await act(async () => {
      dispatch({ ctrlKey: true });
    });
    expect(undoA).not.toHaveBeenCalled();
    expect(undoB).toHaveBeenCalledTimes(1);
  });
});

// Specs for useUndoRedo (CONTRACT.md "useUndoRedo") — faithful port of
// meso_undo.test.js's undo()/redo(), plus the keyboard wiring that in the
// source lived on the root div's @keydown.window and here lives inside the
// hook itself via a window keydown listener + lib/keys.ts's undoKeyIntent.
import { act, renderHook, cleanup } from "@testing-library/react";
import { useUndoRedo } from "./useUndoRedo";
import type { HistoryState } from "../lib/api";

const HISTORY_BOTH: HistoryState = {
  can_undo: true,
  can_redo: true,
  undo_label: "Edited Box Squat",
  redo_label: "Deleted Day 2",
};
const HISTORY_NONE: HistoryState = {
  can_undo: false,
  can_redo: false,
  undo_label: null,
  redo_label: null,
};

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function planData() {
  return {
    ok: true,
    program: [{ id: 10, n: 1, name: "Lower", exercises: [] }],
    weeks: [{ id: 2, index: 1, label: "Wk 1", current: true }],
    phases: [],
    viewing: 2,
    history: HISTORY_BOTH,
  };
}

function setup(history: HistoryState = HISTORY_BOTH, viewedWeekId: number | string | null = 5) {
  const applyPlanData = vi.fn();
  const hook = renderHook(() =>
    useUndoRedo({ planId: 7, csrf: "tok", viewedWeekId, history, applyPlanData }),
  );
  return { ...hook, applyPlanData };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
});

describe("undo", () => {
  it("POSTs the viewed week to the undo endpoint and applies the reply", async () => {
    const { result, applyPlanData } = setup(HISTORY_BOTH, 5);
    const data = planData();
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.undo();
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/undo/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(JSON.parse(opts.body)).toEqual({ week_id: 5 });
    expect(applyPlanData).toHaveBeenCalledWith(data);
    expect(result.current.undoing).toBe(false);
  });

  it("does not fetch when history.can_undo is false", async () => {
    const { result } = setup(HISTORY_NONE);
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.undo();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("console.errors, swallows the failure, and clears the guard for a retry", async () => {
    const { result } = setup(HISTORY_BOTH, 5);
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("boom")) as unknown as typeof fetch;
    await act(async () => {
      await result.current.undo();
    });
    expect(result.current.undoing).toBe(false);
    expect(console.error).toHaveBeenCalled();
    globalThis.fetch = vi.fn().mockResolvedValue(res(planData())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.undo();
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("redo", () => {
  it("POSTs the viewed week to the redo endpoint and applies the reply", async () => {
    const { result, applyPlanData } = setup(HISTORY_BOTH, 3);
    const data = planData();
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.redo();
    });
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/redo/");
    expect(JSON.parse(opts.body)).toEqual({ week_id: 3 });
    expect(applyPlanData).toHaveBeenCalledWith(data);
  });

  it("does not fetch when history.can_redo is false", async () => {
    const { result } = setup({ ...HISTORY_BOTH, can_redo: false });
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.redo();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

describe("keyboard wiring (window keydown, registered for the hook's lifetime)", () => {
  function dispatch(opts: Partial<KeyboardEventInit> = {}, targetEl: Element = document.body) {
    const event = new KeyboardEvent("keydown", { key: "z", bubbles: true, cancelable: true, ...opts });
    const spy = vi.spyOn(event, "preventDefault");
    targetEl.dispatchEvent(event);
    return spy;
  }

  it("Ctrl+Z calls undo() and preventDefault", async () => {
    setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn().mockResolvedValue(res(planData())) as unknown as typeof fetch;
    let spy!: ReturnType<typeof vi.fn>;
    await act(async () => {
      spy = dispatch({ ctrlKey: true }) as unknown as ReturnType<typeof vi.fn>;
      await Promise.resolve();
    });
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/meso/api/plan/7/undo/",
      expect.anything(),
    );
    expect(spy).toHaveBeenCalled();
  });

  it("Cmd+Z calls undo()", async () => {
    setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn().mockResolvedValue(res(planData())) as unknown as typeof fetch;
    await act(async () => {
      dispatch({ metaKey: true });
      await Promise.resolve();
    });
    expect(globalThis.fetch).toHaveBeenCalledWith("/meso/api/plan/7/undo/", expect.anything());
  });

  it("Shift+Ctrl+Z calls redo()", async () => {
    setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn().mockResolvedValue(res(planData())) as unknown as typeof fetch;
    await act(async () => {
      dispatch({ key: "Z", ctrlKey: true, shiftKey: true });
      await Promise.resolve();
    });
    expect(globalThis.fetch).toHaveBeenCalledWith("/meso/api/plan/7/redo/", expect.anything());
  });

  it("a plain z does nothing and does not preventDefault", async () => {
    setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    const spy = dispatch();
    await act(async () => {
      await Promise.resolve();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(spy).not.toHaveBeenCalled();
  });

  it("ignores keystrokes from form fields (native undo should win there)", async () => {
    setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    const input = document.createElement("input");
    document.body.appendChild(input);
    await act(async () => {
      dispatch({ ctrlKey: true }, input);
      await Promise.resolve();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    document.body.removeChild(input);
  });

  it("unregisters the listener on unmount", async () => {
    const { unmount } = setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn().mockResolvedValue(res(planData())) as unknown as typeof fetch;
    unmount();
    await act(async () => {
      dispatch({ ctrlKey: true });
      await Promise.resolve();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("a no-op keystroke (e.g. history exhausted) still doesn't fetch, guard shared with click path", async () => {
    setup(HISTORY_NONE, 5);
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      dispatch({ ctrlKey: true });
      await Promise.resolve();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  // P1 (multi-week table): the table view's undo/redo lives in a sibling
  // hook (useGrid), but the window keydown listener lives here — so
  // DesignerRoot overrides what the shortcut invokes via keyboardUndo/
  // keyboardRedo, letting the shortcut follow whichever view is active
  // instead of always hitting this hook's own planData undo/redo.
  it("routes Ctrl/Cmd+Z to keyboardUndo when provided, and does not hit the internal planData undo", async () => {
    const applyPlanData = vi.fn();
    const keyboardUndo = vi.fn();
    const keyboardRedo = vi.fn();
    renderHook(() =>
      useUndoRedo({
        planId: 7,
        csrf: "tok",
        viewedWeekId: 5,
        history: HISTORY_BOTH,
        applyPlanData,
        keyboardUndo,
        keyboardRedo,
      }),
    );
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      dispatch({ ctrlKey: true });
      await Promise.resolve();
    });
    expect(keyboardUndo).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("routes Shift+Ctrl/Cmd+Z to keyboardRedo when provided, and does not hit the internal planData redo", async () => {
    const applyPlanData = vi.fn();
    const keyboardUndo = vi.fn();
    const keyboardRedo = vi.fn();
    renderHook(() =>
      useUndoRedo({
        planId: 7,
        csrf: "tok",
        viewedWeekId: 5,
        history: HISTORY_BOTH,
        applyPlanData,
        keyboardUndo,
        keyboardRedo,
      }),
    );
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      dispatch({ key: "Z", ctrlKey: true, shiftKey: true });
      await Promise.resolve();
    });
    expect(keyboardRedo).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("falls back to the internal planData undo when keyboardUndo/keyboardRedo are absent (unchanged behavior)", async () => {
    setup(HISTORY_BOTH, 5);
    globalThis.fetch = vi.fn().mockResolvedValue(res(planData())) as unknown as typeof fetch;
    await act(async () => {
      dispatch({ ctrlKey: true });
      await Promise.resolve();
    });
    expect(globalThis.fetch).toHaveBeenCalledWith("/meso/api/plan/7/undo/", expect.anything());
  });
});

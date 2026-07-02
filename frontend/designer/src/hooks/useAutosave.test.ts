// Specs for useAutosave (CONTRACT.md "useAutosave") — persistRow (fire-and-
// forget autosave) and toggleLoadType (flip via patchExercise, then persistRow).
import { act, renderHook } from "@testing-library/react";
import { useAutosave } from "./useAutosave";
import type { Exercise } from "../lib/api";

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function sentBody(n = 0) {
  const mockFetch = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  const call = mockFetch.mock.calls[n] as [string, RequestInit];
  return call[1].body == null ? null : JSON.parse(call[1].body as string);
}

function ex(overrides: Partial<Exercise> = {}): Exercise {
  return { id: 9, name: "Squat", sets: "3", reps: "5", load: "100", ...overrides };
}

function setup() {
  const patchExercise = vi.fn();
  const adoptHistory = vi.fn();
  const hook = renderHook(() =>
    useAutosave({ planId: 7, csrf: "tok", patchExercise, adoptHistory }),
  );
  return { ...hook, patchExercise, adoptHistory };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("persistRow", () => {
  it("POSTs the full field payload with the CSRF header", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true, prescription: { id: 9 } })) as unknown as typeof fetch;
    act(() => {
      result.current.persistRow(ex({ load_type: "pct", rpe: "8", note: "tempo" }));
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/9/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(sentBody()).toEqual({
      name: "Squat",
      sets: "3",
      reps: "5",
      load: "100",
      load_type: "pct",
      rpe: "8",
      note: "tempo",
    });
  });

  it("defaults a typeless row's load_type to abs", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.persistRow(ex({ load_type: undefined }));
    });
    expect(sentBody().load_type).toBe("abs");
  });

  it("is a no-op when the row has no id", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    act(() => {
      result.current.persistRow(ex({ id: undefined as unknown as number }));
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("calls adoptHistory with the reply once it lands (fire-and-forget)", async () => {
    const { result, adoptHistory } = setup();
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(
      () => new Promise((resolve) => (resolveFetch = resolve)),
    ) as unknown as typeof fetch;
    act(() => {
      result.current.persistRow(ex());
    });
    // Fire-and-forget: persistRow itself doesn't return a promise callers await.
    expect(adoptHistory).not.toHaveBeenCalled();
    await act(async () => {
      resolveFetch(res({ ok: true, prescription: { id: 9 }, history: { can_undo: true } }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(adoptHistory).toHaveBeenCalledWith(
      expect.objectContaining({ history: { can_undo: true } }),
    );
  });

  it("console.errors and swallows a failed autosave", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) as unknown as typeof fetch;
    act(() => {
      result.current.persistRow(ex());
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(console.error).toHaveBeenCalledWith("Autosave failed", expect.anything());
  });
});

describe("toggleLoadType", () => {
  it("flips load_type via patchExercise then persists", () => {
    const { result, patchExercise } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.toggleLoadType(ex({ load_type: "abs" }));
    });
    expect(patchExercise).toHaveBeenCalledWith(9, { load_type: "pct" });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(sentBody().load_type).toBe("pct");
  });

  it("treats a typeless row as absolute (flips to pct)", () => {
    const { result, patchExercise } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.toggleLoadType(ex({ load_type: undefined }));
    });
    expect(patchExercise).toHaveBeenCalledWith(9, { load_type: "pct" });
  });

  it("flips pct back to abs", () => {
    const { result, patchExercise } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.toggleLoadType(ex({ load_type: "pct" }));
    });
    expect(patchExercise).toHaveBeenCalledWith(9, { load_type: "abs" });
  });
});

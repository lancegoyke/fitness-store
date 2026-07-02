// Specs for useOneRmEditor (CONTRACT.md "useOneRmEditor") — the individual
// %1RM inline editor. Ported from meso.test.js's "coach 1RM editor" block.
import { act, renderHook } from "@testing-library/react";
import { useOneRmEditor } from "./useOneRmEditor";
import type { Exercise } from "../lib/api";

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function sentBody(n = 0) {
  const mockFetch = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  const call = mockFetch.mock.calls[n] as [string, RequestInit];
  return call[1].body == null ? null : JSON.parse(call[1].body as string);
}

function pctRow(overrides: Partial<Exercise> = {}): Exercise {
  return { id: 21, name: "Back Squat", sets: "3", reps: "5", load: "75", load_type: "pct", one_rm: "140", one_rm_source: "logged", ...overrides };
}

function setup(isGroup = false) {
  const adoptHistory = vi.fn();
  const patchExercise = vi.fn();
  const hook = renderHook(() =>
    useOneRmEditor({ planId: 7, csrf: "tok", isGroup, adoptHistory, patchExercise }),
  );
  return { ...hook, adoptHistory, patchExercise };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("openOneRm", () => {
  it("opens on an individual %1RM row, seeding the current value", () => {
    const { result } = setup();
    const ex = pctRow();
    act(() => result.current.openOneRm(ex));
    expect(result.current.oneRm).not.toBe(null);
    expect(result.current.oneRm!.ex).toBe(ex);
    expect(result.current.oneRm!.value).toBe("140");
  });

  it("seeds a blank value when the row has no stored 1RM", () => {
    const { result } = setup();
    act(() => result.current.openOneRm(pctRow({ one_rm: "" })));
    expect(result.current.oneRm!.value).toBe("");
  });

  it("is a no-op in group mode", () => {
    const { result } = setup(true);
    act(() => result.current.openOneRm(pctRow()));
    expect(result.current.oneRm).toBe(null);
  });

  it("is a no-op on a non-percent row", () => {
    const { result } = setup(false);
    act(() => result.current.openOneRm(pctRow({ load_type: "abs" })));
    expect(result.current.oneRm).toBe(null);
  });
});

describe("closeOneRm", () => {
  it("guards a dismiss while saving", async () => {
    const { result } = setup();
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(() => new Promise((resolve) => (resolveFetch = resolve))) as unknown as typeof fetch;
    act(() => result.current.openOneRm(pctRow()));
    act(() => result.current.updateValue("150"));
    let savePromise!: Promise<void>;
    act(() => {
      savePromise = result.current.saveOneRm();
    });
    act(() => result.current.closeOneRm());
    expect(result.current.oneRm).not.toBe(null);
    await act(async () => {
      resolveFetch(res({ one_rm: "150", source: "manual" }));
      await savePromise;
    });
  });
});

describe("saveOneRm", () => {
  it("posts the value, patches the row from the reply, and closes", async () => {
    const { result, patchExercise } = setup();
    const ex = pctRow({ one_rm: "", one_rm_source: "" });
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true, one_rm: "150", source: "manual" })) as unknown as typeof fetch;
    act(() => result.current.openOneRm(ex));
    act(() => result.current.updateValue("150"));
    await act(async () => {
      await result.current.saveOneRm();
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/21/one-rm/");
    expect(sentBody()).toEqual({ value: "150" });
    expect(patchExercise).toHaveBeenCalledWith(21, { one_rm: "150", one_rm_source: "manual" });
    expect(result.current.oneRm).toBe(null);
    // NOTE (contract ambiguity, resolved toward meso.js ground truth): the
    // source's saveOneRm never calls adoptHistory and the one-rm/ endpoint's
    // reply carries no `history` key (phase2-inventory.md), so whether the
    // ported hook calls the injected adoptHistory is behaviorally invisible —
    // deliberately not asserted either way here.
  });

  it("clears by saving a blank value, reverting to the log-derived estimate", async () => {
    const { result, patchExercise } = setup();
    const ex = pctRow();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true, one_rm: "130", source: "logged" })) as unknown as typeof fetch;
    act(() => result.current.openOneRm(ex));
    act(() => result.current.updateValue(""));
    await act(async () => {
      await result.current.saveOneRm();
    });
    expect(sentBody()).toEqual({ value: "" });
    expect(patchExercise).toHaveBeenCalledWith(21, { one_rm: "130", one_rm_source: "logged" });
  });

  it("rejects a non-numeric or non-positive value without posting", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    act(() => result.current.openOneRm(pctRow()));
    act(() => result.current.updateValue("heavy"));
    await act(async () => {
      await result.current.saveOneRm();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.oneRm!.error).toMatch(/positive number/);

    act(() => result.current.updateValue("0"));
    await act(async () => {
      await result.current.saveOneRm();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("keeps the editor open with an error and the row untouched on failure", async () => {
    const { result } = setup();
    const ex = pctRow();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 500)) as unknown as typeof fetch;
    act(() => result.current.openOneRm(ex));
    act(() => result.current.updateValue("160"));
    await act(async () => {
      await result.current.saveOneRm();
    });
    expect(result.current.oneRm).not.toBe(null);
    expect(result.current.oneRm!.error).toMatch(/Couldn't save/);
    expect(ex.one_rm).toBe("140");
  });
});

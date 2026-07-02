// Specs for useOverrideEditor (CONTRACT.md "useOverrideEditor") — the group
// per-athlete adjust editor. Ported from meso.test.js's "override editor"
// describe block: no-op outside group mode / no members, member preselection
// off ex.adjusts, guarded mid-save close, validated load%, full-diff submit,
// error keeps the editor open with the row untouched.
import { act, renderHook } from "@testing-library/react";
import { useOverrideEditor } from "./useOverrideEditor";
import type { Exercise, GroupIdentity } from "../lib/api";

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function sentBody(n = 0) {
  const mockFetch = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  const call = mockFetch.mock.calls[n] as [string, RequestInit];
  return call[1].body == null ? null : JSON.parse(call[1].body as string);
}

function group(overrides: Partial<GroupIdentity> = {}): GroupIdentity {
  return {
    id: 3,
    name: "Squad",
    member_count: 2,
    members: [
      { id: "a1", name: "Maya Okonkwo", initials: "MO" },
      { id: "a2", name: "Aaron Adams", initials: "AA" },
    ],
    flags: [],
    ...overrides,
  };
}

function groupRow(overrides: Partial<Exercise> = {}): Exercise {
  return {
    id: 11,
    name: "Back Squat",
    sets: "3",
    reps: "10",
    load: "100",
    adj: "MO -10%",
    adjusts: [
      { id: "a1", name: "Maya Okonkwo", initials: "MO", label: "-10%", swap: "", load_pct: 90, sets: "", reps: "", note: "" },
    ],
    ...overrides,
  };
}

function setup(g: GroupIdentity | null = group()) {
  const adoptHistory = vi.fn();
  const patchExercise = vi.fn();
  const hook = renderHook(() =>
    useOverrideEditor({ planId: 7, csrf: "tok", group: g, adoptHistory, patchExercise }),
  );
  return { ...hook, adoptHistory, patchExercise };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("openOverride", () => {
  it("opens on a shared row, preselecting the first member with their stored diff", () => {
    const { result } = setup();
    const ex = groupRow();
    act(() => result.current.openOverride(ex));
    expect(result.current.override).not.toBe(null);
    expect(result.current.override!.ex).toBe(ex);
    expect(result.current.override!.memberId).toBe("a1");
    expect(result.current.override!.draft.load_pct).toBe("90");
    expect(result.current.overrideHasExisting).toBe(true);
  });

  it("preselects the adjusted member when the badge isn't the first member", () => {
    const { result } = setup();
    const ex = groupRow({
      adj: "AA -10%",
      adjusts: [{ id: "a2", name: "Aaron Adams", initials: "AA", label: "-10%", swap: "", load_pct: 90, sets: "", reps: "", note: "" }],
    });
    act(() => result.current.openOverride(ex));
    expect(result.current.override!.memberId).toBe("a2");
  });

  it("is a no-op outside group mode (group is null)", () => {
    const { result } = setup(null);
    act(() => result.current.openOverride(groupRow()));
    expect(result.current.override).toBe(null);
  });

  it("is a no-op with no members", () => {
    const { result } = setup(group({ members: [] }));
    act(() => result.current.openOverride(groupRow()));
    expect(result.current.override).toBe(null);
  });
});

describe("selectOverrideMember", () => {
  it("re-derives the draft and clears any error", () => {
    const { result } = setup();
    act(() => result.current.openOverride(groupRow()));
    act(() => result.current.selectOverrideMember("a2"));
    expect(result.current.override!.memberId).toBe("a2");
    expect(result.current.override!.draft.load_pct).toBe("");
    expect(result.current.overrideHasExisting).toBe(false);
  });
});

describe("closeOverride", () => {
  it("closes when idle", () => {
    const { result } = setup();
    act(() => result.current.openOverride(groupRow()));
    act(() => result.current.closeOverride());
    expect(result.current.override).toBe(null);
  });

  it("guards a dismiss while saving mid-flight", async () => {
    const { result } = setup();
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(() => new Promise((resolve) => (resolveFetch = resolve))) as unknown as typeof fetch;
    act(() => result.current.openOverride(groupRow()));
    let savePromise!: Promise<void>;
    act(() => {
      savePromise = result.current.saveOverride();
    });
    act(() => result.current.closeOverride());
    expect(result.current.override).not.toBe(null);
    await act(async () => {
      resolveFetch(res({ adj: null, adjusts: [] }));
      await savePromise;
    });
  });
});

describe("saveOverride", () => {
  it("posts the full diff and, on success, patches the row + adopts history + closes", async () => {
    const { result, adoptHistory, patchExercise } = setup();
    const ex = groupRow();
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ adj: "2 adjusts", adjusts: [{ id: "a1" }, { id: "a2" }], history: { can_undo: true } }),
    );
    act(() => result.current.openOverride(ex));
    act(() => result.current.selectOverrideMember("a2"));
    act(() => result.current.updateDraft({ swap: "Box Squat", load_pct: "85" }));
    await act(async () => {
      await result.current.saveOverride();
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/prescription/11/override/");
    expect(sentBody()).toEqual({ athlete: "a2", swap: "Box Squat", load_pct: 85, sets: "", reps: "", note: "" });
    expect(patchExercise).toHaveBeenCalledWith(11, { adj: "2 adjusts", adjusts: [{ id: "a1" }, { id: "a2" }] });
    expect(adoptHistory).toHaveBeenCalledWith(expect.objectContaining({ history: { can_undo: true } }));
    expect(result.current.override).toBe(null);
  });

  it("sends load_pct null when the field is blank", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ adj: null, adjusts: [] })) as unknown as typeof fetch;
    act(() => result.current.openOverride(groupRow()));
    act(() => result.current.selectOverrideMember("a2"));
    act(() => result.current.updateDraft({ note: "tempo" }));
    await act(async () => {
      await result.current.saveOverride();
    });
    expect(sentBody().load_pct).toBe(null);
    expect(sentBody().note).toBe("tempo");
  });

  it("rejects an invalid load% without posting and sets the error", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    act(() => result.current.openOverride(groupRow()));
    act(() => result.current.updateDraft({ load_pct: "abc" }));
    await act(async () => {
      await result.current.saveOverride();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.override!.error).toMatch(/Load %/);

    act(() => result.current.updateDraft({ load_pct: "500" }));
    await act(async () => {
      await result.current.saveOverride();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("keeps the editor open with an error and leaves the row untouched on failure", async () => {
    const { result } = setup();
    const ex = groupRow();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 500)) as unknown as typeof fetch;
    act(() => result.current.openOverride(ex));
    act(() => result.current.updateDraft({ load_pct: "80" }));
    await act(async () => {
      await result.current.saveOverride();
    });
    expect(result.current.override).not.toBe(null);
    expect(result.current.override!.error).toMatch(/Couldn't save/);
    expect(ex.adj).toBe("MO -10%");
  });
});

describe("clearOverride", () => {
  it("posts {athlete, clear: true} and repaints the row", async () => {
    const { result, patchExercise } = setup();
    const ex = groupRow();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ adj: null, adjusts: [] })) as unknown as typeof fetch;
    act(() => result.current.openOverride(ex));
    await act(async () => {
      await result.current.clearOverride();
    });
    expect(sentBody()).toEqual({ athlete: "a1", clear: true });
    expect(patchExercise).toHaveBeenCalledWith(11, { adj: null, adjusts: [] });
    expect(result.current.override).toBe(null);
  });
});

// Specs for useDeletes (CONTRACT.md "useDeletes") — faithful port of the
// Phase-0 delete verbs (meso_delete.test.js), but pendingDelete/setPendingDelete
// are handed in (lifted from usePlanData) rather than owned here. One shared
// `deleting` in-flight guard across removeExercise + confirmPendingDelete;
// confirmPendingDelete always disarms in a finally, even on failure.
import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import { useDeletes } from "./useDeletes";
import type { Day, Week, PendingDelete } from "./usePlanData";

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function twoExerciseDay(): Day[] {
  return [
    {
      id: 1,
      n: 1,
      name: "Day 1",
      exercises: [
        { id: 9, name: "Squat", sets: "3", reps: "5", load: "100" },
        { id: 10, name: "Bench", sets: "3", reps: "5", load: "80" },
      ],
    },
  ];
}

function twoDayProgram(): Day[] {
  return [
    { id: 1, n: 1, name: "Day 1", exercises: [] },
    { id: 5, n: 2, name: "Day 2", exercises: [] },
  ];
}

function twoWeeks(): Week[] {
  return [
    { id: 1, index: 1, label: "Wk 1", current: true },
    { id: 2, index: 2, label: "Wk 2", current: false },
  ];
}

function planEnvelope() {
  return { ok: true, program: [], weeks: [], phases: [], viewing: null };
}

function setup(program: Day[] = [], weeks: Week[] = []) {
  const applyPlanData = vi.fn();
  const hook = renderHook(() => {
    const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);
    const deletes = useDeletes({
      planId: 7,
      csrf: "tok",
      program,
      weeks,
      pendingDelete,
      setPendingDelete,
      applyPlanData,
    });
    return { ...deletes, pendingDelete };
  });
  return { ...hook, applyPlanData };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("removeExercise", () => {
  it("posts to the prescription-delete URL and applies the reply", async () => {
    const { result, applyPlanData } = setup(twoExerciseDay());
    const data = planEnvelope();
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.removeExercise(0, 0);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/9/delete/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(opts.body).toBe(null);
    expect(applyPlanData).toHaveBeenCalledWith(data);
    expect(result.current.deleting).toBe(false);
  });

  it("shared in-flight guard: a second call while one is pending fires no extra fetch", async () => {
    const { result } = setup(twoExerciseDay());
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(() => new Promise((resolve) => (resolveFetch = resolve))) as unknown as typeof fetch;
    let first: Promise<void>, second: Promise<void>;
    act(() => {
      first = result.current.removeExercise(0, 0);
      second = result.current.removeExercise(0, 0);
    });
    await act(async () => {
      resolveFetch(res(planEnvelope()));
      await Promise.all([first, second]);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it("console.errors, leaves the guard clear, and lets a retry through", async () => {
    const { result } = setup(twoExerciseDay());
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) as unknown as typeof fetch;
    await act(async () => {
      await result.current.removeExercise(0, 0);
    });
    expect(result.current.deleting).toBe(false);
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.removeExercise(0, 0);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("requestRemoveDay / requestRemoveWeek / cancelPendingDelete", () => {
  it("requestRemoveDay arms pendingDelete with the day index", () => {
    const { result } = setup(twoDayProgram());
    act(() => result.current.requestRemoveDay(1));
    expect(result.current.pendingDelete).toEqual({ type: "day", di: 1 });
  });

  it("requestRemoveWeek replaces an armed day (the two verbs are symmetric)", () => {
    const { result } = setup(twoDayProgram(), twoWeeks());
    act(() => result.current.requestRemoveDay(0));
    act(() => result.current.requestRemoveWeek(2));
    expect(result.current.pendingDelete).toEqual({ type: "week", weekId: 2 });
  });

  it("cancelPendingDelete disarms without firing a request", () => {
    const { result } = setup(twoDayProgram());
    act(() => result.current.requestRemoveDay(0));
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    act(() => result.current.cancelPendingDelete());
    expect(result.current.pendingDelete).toBe(null);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

describe("confirmPendingDelete", () => {
  it("day: posts to the session-delete URL, applies the reply, disarms", async () => {
    const { result, applyPlanData } = setup(twoDayProgram());
    const data = planEnvelope();
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    act(() => result.current.requestRemoveDay(1)); // program[1].id === 5
    await act(async () => {
      await result.current.confirmPendingDelete();
    });
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/session/5/delete/");
    expect(opts.method).toBe("POST");
    expect(applyPlanData).toHaveBeenCalledWith(data);
    expect(result.current.pendingDelete).toBe(null);
  });

  it("week: posts to the week-delete URL, applies the reply, disarms", async () => {
    const { result, applyPlanData } = setup([], twoWeeks());
    const data = planEnvelope();
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    act(() => result.current.requestRemoveWeek(2));
    await act(async () => {
      await result.current.confirmPendingDelete();
    });
    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/week/2/delete/");
    expect(applyPlanData).toHaveBeenCalledWith(data);
    expect(result.current.pendingDelete).toBe(null);
  });

  it("is a no-op with nothing armed", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.confirmPendingDelete();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("shared in-flight guard: no-ops while a removeExercise is still in flight", async () => {
    const { result } = setup(
      [{ id: 1, n: 1, name: "Day 1", exercises: [{ id: 9, name: "Squat", sets: "3", reps: "5", load: "100" }] }],
      twoWeeks(),
    );
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(() => new Promise((resolve) => (resolveFetch = resolve))) as unknown as typeof fetch;
    let first: Promise<void>, second: Promise<void>;
    act(() => {
      first = result.current.removeExercise(0, 0);
      result.current.requestRemoveWeek(2);
      second = result.current.confirmPendingDelete();
    });
    await act(async () => {
      resolveFetch(res(planEnvelope()));
      await Promise.all([first, second]);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it("always disarms in a finally, even on failure — clearing the way for a retry", async () => {
    const { result } = setup(twoDayProgram());
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) as unknown as typeof fetch;
    act(() => result.current.requestRemoveDay(1));
    await act(async () => {
      await result.current.confirmPendingDelete();
    });
    expect(result.current.deleting).toBe(false);
    expect(result.current.pendingDelete).toBe(null);

    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    act(() => result.current.requestRemoveDay(1));
    await act(async () => {
      await result.current.confirmPendingDelete();
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });
});

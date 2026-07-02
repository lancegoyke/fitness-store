// Specs for usePlanData — the sole owner of program/weeks/phases (CONTRACT.md
// "usePlanData"). Ported behavior: applyPlanData is the central sink (also
// disarms pendingDelete), updateExerciseField writes per-keystroke without
// persisting, addExercise/addDay row-merge + adoptHistory, switchWeek/addWeek/
// setCurrentWeek all applyPlanData, week methods no-op on a redundant switch.
import { act, renderHook } from "@testing-library/react";
import { usePlanData } from "./usePlanData";
import { EMPTY_HISTORY } from "../lib/api";
import type { Day, Week, Phase, HistoryState } from "../lib/api";

const HISTORY_BOTH: HistoryState = {
  can_undo: true,
  can_redo: true,
  undo_label: "Edited Box Squat",
  redo_label: "Deleted Day 2",
};

function day(overrides: Partial<Day> = {}): Day {
  return { id: 1, n: 1, name: "Day 1", exercises: [], ...overrides };
}

function week(overrides: Partial<Week> = {}): Week {
  return { id: 1, index: 1, label: "Wk 1", current: true, ...overrides };
}

function phase(overrides: Partial<Phase> = {}): Phase {
  return { name: "Hypertrophy", weeks: "4 wk", state: "current", ...overrides };
}

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function sentBody(n = 0) {
  const mockFetch = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  const call = mockFetch.mock.calls[n] as [string, RequestInit];
  return call[1].body == null ? null : JSON.parse(call[1].body as string);
}

function setup(overrides: Partial<Parameters<typeof usePlanData>[2]> = {}) {
  const initial = {
    program: [day()],
    weeks: [week()],
    phases: [phase()],
    viewing: 1,
    ...overrides,
  };
  return renderHook(() =>
    usePlanData(7, "tok", initial, { athlete: null, group: null }),
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("usePlanData initial hydration", () => {
  it("seeds program/weeks/phases/viewedWeekId from the initial payload", () => {
    const { result } = setup();
    expect(result.current.program).toHaveLength(1);
    expect(result.current.weeks).toHaveLength(1);
    expect(result.current.phases).toHaveLength(1);
    expect(result.current.viewedWeekId).toBe(1);
  });

  it("defaults history to EMPTY_HISTORY when the initial payload has none", () => {
    const { result } = setup();
    expect(result.current.history).toEqual(EMPTY_HISTORY);
  });

  it("adopts an initial history when given", () => {
    const { result } = setup({ history: HISTORY_BOTH });
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });

  it("hydrates athlete/group once and never mutates them via applyPlanData", () => {
    const athlete = { name: "Maya", initials: "MO", contraindications: [] };
    const { result } = renderHook(() =>
      usePlanData(
        7,
        "tok",
        { program: [day()], weeks: [week()], phases: [phase()], viewing: 1 },
        { athlete, group: null },
      ),
    );
    expect(result.current.athlete).toBe(athlete);
    act(() => {
      result.current.applyPlanData({
        program: [],
        weeks: [],
        phases: [],
        viewing: null,
      });
    });
    expect(result.current.athlete).toBe(athlete);
  });

  it("starts with no pending delete", () => {
    const { result } = setup();
    expect(result.current.pendingDelete).toBe(null);
  });
});

describe("applyPlanData", () => {
  it("swaps program/weeks/phases/viewedWeekId and adopts history", () => {
    const { result } = setup();
    act(() => {
      result.current.applyPlanData({
        program: [day({ id: 2 })],
        weeks: [week({ id: 2, label: "Wk 2" })],
        phases: [phase({ name: "Peak" })],
        viewing: 2,
        history: HISTORY_BOTH,
      });
    });
    expect(result.current.program[0]?.id).toBe(2);
    expect(result.current.weeks[0]?.id).toBe(2);
    expect(result.current.phases[0]?.name).toBe("Peak");
    expect(result.current.viewedWeekId).toBe(2);
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });

  it("falls back to null viewedWeekId and EMPTY_HISTORY when the payload omits them", () => {
    const { result } = setup();
    act(() => {
      result.current.applyPlanData({ program: [], weeks: [], phases: [], viewing: null });
    });
    expect(result.current.viewedWeekId).toBe(null);
    expect(result.current.history).toEqual(EMPTY_HISTORY);
  });

  it("always disarms pendingDelete, even across an unrelated swap", () => {
    const { result } = setup();
    act(() => {
      result.current.setPendingDelete({ type: "day", di: 1 });
    });
    expect(result.current.pendingDelete).not.toBe(null);
    act(() => {
      result.current.applyPlanData({ program: [], weeks: [], phases: [], viewing: null });
    });
    expect(result.current.pendingDelete).toBe(null);
  });
});

describe("adoptHistory", () => {
  it("adopts history from a row-merge reply", () => {
    const { result } = setup();
    act(() => {
      result.current.adoptHistory({ history: HISTORY_BOTH });
    });
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });

  it("is a no-op when the reply carries no history", () => {
    const { result } = setup({ history: HISTORY_BOTH });
    act(() => {
      result.current.adoptHistory({});
    });
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });
});

describe("patchExercise", () => {
  it("merges a partial patch onto the matching exercise by id, leaving others untouched", () => {
    const { result } = setup({
      program: [
        day({
          exercises: [
            { id: 9, name: "Squat", sets: "3", reps: "5", load: "100" },
            { id: 10, name: "Bench", sets: "3", reps: "5", load: "80" },
          ],
        }),
      ],
    });
    act(() => {
      result.current.patchExercise(9, { one_rm: "150", one_rm_source: "manual" });
    });
    const [squat, bench] = result.current.program[0]!.exercises;
    expect(squat).toMatchObject({ id: 9, name: "Squat", one_rm: "150", one_rm_source: "manual" });
    expect(bench).toEqual({ id: 10, name: "Bench", sets: "3", reps: "5", load: "80" });
  });
});

describe("updateExerciseField", () => {
  it("writes the field into program immediately without persisting", () => {
    const { result } = setup({
      program: [day({ exercises: [{ id: 9, name: "Squat", sets: "3", reps: "5", load: "100" }] })],
    });
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    act(() => {
      result.current.updateExerciseField(0, 0, "name", "Front Squat");
    });
    expect(result.current.program[0]!.exercises[0]!.name).toBe("Front Squat");
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

describe("addExercise", () => {
  it("POSTs to the session-exercise endpoint, pushes the reply row, and adopts history", async () => {
    const { result } = setup({ program: [day({ id: 1, exercises: [] })] });
    const newRow = { id: 12, name: "New exercise", sets: "3", reps: "10", load: "" };
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, prescription: newRow, history: HISTORY_BOTH }),
    );
    await act(async () => {
      await result.current.addExercise(0);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/session/1/exercise/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(result.current.program[0]!.exercises).toEqual([newRow]);
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });
});

describe("addDay", () => {
  it("POSTs {week_id: viewedWeekId}, pushes the reply day, and adopts history", async () => {
    const { result } = setup({ viewing: 5, program: [day({ id: 1 })] });
    const newDay = { id: 2, n: 2, name: "Day 2", bias: "", exercises: [{ id: 9, name: "New exercise" }] };
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, session: newDay, history: HISTORY_BOTH }),
    );
    await act(async () => {
      await result.current.addDay();
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/session/");
    expect(sentBody().week_id).toBe(5);
    expect(result.current.program).toHaveLength(2);
    expect(result.current.program[1]).toEqual(newDay);
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });
});

describe("switchWeek", () => {
  it("GETs the week endpoint and applies the reply", async () => {
    const { result } = setup({ viewing: 1 });
    const data = {
      ok: true,
      program: [day({ id: 10, name: "Lower" })],
      weeks: [week({ id: 1, current: true }), week({ id: 2, label: "Wk 2", current: false })],
      phases: [phase()],
      viewing: 2,
    };
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.switchWeek(2);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/week/2/");
    expect(opts).toBeUndefined();
    expect(result.current.viewedWeekId).toBe(2);
  });

  it("is a no-op when already viewing that week", async () => {
    const { result } = setup({ viewing: 2 });
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.switchWeek(2);
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("leaves state unchanged and console.errors on a failed fetch", async () => {
    const { result } = setup({ viewing: 1, program: [day({ id: 99 })] });
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 404)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.switchWeek(2);
    });
    expect(result.current.viewedWeekId).toBe(1);
    expect(result.current.program[0]!.id).toBe(99);
    expect(console.error).toHaveBeenCalled();
  });
});

describe("addWeek", () => {
  it("POSTs the week endpoint and applies the reply", async () => {
    const { result } = setup({ viewing: 1 });
    const data = {
      ok: true,
      program: [day()],
      weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })],
      phases: [phase()],
      viewing: 2,
    };
    globalThis.fetch = vi.fn().mockResolvedValue(res(data, true, 201)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.addWeek();
    });
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/week/");
    expect(opts.method).toBe("POST");
    expect(result.current.viewedWeekId).toBe(2);
    expect(result.current.weeks).toHaveLength(2);
  });
});

describe("setCurrentWeek", () => {
  it("POSTs the current endpoint and applies the reply", async () => {
    const { result } = setup({ viewing: 2, weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })] });
    const data = {
      ok: true,
      program: [day()],
      weeks: [week({ id: 1, current: false }), week({ id: 2, label: "Wk 2", current: true })],
      phases: [phase()],
      viewing: 2,
    };
    globalThis.fetch = vi.fn().mockResolvedValue(res(data)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.setCurrentWeek(2);
    });
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/week/2/current/");
    expect(opts.method).toBe("POST");
    expect(result.current.viewedIsCurrent).toBe(true);
  });
});

describe("derived values", () => {
  it("currentWeek/viewedWeek/weekIsViewed/viewedIsCurrent track the viewed pointer", () => {
    const { result } = setup({
      viewing: 2,
      weeks: [week({ id: 1, current: true }), week({ id: 2, label: "Wk 2", current: false })],
    });
    expect(result.current.currentWeek?.id).toBe(1);
    expect(result.current.viewedWeek?.id).toBe(2);
    expect(result.current.weekIsViewed(result.current.weeks[1]!)).toBe(true);
    expect(result.current.weekIsViewed(result.current.weeks[0]!)).toBe(false);
    expect(result.current.viewedIsCurrent).toBe(false);
  });

  it("cycleLabel/weekHeading/blockHeading/deliverHref derive from phase + viewed week", () => {
    const { result } = setup({
      viewing: 2,
      weeks: [week({ id: 2, label: "Wk 2", phase: "Accum", current: false })],
      phases: [phase({ name: "Hypertrophy" })],
    });
    expect(result.current.cycleLabel).toBe("Hypertrophy · Wk 2 / 1");
    expect(result.current.weekHeading).toBe("Wk 2 — Accum");
    expect(result.current.blockHeading).toBe("Hypertrophy — 4 wk mesocycle");
    expect(result.current.deliverHref).toBe("/meso/deliver/7/?week=2");
  });
});

describe("stale replies after a week switch", () => {
  // Alpine captured the day ARRAY by reference, so a reply landing after a
  // week switch mutated a detached object — invisible and harmless. The port
  // must match: a row-merge reply whose day (or week) is no longer on screen
  // is dropped — the row exists server-side and re-hydrates on switch-back —
  // while its history (a valid post-mutation fact) is still adopted.
  it("addExercise drops a reply that resolves after the grid swapped", async () => {
    const { result } = setup({ program: [day({ id: 1, exercises: [] })] });
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn().mockReturnValue(
      new Promise((r) => {
        resolveFetch = r;
      }),
    );
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.addExercise(0);
    });
    act(() => {
      result.current.applyPlanData({
        program: [day({ id: 99, exercises: [] })],
        weeks: [week({ id: 2 })],
        phases: [],
        viewing: 2,
      });
    });
    await act(async () => {
      resolveFetch(
        res({ ok: true, prescription: { id: 12, name: "New exercise" }, history: HISTORY_BOTH }),
      );
      await pending;
    });
    expect(result.current.program[0]!.exercises).toEqual([]);
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });

  it("addDay drops a reply that resolves after the viewed week changed", async () => {
    const { result } = setup({ program: [day({ id: 1 })] });
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn().mockReturnValue(
      new Promise((r) => {
        resolveFetch = r;
      }),
    );
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.addDay();
    });
    act(() => {
      result.current.applyPlanData({
        program: [day({ id: 99 })],
        weeks: [week({ id: 2 })],
        phases: [],
        viewing: 2,
      });
    });
    await act(async () => {
      resolveFetch(
        res({ ok: true, session: { id: 55, n: 2, name: "Day 2", exercises: [] }, history: HISTORY_BOTH }),
      );
      await pending;
    });
    expect(result.current.program.map((d) => d.id)).toEqual([99]);
    expect(result.current.history).toEqual(HISTORY_BOTH);
  });
});

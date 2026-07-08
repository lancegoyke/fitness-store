// Specs for useGrid (P1 multi-week table) — a self-contained state-owning
// hook for MesoTable. Cell edits (patchCell/renameExercise) are optimistic +
// fire-and-forget, mirroring useAutosave's semantics (CONTRACT.md
// "useAutosave") — no rollback on failure. Structural verbs (add/remove
// day|week|exercise, undo/redo) POST then refetch the whole grid (GET
// grid/), mirroring usePlanData/useReorder's ref-guard idiom so concurrent
// structural ops can't race.
import { act, renderHook, waitFor } from "@testing-library/react";
import { useGrid } from "./useGrid";
import type { GridCell, GridDay, GridRow, GridWeek, MesoGrid } from "../lib/api";

function week(overrides: Partial<GridWeek> = {}): GridWeek {
  return {
    id: 1,
    index: 0,
    label: "Wk 1",
    phase: "Accum",
    deload: false,
    current: true,
    delivered_at: null,
    ...overrides,
  };
}

function cell(overrides: Partial<GridCell> = {}): GridCell {
  return {
    prescription_id: 100,
    sets: "3",
    reps: "5",
    load: "100",
    load_type: "abs",
    rpe: "8",
    rest: "90",
    note: "",
    skipped: false,
    swap_name: "",
    swap_exercise_id: null,
    swap_display: "",
    ...overrides,
  };
}

function row(overrides: Partial<GridRow> = {}): GridRow {
  return {
    exercise_slot_id: 9,
    name: "Squat",
    exercise_id: 55,
    order: 0,
    tags: [],
    cells: { "1": cell() },
    ...overrides,
  };
}

function day(overrides: Partial<GridDay> = {}): GridDay {
  return {
    session_slot_id: 1,
    session_id: 11,
    day_number: 1,
    name: "Lower",
    bias: "",
    order: 0,
    rows: [row()],
    ...overrides,
  };
}

function grid(overrides: Partial<MesoGrid> = {}): MesoGrid {
  return {
    mesocycle: { id: 1, plan_id: 7, name: "Block 1", week_count: 1 },
    weeks: [week()],
    days: [day()],
    history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" },
    ...overrides,
  };
}

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function sentBody(n = 0) {
  const mockFetch = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  const call = mockFetch.mock.calls[n] as [string, RequestInit];
  return call[1].body == null ? null : JSON.parse(call[1].body as string);
}

function setup(initialGrid: MesoGrid | null = grid()) {
  return renderHook(() => useGrid({ planId: 7, csrf: "tok", initialGrid }));
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("initial hydration", () => {
  it("seeds grid/history from initialGrid", () => {
    const { result } = setup();
    expect(result.current.grid?.days).toHaveLength(1);
    expect(result.current.history).toEqual({ can_undo: false, can_redo: false, undo_label: "", redo_label: "" });
  });

  it("tolerates a null initialGrid", () => {
    const { result } = setup(null);
    expect(result.current.grid).toBe(null);
  });
});

describe("patchCell", () => {
  it("optimistically updates the cell and POSTs only the given patch, adopting history", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({
        ok: true,
        prescription: {},
        history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: "" },
      }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.patchCell(100, { sets: "4" });
    });

    // Optimistic: reflected immediately, before the fetch resolves.
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.sets).toBe("4");
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/100/");
    expect(opts.method).toBe("POST");
    expect(sentBody()).toEqual({ sets: "4" });

    await waitFor(() => expect(result.current.history.can_undo).toBe(true));
    expect(result.current.history.undo_label).toBe("Edited Squat");
  });

  it("leaves other cells/fields untouched", () => {
    const { result } = setup(
      grid({
        days: [
          day({
            rows: [
              row({ exercise_slot_id: 9, cells: { "1": cell({ prescription_id: 100, reps: "5" }) } }),
              row({ exercise_slot_id: 10, cells: { "1": cell({ prescription_id: 200, sets: "5" }) } }),
            ],
          }),
        ],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.patchCell(100, { sets: "4" });
    });
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]).toMatchObject({ sets: "4", reps: "5" });
    expect(result.current.grid?.days[0]?.rows[1]?.cells["1"]).toMatchObject({ sets: "5" });
  });

  it("console.errors on failure without rolling back the optimistic update", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("boom")) as unknown as typeof fetch;

    act(() => {
      result.current.patchCell(100, { note: "left knee sore" });
    });

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.note).toBe("left knee sore");
  });
});

describe("renameExercise", () => {
  it("POSTs {name} to the row's FIRST live week's (non-swapped) cell, optimistically updating row.name", async () => {
    const { result } = setup(
      grid({
        weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })],
        days: [
          day({
            rows: [
              row({
                exercise_slot_id: 9,
                name: "Squat",
                cells: {
                  "1": cell({ prescription_id: 100 }),
                  "2": cell({ prescription_id: 101, swap_name: "Leg Press", swap_exercise_id: 77 }),
                },
              }),
            ],
          }),
        ],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, history: { can_undo: true, can_redo: false, undo_label: "Renamed Squat", redo_label: "" } }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.renameExercise(9, "Front Squat");
    });

    expect(result.current.grid?.days[0]?.rows[0]?.name).toBe("Front Squat");
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/100/"); // week[0]'s cell, not the swapped week[1] one
    expect(JSON.parse(opts.body as string)).toEqual({ name: "Front Squat" });
    await waitFor(() => expect(result.current.history.undo_label).toBe("Renamed Squat"));
  });

  it("retargets to the first NON-swapped cell when week[0]'s cell is itself the swap", async () => {
    // prescription_patch treats a `name` edit on a swapped cell as editing
    // the one-week swap, not the block ExerciseSlot.name — so renaming must
    // never target week[0]'s cell when THAT week is the swapped one.
    const { result } = setup(
      grid({
        weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })],
        days: [
          day({
            rows: [
              row({
                exercise_slot_id: 9,
                name: "Squat",
                cells: {
                  "1": cell({ prescription_id: 100, swap_name: "Leg Press", swap_exercise_id: 77 }),
                  "2": cell({ prescription_id: 101 }),
                },
              }),
            ],
          }),
        ],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, history: { can_undo: true, can_redo: false, undo_label: "Renamed Squat", redo_label: "" } }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.renameExercise(9, "Front Squat");
    });

    expect(result.current.grid?.days[0]?.rows[0]?.name).toBe("Front Squat");
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/101/"); // week[1]'s (unswapped) cell, not the swapped week[0] one
    expect(JSON.parse(opts.body as string)).toEqual({ name: "Front Squat" });
    await waitFor(() => expect(result.current.history.undo_label).toBe("Renamed Squat"));
  });
});

describe("addExercise", () => {
  it("POSTs session/{sessionId}/exercise/ with a null body, then refetches the grid", async () => {
    const initial = grid();
    const { result } = setup(initial);
    const refreshed = grid({
      days: [day({ rows: [row(), row({ exercise_slot_id: 20, name: "New exercise" })] })],
    });
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...refreshed })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.addExercise(initial.days[0]!);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/session/11/exercise/");
    expect(calls[0]![1].method).toBe("POST");
    expect(calls[0]![1].body).toBe(null);
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.days[0]?.rows).toHaveLength(2);
  });
});

describe("removeExercise", () => {
  it("POSTs prescription/{cellId}/delete/ for the row's first-week cell, then refetches", async () => {
    const initial = grid();
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid({ days: [day({ rows: [] })] }) })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.removeExercise(9);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/prescription/100/delete/");
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.days[0]?.rows).toHaveLength(0);
  });
});

describe("addDay", () => {
  it("POSTs session/ with {week_id: current week id}, then refetches", async () => {
    const initial = grid({ weeks: [week({ id: 1, current: true }), week({ id: 2, current: false })] });
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid({ days: [day(), day({ session_slot_id: 2, name: "Upper" })] }) })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.addDay();
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/session/");
    expect(JSON.parse(calls[0]![1].body as string)).toEqual({ week_id: 1 });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.days).toHaveLength(2);
  });
});

describe("removeDay", () => {
  it("POSTs session/{sessionId}/delete/, then refetches", async () => {
    const initial = grid();
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid({ days: [] }) })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.removeDay(initial.days[0]!);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/session/11/delete/");
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.days).toHaveLength(0);
  });
});

describe("addWeek", () => {
  it("POSTs week/ with a null body, then refetches", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(
        res({ ok: true, ...grid({ weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })] }) }),
      ) as unknown as typeof fetch;

    await act(async () => {
      await result.current.addWeek();
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/week/");
    expect(calls[0]![1].body).toBe(null);
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.weeks).toHaveLength(2);
  });
});

describe("removeWeek", () => {
  it("POSTs week/{weekId}/delete/, then refetches", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.removeWeek(1);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/week/1/delete/");
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });
});

describe("setCurrentWeek", () => {
  it("POSTs week/{weekId}/current/ with a null body, then refetches", async () => {
    const initial = grid({ weeks: [week({ id: 1, current: true }), week({ id: 2, current: false })] });
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(
        res({ ok: true, ...grid({ weeks: [week({ id: 1, current: false }), week({ id: 2, current: true })] }) }),
      ) as unknown as typeof fetch;

    await act(async () => {
      await result.current.setCurrentWeek(2);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/week/2/current/");
    expect(calls[0]![1].body).toBe(null);
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.weeks[1]?.current).toBe(true);
  });
});

describe("undo/redo", () => {
  it("undo POSTs {week_id: current week id} to undo/, then refetches the grid (ignoring its own envelope)", async () => {
    const initial = grid({
      history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: "" },
    });
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true, program: [], weeks: [], phases: [], viewing: null })) // undo's own single-week envelope, ignored
      .mockResolvedValueOnce(
        res({
          ok: true,
          ...grid({ history: { can_undo: false, can_redo: true, undo_label: "", redo_label: "Edited Squat" } }),
        }),
      ) as unknown as typeof fetch;

    await act(async () => {
      await result.current.undo();
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/undo/");
    expect(JSON.parse(calls[0]![1].body as string)).toEqual({ week_id: 1 });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.history.can_undo).toBe(false);
    expect(result.current.history.can_redo).toBe(true);
  });

  it("redo POSTs {week_id} to redo/, then refetches", async () => {
    const initial = grid({
      history: { can_undo: false, can_redo: true, undo_label: "", redo_label: "Edited Squat" },
    });
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.redo();
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/redo/");
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });

  it("undo is a no-op when history.can_undo is false", async () => {
    const { result } = setup(grid({ history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" } }));
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.undo();
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

describe("concurrency guard on structural ops", () => {
  it("a second structural call while one is in flight is a no-op; busy reflects it", async () => {
    const { result } = setup();
    let resolvePost!: (v: unknown) => void;
    const fetchMock = vi.fn();
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.addWeek();
      second = result.current.addWeek();
    });

    expect(result.current.busy).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1); // the second call bailed before POSTing

    fetchMock.mockResolvedValueOnce(res({ ok: true, ...grid() })); // the refetch GET

    await act(async () => {
      resolvePost(res({ ok: true }));
      await first;
      await second;
    });

    expect(fetchMock).toHaveBeenCalledTimes(2); // POST + GET only — no third/fourth call
    expect(result.current.busy).toBe(false);
  });
});

describe("refetchGrid", () => {
  it("GETs the grid endpoint (no options) and adopts the reply", async () => {
    const { result } = setup();
    const data = grid({ mesocycle: { id: 1, plan_id: 7, name: "Renamed block", week_count: 1 } });
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true, ...data })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.refetchGrid();
    });

    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/grid/");
    expect(opts).toBeUndefined();
    expect(result.current.grid?.mesocycle.name).toBe("Renamed block");
  });

  it("console.errors and leaves state unchanged on a failed refetch", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 500)) as unknown as typeof fetch;

    await act(async () => {
      await result.current.refetchGrid();
    });

    expect(console.error).toHaveBeenCalled();
    expect(result.current.grid?.mesocycle.name).toBe("Block 1");
  });
});

// --- P2 exceptions: skip / swap / fill / add-this-week -------------------
// These four verbs are STRUCTURAL (contract "useGrid.ts — new verbs"): each
// awaits its POST then refetches the whole grid, sharing the same busyRef
// guard as add/removeExercise|Day|Week — mirroring those existing specs.

describe("skipCell", () => {
  it("POSTs {skipped:true} to prescription/{cellId}/skip/, then refetches the grid", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(
        res({ ok: true, ...grid({ days: [day({ rows: [row({ cells: { "1": cell({ skipped: true }) } })] })] }) }),
      ) as unknown as typeof fetch;

    await act(async () => {
      await result.current.skipCell(100, true);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/prescription/100/skip/");
    expect(calls[0]![1].method).toBe("POST");
    expect(JSON.parse(calls[0]![1].body as string)).toEqual({ skipped: true });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.skipped).toBe(true);
  });

  it("unskip POSTs {skipped:false}", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.skipCell(100, false);
    });

    expect(sentBody()).toEqual({ skipped: false });
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });
});

describe("swapCell", () => {
  it('sends {swap_name} to prescription/{cellId}/swap/ for a non-blank name, then refetches', async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(
        res({
          ok: true,
          ...grid({
            days: [
              day({
                rows: [row({ cells: { "1": cell({ swap_name: "Front Squat", swap_display: "Front Squat" }) } })],
              }),
            ],
          }),
        }),
      ) as unknown as typeof fetch;

    await act(async () => {
      await result.current.swapCell(100, "Front Squat");
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/prescription/100/swap/");
    expect(calls[0]![1].method).toBe("POST");
    expect(JSON.parse(calls[0]![1].body as string)).toEqual({ swap_name: "Front Squat" });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.swap_display).toBe("Front Squat");
  });

  it("sends {clear:true} for a blank name", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.swapCell(100, "");
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/prescription/100/swap/");
    expect(sentBody()).toEqual({ clear: true });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });
});

describe("fillAcrossWeeks", () => {
  it("POSTs {} to prescription/{cellId}/fill/, then refetches the grid", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true, filled: 2 }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.fillAcrossWeeks(100);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/prescription/100/fill/");
    expect(calls[0]![1].method).toBe("POST");
    expect(sentBody()).toEqual({});
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });
});

describe("addExerciseThisWeek", () => {
  it("POSTs {week_id} to session/{sessionId}/exercise/, then refetches the grid", async () => {
    const initial = grid();
    const { result } = setup(initial);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.addExerciseThisWeek(initial.days[0]!, 2);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/session/11/exercise/");
    expect(calls[0]![1].method).toBe("POST");
    expect(JSON.parse(calls[0]![1].body as string)).toEqual({ week_id: 2 });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });
});

describe("concurrency guard covers the new P2 verbs", () => {
  it("a concurrent call to a different structural verb while one is in flight is a no-op", async () => {
    const { result } = setup();
    let resolvePost!: (v: unknown) => void;
    const fetchMock = vi.fn();
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.skipCell(100, true);
      second = result.current.swapCell(100, "Leg Press");
    });

    expect(result.current.busy).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1); // the second call bailed before POSTing

    fetchMock.mockResolvedValueOnce(res({ ok: true, ...grid() })); // the refetch GET

    await act(async () => {
      resolvePost(res({ ok: true }));
      await first;
      await second;
    });

    expect(fetchMock).toHaveBeenCalledTimes(2); // POST + GET only — no third/fourth call
    expect(result.current.busy).toBe(false);
  });
});

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
    delivered_at: null,
    ...overrides,
  };
}

function cell(overrides: Partial<GridCell> = {}): GridCell {
  return {
    prescription_id: 100,
    text: "3 x 5, RPE 8, 100",
    skipped: false,
    lines: [],
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
    tempo: "",
    rest: "",
    note: "",
    cells: { "1": cell() },
    ...overrides,
  };
}

function day(overrides: Partial<GridDay> = {}): GridDay {
  return {
    session_slot_id: 1,
    session_id: 11,
    session_ids: { "1": 11 },
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
      result.current.patchCell(100, { text: "4 x 6, RPE 9" });
    });

    // Optimistic: reflected immediately, before the fetch resolves.
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.text).toBe("4 x 6, RPE 9");
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/prescription/100/");
    expect(opts.method).toBe("POST");
    expect(sentBody()).toEqual({ text: "4 x 6, RPE 9" });

    await waitFor(() => expect(result.current.history.can_undo).toBe(true));
    expect(result.current.history.undo_label).toBe("Edited Squat");
  });

  it("leaves other cells (and the patched cell's other fields) untouched", () => {
    const { result } = setup(
      grid({
        days: [
          day({
            rows: [
              row({
                exercise_slot_id: 9,
                cells: { "1": cell({ prescription_id: 100, text: "3 x 5", lines: [{ id: 5, line: 1, text: "RPE 8" }] }) },
              }),
              row({ exercise_slot_id: 10, cells: { "1": cell({ prescription_id: 200, text: "5 x 5" }) } }),
            ],
          }),
        ],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.patchCell(100, { text: "4 x 5" });
    });
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]).toMatchObject({
      text: "4 x 5",
      lines: [{ id: 5, line: 1, text: "RPE 8" }],
    });
    expect(result.current.grid?.days[0]?.rows[1]?.cells["1"]).toMatchObject({ text: "5 x 5" });
  });

  it("console.errors on failure without rolling back the optimistic update", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("boom")) as unknown as typeof fetch;

    act(() => {
      result.current.patchCell(100, { text: "AMRAP" });
    });

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.text).toBe("AMRAP");
  });
});

describe("renameExercise", () => {
  it("POSTs {name} to the row's FIRST live week's cell (the identity cell), optimistically updating row.name", async () => {
    // Phase 2a: the one-week swap fields are gone, so identity is always the
    // block-shared slot's — the first live week's cell is the stable target
    // (prescription_patch's `name` branch renames the slot).
    const { result } = setup(
      grid({
        weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2" })],
        days: [
          day({
            rows: [
              row({
                exercise_slot_id: 9,
                name: "Squat",
                cells: {
                  "1": cell({ prescription_id: 100 }),
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
    expect(url).toBe("/meso/api/plan/7/prescription/100/"); // week[0]'s cell
    expect(JSON.parse(opts.body as string)).toEqual({ name: "Front Squat" });
    await waitFor(() => expect(result.current.history.undo_label).toBe("Renamed Squat"));
  });
});

// --- Phase 2a text-first: sub-line writes + per-row columns ----------------
// Both are optimistic + fire-and-forget, mirroring patchCell's semantics
// above — local repaint immediately, POST not awaited, no rollback.

describe("writeCellLine", () => {
  it("POSTs {week_id, line, text} to row/{slotId}/cell/, optimistically inserting the new sub-line, adopting history", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: "" } }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.writeCellLine(9, 1, 1, "RPE 8");
    });

    // Optimistic: the sub-line appears immediately, before the fetch resolves.
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([{ line: 1, text: "RPE 8" }]);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/row/9/cell/");
    expect(opts.method).toBe("POST");
    expect(sentBody()).toEqual({ week_id: 1, line: 1, text: "RPE 8" });
    await waitFor(() => expect(result.current.history.undo_label).toBe("Edited Squat"));
  });

  it("updates an existing sub-line in place (id preserved), keeping line order", () => {
    const { result } = setup(
      grid({
        days: [
          day({
            rows: [
              row({
                cells: {
                  "1": cell({
                    lines: [
                      { id: 5, line: 1, text: "RPE 8" },
                      { id: 6, line: 2, text: "slow eccentric" },
                    ],
                  }),
                },
              }),
            ],
          }),
        ],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;

    act(() => {
      result.current.writeCellLine(9, 1, 1, "RPE 9");
    });

    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([
      { id: 5, line: 1, text: "RPE 9" },
      { id: 6, line: 2, text: "slow eccentric" },
    ]);
  });

  it("inserts a new line in line order between existing ones", () => {
    const { result } = setup(
      grid({
        days: [day({ rows: [row({ cells: { "1": cell({ lines: [{ id: 6, line: 3, text: "cue" }] }) } })] })],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;

    act(() => {
      result.current.writeCellLine(9, 1, 1, "RPE 8");
    });

    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([
      { line: 1, text: "RPE 8" },
      { id: 6, line: 3, text: "cue" },
    ]);
  });

  it("line 0 updates cell.text locally (the prescription line, no sub-line entry)", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;

    act(() => {
      result.current.writeCellLine(9, 1, 0, "4 x 6");
    });

    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.text).toBe("4 x 6");
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([]);
    expect(sentBody()).toEqual({ week_id: 1, line: 0, text: "4 x 6" });
  });

  it("console.errors on failure without rolling back the optimistic update", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("boom")) as unknown as typeof fetch;

    act(() => {
      result.current.writeCellLine(9, 1, 1, "RPE 8");
    });

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([{ line: 1, text: "RPE 8" }]);
  });
});

describe("patchRowColumns", () => {
  it("POSTs the partial patch to row/{slotId}/, optimistically updating the row columns, adopting history", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: "" } }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.patchRowColumns(9, { tempo: "31X1" });
    });

    expect(result.current.grid?.days[0]?.rows[0]?.tempo).toBe("31X1");
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("/meso/api/plan/7/row/9/");
    expect(opts.method).toBe("POST");
    expect(sentBody()).toEqual({ tempo: "31X1" });
    await waitFor(() => expect(result.current.history.undo_label).toBe("Edited Squat"));
  });

  it("patches only the given columns, leaving the others untouched", () => {
    const { result } = setup(
      grid({ days: [day({ rows: [row({ tempo: "20X0", rest: "2 min", note: "brace hard" })] })] }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;

    act(() => {
      result.current.patchRowColumns(9, { rest: "3 min" });
    });

    expect(result.current.grid?.days[0]?.rows[0]).toMatchObject({ tempo: "20X0", rest: "3 min", note: "brace hard" });
  });

  it("console.errors on failure without rolling back the optimistic update", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("boom")) as unknown as typeof fetch;

    act(() => {
      result.current.patchRowColumns(9, { note: "left knee sore" });
    });

    await waitFor(() => expect(console.error).toHaveBeenCalled());
    expect(result.current.grid?.days[0]?.rows[0]?.note).toBe("left knee sore");
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
  it("POSTs session/ with {week_id: the viewed (first) week's id}, then refetches", async () => {
    const initial = grid({ weeks: [week({ id: 1 }), week({ id: 2 })] });
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
        res({ ok: true, ...grid({ weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2" })] }) }),
      ) as unknown as typeof fetch;

    await act(async () => {
      await result.current.addWeek();
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/week/");
    // The new week must land in the block this grid is SHOWING, so the post
    // names it rather than letting the server pick a default that can diverge.
    expect(JSON.parse(calls[0]![1].body)).toEqual({ mesocycle_id: 1 });
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

describe("undo/redo", () => {
  it("undo POSTs {week_id: the viewed (first) week's id} to undo/, then refetches the grid (ignoring its own envelope)", async () => {
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

describe("reorder -> undo integration (issue #455 phase A2)", () => {
  it("a reorder POST's fresh undo_label flows through to a subsequent undo, in the right fetch sequence", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true })) // POST reorder
      .mockResolvedValueOnce(
        res({
          ok: true,
          ...grid({ history: { can_undo: true, can_redo: false, undo_label: "Reordered exercises", redo_label: "" } }),
        }),
      ) // GET grid (post-reorder)
      .mockResolvedValueOnce(res({ ok: true, program: [], weeks: [], phases: [], viewing: null })) // POST undo (its own single-week envelope, ignored)
      .mockResolvedValueOnce(
        res({
          ok: true,
          ...grid({ history: { can_undo: false, can_redo: true, undo_label: "", redo_label: "Reordered exercises" } }),
        }),
      ) as unknown as typeof fetch; // GET grid (post-undo)

    await act(async () => {
      await result.current.reorderExercises(11, [101, 100]);
    });
    expect(result.current.history.undo_label).toBe("Reordered exercises");

    await act(async () => {
      await result.current.undo();
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.map((c) => c[0])).toEqual([
      "/meso/api/plan/7/session/11/reorder/",
      "/meso/api/plan/7/grid/",
      "/meso/api/plan/7/undo/",
      "/meso/api/plan/7/grid/",
    ]);
    expect(result.current.history.can_undo).toBe(false);
    expect(result.current.history.redo_label).toBe("Reordered exercises");
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

  // Issue #455 phase A5 regression: plan/athlete/phases are now the
  // front-end's ONLY source for the top bar / left rail / block view (the
  // one-week plan_data owner that used to carry them is gone) — a refetch
  // that silently dropped them would blank that chrome after the very next
  // structural edit (add day, add week, undo, ...).
  it("carries the new plan/athlete/phases fields through a refetch", async () => {
    const { result } = setup();
    const data = grid({
      plan: { id: 7, title: "Renamed plan", status: "active", unit: "kg" },
      athlete: { name: "Devon Reyes", initials: "DR", goal: "Strength", contraindications: [] },
      phases: [{ name: "Hypertrophy", weeks: "4 wk", state: "current" }],
    });
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true, ...data })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.refetchGrid();
    });

    expect(result.current.grid?.plan).toEqual(data.plan);
    expect(result.current.grid?.athlete).toEqual(data.athlete);
    expect(result.current.grid?.phases).toEqual(data.phases);
  });
});

// --- Issue #455 phase A2: drag reordering ---------------------------------
// reorderExercises/reorderDays are STRUCTURAL, same shape as every verb
// above: await the POST, then refetch the whole grid, sharing busyRef.
// useTableReorder (the pure drag-event translator) is the caller; these
// specs only cover the verbs' own POST/refetch contract.

describe("reorderExercises", () => {
  it("POSTs {order} to session/{sessionId}/reorder/, then refetches the grid", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.reorderExercises(11, [201, 202]);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/session/11/reorder/");
    expect(calls[0]![1].method).toBe("POST");
    expect(sentBody()).toEqual({ order: [201, 202] });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
  });

  it("console.errors and does not refetch on POST failure", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("boom")) as unknown as typeof fetch;

    await act(async () => {
      await result.current.reorderExercises(11, [201, 202]);
    });

    expect(console.error).toHaveBeenCalled();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1); // no refetch after a failed POST
  });
});

describe("reorderDays", () => {
  it("POSTs {order} to week/{weekId}/reorder/, then refetches the grid", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ ok: true }))
      .mockResolvedValueOnce(res({ ok: true, ...grid() })) as unknown as typeof fetch;

    await act(async () => {
      await result.current.reorderDays(1, [10, 11]);
    });

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[0]![0]).toBe("/meso/api/plan/7/week/1/reorder/");
    expect(calls[0]![1].method).toBe("POST");
    expect(sentBody()).toEqual({ order: [10, 11] });
    expect(calls[1]![0]).toBe("/meso/api/plan/7/grid/");
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

  it("flushes a pending cell autosave before POSTing fill/, so fill never races a stale value to the server", async () => {
    // Codex P2: fill/ makes the server copy the source cell's ALREADY-STORED
    // DB values to sibling weeks. If a coach edits then immediately fills,
    // fill must wait for the edit's autosave POST to land first.
    const { result } = setup();
    let resolvePatch!: (v: unknown) => void;
    const fetchMock = vi.fn();
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePatch = resolve;
        }),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    // Kick off the cell autosave (fire-and-forget) — its POST is now in flight.
    act(() => {
      result.current.patchCell(100, { text: "4 x 5" });
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Trigger fill while the autosave is still unresolved.
    let fillDone!: Promise<void>;
    act(() => {
      fillDone = result.current.fillAcrossWeeks(100);
    });

    // fill is blocked on flushPendingWrites() — the fill/ POST must NOT have
    // been sent yet, even though fillAcrossWeeks has already been called.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Now let the pending autosave land, queuing up fill's own POST + refetch.
    fetchMock.mockResolvedValueOnce(res({ ok: true, filled: 2 }));
    fetchMock.mockResolvedValueOnce(res({ ok: true, ...grid() }));

    await act(async () => {
      resolvePatch(res({ ok: true }));
      await fillDone;
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0]![0]).toBe("/meso/api/plan/7/prescription/100/"); // the autosave
    expect(fetchMock.mock.calls[1]![0]).toBe("/meso/api/plan/7/prescription/100/fill/"); // fill only after
    expect(fetchMock.mock.calls[2]![0]).toBe("/meso/api/plan/7/grid/"); // then the refetch
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
      second = result.current.fillAcrossWeeks(100);
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

  it("reorderExercises (issue #455 phase A2) also shares the busyRef guard", async () => {
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
      first = result.current.reorderExercises(11, [100]);
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

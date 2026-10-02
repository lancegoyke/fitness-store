// Specs for useGrid (P1 multi-week table) — a self-contained state-owning
// hook for MesoTable. Cell edits (patchCell/renameExercise) are optimistic +
// fire-and-forget, mirroring useAutosave's semantics (CONTRACT.md
// "useAutosave") — no rollback on failure. Structural verbs (add/remove
// day|week|exercise, undo/redo) POST then refetch the whole grid (GET
// grid/), mirroring usePlanData/useReorder's ref-guard idiom so concurrent
// structural ops can't race.
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { MesoTable } from "../components/MesoTable";
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
    exercise_id: "55",
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

describe("renamePlan", () => {
  it("optimistically updates grid.plan, applies the reply, and adopts history", async () => {
    const { result } = setup(
      grid({ plan: { id: 7, title: "Old title", goal: "Strength" } }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({
        ok: true,
        plan: { title: "Server title" },
        history: {
          can_undo: true,
          can_redo: false,
          undo_label: "Renamed program",
          redo_label: null,
        },
      }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.renamePlan("New title");
    });

    expect(result.current.grid?.plan?.title).toBe("New title");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/meso/api/plan/7/title/",
      expect.objectContaining({ method: "POST" }),
    );
    expect(sentBody()).toEqual({ title: "New title" });
    await waitFor(() => expect(result.current.grid?.plan?.title).toBe("Server title"));
    expect(result.current.history.undo_label).toBe("Renamed program");
  });
});

describe("renameMesocycle", () => {
  it("optimistically updates the phase/open block, applies the reply, and adopts history", async () => {
    const { result } = setup(
      grid({
        phases: [
          { id: 1, name: "Block 1", weeks: "4 wk", state: "current" },
          { id: 2, name: "Block 2", weeks: "4 wk", state: "next" },
        ],
      }),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({
        ok: true,
        mesocycle: { id: 1, name: "Server block" },
        history: {
          can_undo: true,
          can_redo: false,
          undo_label: "Renamed block",
          redo_label: null,
        },
      }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.renameMesocycle(1, "Strength block");
    });

    expect(result.current.grid?.phases?.[0]?.name).toBe("Strength block");
    expect(result.current.grid?.phases?.[1]?.name).toBe("Block 2");
    expect(result.current.grid?.mesocycle.name).toBe("Strength block");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/meso/api/plan/7/mesocycle/1/name/",
      expect.objectContaining({ method: "POST" }),
    );
    expect(sentBody()).toEqual({ name: "Strength block" });
    await waitFor(() => expect(result.current.grid?.mesocycle.name).toBe("Server block"));
    expect(result.current.grid?.phases?.[0]?.name).toBe("Server block");
    expect(result.current.history.undo_label).toBe("Renamed block");
  });
});

describe("renameDay", () => {
  it("optimistically updates the day, applies the reply, and adopts history", async () => {
    const { result } = setup(grid({ days: [day({ name: "Old day" })] }));
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({
        ok: true,
        day: { session_slot_id: 1, name: "Server day", day_number: 1 },
        history: {
          can_undo: true,
          can_redo: false,
          undo_label: "Renamed day",
          redo_label: null,
        },
      }),
    ) as unknown as typeof fetch;

    act(() => {
      result.current.renameDay(1, "Power day");
    });

    expect(result.current.grid?.days[0]?.name).toBe("Power day");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/meso/api/plan/7/day/1/name/",
      expect.objectContaining({ method: "POST" }),
    );
    expect(sentBody()).toEqual({ name: "Power day" });
    await waitFor(() => expect(result.current.grid?.days[0]?.name).toBe("Server day"));
    expect(result.current.history.undo_label).toBe("Renamed day");
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

describe("renameExercise link (#608)", () => {
  const rowOf = (r: { current: ReturnType<typeof setup>["result"]["current"] }) => r.current.grid?.days[0]?.rows[0];
  const post = () => JSON.parse(((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]![1] as { body: string }).body);
  beforeEach(() => {
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
  });

  it("a plain typed rename sends {name} only and clears the link when the name changed", () => {
    const { result } = setup();
    act(() => result.current.renameExercise(9, "Front Squat"));
    expect(post()).toEqual({ name: "Front Squat" });
    expect(rowOf(result)?.exercise_id).toBeNull();
  });

  it("a typed rename to the SAME name keeps the link", () => {
    const { result } = setup();
    act(() => result.current.renameExercise(9, "Squat"));
    expect(rowOf(result)?.exercise_id).toBe("55");
  });

  it("a pick sends {name, exercise_id} in one POST and sets the link", () => {
    const { result } = setup();
    act(() => result.current.renameExercise(9, "Back Squat", "uuid-1"));
    expect(post()).toEqual({ name: "Back Squat", exercise_id: "uuid-1" });
    expect(rowOf(result)?.exercise_id).toBe("uuid-1");
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(1);
  });

  it("a pick with an explicit null link sends null and unlinks", () => {
    const { result } = setup();
    act(() => result.current.renameExercise(9, "Box Squat", null));
    expect(post()).toEqual({ name: "Box Squat", exercise_id: null });
    expect(rowOf(result)?.exercise_id).toBeNull();
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
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([
      { line: 1, text: "RPE 8", athlete_authored: false },
    ]);
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

  it("an in-place coach write onto an athlete-entered line paints it as a coach line (the server then refuses visibly)", () => {
    const { result } = setup(
      grid({
        days: [
          day({
            rows: [
              row({
                cells: {
                  "1": cell({
                    lines: [
                      { id: 5, line: 1, text: "100 x 5", athlete_authored: true },
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
      result.current.writeCellLine(9, 1, 1, "105 x 5");
    });

    // A coach write painted onto a line is always a coach line in the view.
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([
      { id: 5, line: 1, text: "105 x 5", athlete_authored: false },
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
      { line: 1, text: "RPE 8", athlete_authored: false },
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
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines).toEqual([
      { line: 1, text: "RPE 8", athlete_authored: false },
    ]);
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

  it("makes no fetch call when the grid has zero live weeks (no viewed week to anchor on)", async () => {
    // Posting anyway would send {week_id: undefined} — JSON drops the key,
    // and the server's own fallback would create the day in whatever block
    // DOES have a live week, i.e. not the block on screen.
    const { result } = setup(grid({ weeks: [] }));
    globalThis.fetch = vi.fn() as unknown as typeof fetch;

    await act(async () => {
      await result.current.addDay();
    });

    expect(globalThis.fetch).not.toHaveBeenCalled();
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
      phases: [{ id: 1, name: "Hypertrophy", weeks: "4 wk", state: "current" }],
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

// --- #709: the coach logs in the grid — server-stack adoption, per-cell
// serialization, refusals, retry marks, and the saveError banner ----------

describe("writeCellLine (#709)", () => {
  const key = "9:1";
  const athlete = { name: "Dana Reyes", initials: "DR", contraindications: [] };

  function linesOf(r: { current: ReturnType<typeof useGrid> }) {
    return r.current.grid?.days[0]?.rows[0]?.cells["1"]?.lines;
  }

  function gridCell(lines: GridCell["lines"], extra: Partial<GridCell> = {}) {
    return { prescription_id: 100, text: "SERVER LINE 0", skipped: false, lines, athlete_summary: null, session_started: true, ...extra };
  }

  function withLines(lines: GridCell["lines"], extra: Partial<MesoGrid> = {}) {
    return grid({
      ...extra,
      days: [day({ rows: [row({ cells: { "1": cell({ lines }) } })] })],
    });
  }

  async function flush() {
    await act(async () => {
      for (let i = 0; i < 20; i++) await Promise.resolve();
    });
  }

  function deferred() {
    let resolve!: (v: unknown) => void;
    const promise = new Promise((r) => {
      resolve = r;
    });
    return { promise, resolve };
  }

  it("sends intent/kind only when given", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 2, "225 x 5", { intent: "edit", kind: "set" });
    });
    expect(sentBody()).toEqual({ week_id: 1, line: 2, text: "225 x 5", intent: "edit", kind: "set" });
  });

  it("intent new on an absent or blank line puts the text there", () => {
    const { result } = setup(withLines([{ line: 1, text: "" }]));
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "cue", { intent: "new" });
    });
    expect(linesOf(result)).toEqual([{ line: 1, text: "cue", athlete_authored: false }]);
  });

  it("intent new on an occupied line is predicted onto the next absent/blank line above it", () => {
    const { result } = setup(
      withLines([
        { line: 1, text: "100 x 5", athlete_authored: true },
        { line: 2, text: "cue two" },
        { line: 3, text: "" },
      ]),
    );
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "my text", { intent: "new" });
    });
    expect(linesOf(result)).toEqual([
      { line: 1, text: "100 x 5", athlete_authored: true },
      { line: 2, text: "cue two" },
      { line: 3, text: "my text", athlete_authored: false },
    ]);
    // The request still names the line the coach typed on; the server relocates.
    expect(sentBody()).toMatchObject({ line: 1, intent: "new" });
  });

  it("serializes writes to one cell and re-applies a queued write over the adopted server stack", async () => {
    const { result } = setup(withLines([{ line: 1, text: "old" }]));
    const first = deferred();
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(res({ ok: true, grid_cell: gridCell([{ line: 1, text: "A" }, { line: 2, text: "B" }]) }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    act(() => {
      result.current.writeCellLine(9, 1, 1, "A", { intent: "edit" });
      result.current.writeCellLine(9, 1, 2, "B", { intent: "new" });
    });
    // Only the first is in flight; the second waits for its answer.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      first.resolve(
        res({
          ok: true,
          grid_cell: gridCell(
            [
              { line: 1, text: "A", athlete_authored: false },
              { line: 3, text: "athlete line", athlete_authored: true },
            ],
            { athlete_summary: { sets: 1, load: "100", unit: "kg", rpe: "" } },
          ),
        }),
      );
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(sentBody(1)).toMatchObject({ line: 2, text: "B", intent: "new" });
    // The second answer is adopted too: server stack wins, line-0 text untouched.
    expect(linesOf(result)).toEqual([
      { line: 1, text: "A" },
      { line: 2, text: "B" },
    ]);
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.text).toBe("3 x 5, RPE 8, 100");
  });

  it("keeps a still-queued write on screen when an earlier answer is adopted", async () => {
    const { result } = setup(withLines([{ line: 1, text: "old" }]));
    const first = deferred();
    globalThis.fetch = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValue(new Promise(() => {})) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "A", { intent: "edit" });
      result.current.writeCellLine(9, 1, 2, "B", { intent: "new" });
    });
    await act(async () => {
      first.resolve(res({ ok: true, grid_cell: gridCell([{ line: 1, text: "A" }], { session_started: true }) }));
    });
    await flush();
    expect(linesOf(result)).toEqual([
      { line: 1, text: "A" },
      { line: 2, text: "B", athlete_authored: false },
    ]);
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.session_started).toBe(true);
  });

  it("a relocated write shows 'Moved below <First>'s line' and clears after ~6s", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const { result } = setup(withLines([{ line: 1, text: "100 x 5", athlete_authored: true }], { athlete }));
      globalThis.fetch = vi.fn().mockResolvedValue(
        res({
          ok: true,
          relocated_from: 1,
          grid_cell: gridCell([
            { line: 1, text: "100 x 5", athlete_authored: true },
            { line: 2, text: "mine" },
          ]),
        }),
      ) as unknown as typeof fetch;
      act(() => {
        result.current.writeCellLine(9, 1, 1, "mine", { intent: "new" });
      });
      await flush();
      expect(result.current.cellUi[key]?.notice).toEqual({ kind: "moved", message: "Moved below Dana's line" });
      await act(async () => {
        vi.advanceTimersByTime(6100);
      });
      expect(result.current.cellUi[key]).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("falls back to 'your athlete' in the moved notice when the grid has no athlete", async () => {
    const { result } = setup(withLines([{ line: 1, text: "x" }]));
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: true, relocated_from: 1, grid_cell: gridCell([{ line: 1, text: "x" }, { line: 2, text: "y" }]) }),
    ) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "y", { intent: "new" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.notice?.message).toBe("Moved below your athlete's line"));
  });

  it("422 athlete_line: adopts grid_cell and records a refusal holding the coach's text", async () => {
    const { result } = setup(withLines([{ line: 1, text: "cue" }], { athlete }));
    globalThis.fetch = vi.fn().mockResolvedValue(
      res(
        {
          ok: false,
          code: "athlete_line",
          error: "Dana logged this line — your text wasn't saved over it.",
          athlete_first_name: "Dana",
          grid_cell: gridCell([{ line: 1, text: "100 x 5", athlete_authored: true }]),
        },
        false,
        422,
      ),
    ) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "my cue", { intent: "edit" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.refusals).toHaveLength(1));
    expect(result.current.cellUi[key]?.refusals).toEqual([
      {
        id: expect.any(Number),
        text: "my cue",
        message: "Dana just logged on this line — your text is below it.",
        canAdd: true,
      },
    ]);
    expect(linesOf(result)).toEqual([{ line: 1, text: "100 x 5", athlete_authored: true }]);
    expect(result.current.saveError).toBe(null);
  });

  const REFUSED = () =>
    res({ ok: false, code: "athlete_line", error: "x", grid_cell: gridCell([{ line: 1, text: "theirs", athlete_authored: true }]) }, false, 422);

  it("refusals are a list: two refused writes in one cell both stay, and only discardRefusal(id) removes one", async () => {
    const { result } = setup(withLines([{ line: 1, text: "cue" }, { line: 2, text: "cue2" }]));
    globalThis.fetch = vi.fn().mockImplementation(async () => REFUSED()) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "first", { intent: "edit" });
      result.current.writeCellLine(9, 1, 2, "second", { intent: "edit" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.refusals).toHaveLength(2));
    const [r1, r2] = result.current.cellUi[key]!.refusals!;
    expect([r1!.text, r2!.text]).toEqual(["first", "second"]);
    act(() => {
      result.current.discardRefusal(9, 1, r1!.id);
    });
    expect(result.current.cellUi[key]?.refusals?.map((r) => r.text)).toEqual(["second"]);
    act(() => {
      result.current.discardRefusal(9, 1, r2!.id);
    });
    expect(result.current.cellUi[key]).toBeUndefined();
  });

  it("an unrelated intent-new write does NOT clear a refusal", async () => {
    const { result } = setup(withLines([{ line: 1, text: "cue" }]));
    globalThis.fetch = vi
      .fn()
      .mockImplementationOnce(async () => REFUSED())
      .mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "mine", { intent: "edit" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.refusals).toHaveLength(1));
    act(() => {
      result.current.writeCellLine(9, 1, 2, "unrelated ghost line", { intent: "new" });
    });
    expect(result.current.cellUi[key]?.refusals?.map((r) => r.text)).toEqual(["mine"]);
  });

  it("an athlete taking the line under a dirty draft: the unmounted draft is refused visibly, text survives (end to end)", async () => {
    const athleteLineGrid = withLines([{ line: 1, text: "100 x 5", athlete_authored: true }], { athlete });
    function Harness() {
      const g = useGrid({ planId: 7, csrf: "tok", initialGrid: withLines([{ id: 1, line: 1, text: "old cue" }], { athlete }) });
      return (
        <>
          <button onClick={() => void g.refetchGrid()}>refetch</button>
          <MesoTable
            grid={g.grid}
            busy={g.busy}
            onPatchCell={g.patchCell}
            onWriteCellLine={g.writeCellLine}
            onPatchRowColumns={g.patchRowColumns}
            onRenameExercise={g.renameExercise}
            onRenameDay={g.renameDay}
            onAddExercise={g.addExercise}
            onRemoveExercise={g.removeExercise}
            onAddDay={g.addDay}
            onRemoveDay={g.removeDay}
            onAddWeek={g.addWeek}
            onRemoveWeek={g.removeWeek}
            onSkipCell={g.skipCell}
            onFillAcrossWeeks={g.fillAcrossWeeks}
            onAddExerciseThisWeek={g.addExerciseThisWeek}
            cellUi={g.cellUi}
            onRetryCellLine={g.retryCellLine}
            onDismissCellNotice={g.dismissCellNotice}
            onDiscardRefusal={g.discardRefusal}
          />
        </>
      );
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(res(athleteLineGrid))
      .mockResolvedValue(
        res(
          {
            ok: false,
            code: "athlete_line",
            error: "x",
            athlete_first_name: "Dana",
            grid_cell: gridCell([{ id: 1, line: 1, text: "100 x 5", athlete_authored: true }]),
          },
          false,
          422,
        ),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<Harness />);
    fireEvent.change(screen.getByTestId("cell-line-100-1"), { target: { value: "my edit" } });
    fireEvent.click(screen.getByText("refetch"));
    await waitFor(() => expect(screen.queryByTestId("cell-line-100-1")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("cell-refusal-text-100-0")).toHaveTextContent("my edit"));
    expect(sentBody(1)).toMatchObject({ line: 1, text: "my edit", intent: "edit" });
  });

  it("a dirty ghost draft is committed as intent new on unmount", () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ ok: true }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    function Harness() {
      const g = useGrid({ planId: 7, csrf: "tok", initialGrid: grid() });
      return (
        <MesoTable
          grid={g.grid} busy={false} onPatchCell={g.patchCell} onWriteCellLine={g.writeCellLine}
          onPatchRowColumns={g.patchRowColumns} onRenameExercise={g.renameExercise} onRenameDay={g.renameDay}
          onAddExercise={g.addExercise} onRemoveExercise={g.removeExercise} onAddDay={g.addDay}
          onRemoveDay={g.removeDay} onAddWeek={g.addWeek} onRemoveWeek={g.removeWeek} onSkipCell={g.skipCell}
          onFillAcrossWeeks={g.fillAcrossWeeks} onAddExerciseThisWeek={g.addExerciseThisWeek}
        />
      );
    }
    const view = render(<Harness />);
    fireEvent.change(screen.getByTestId("cell-line-new-100"), { target: { value: "half" } });
    view.unmount();
    expect(sentBody()).toMatchObject({ line: 1, text: "half", intent: "new" });
  });

  it("Add as a new line (a new write) gets a NEW token; the refused write's retry keeps its OWN", async () => {
    const { result } = setup(withLines([{ line: 1, text: "cue" }]));
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 2, "typed", { intent: "new" });
    });
    const token1 = sentBody(0).token;
    expect(typeof token1).toBe("string");
    expect(token1.length).toBeGreaterThan(0);
    expect(token1.length).toBeLessThanOrEqual(64);
    await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([2]));
    act(() => {
      result.current.retryCellLine(9, 1, 2);
    });
    expect(sentBody(1).token).toBe(token1);
    await flush();
    act(() => {
      result.current.writeCellLine(9, 1, 2, "typed", { intent: "new" });
    });
    expect(sentBody(2).token).not.toBe(token1);
  });

  it("edits carry no token", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ ok: true })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "x", { intent: "edit" });
    });
    expect(sentBody()).not.toHaveProperty("token");
  });

  it("retry mark follows the line the text is shown on after a queued new write is repainted", async () => {
    const { result } = setup(withLines([]));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const first = deferred();
    globalThis.fetch = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockRejectedValue(new TypeError("offline")) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "A", { intent: "new" });
      result.current.writeCellLine(9, 1, 2, "B", { intent: "new" });
    });
    // The server relocated A to line 2 (the athlete took line 1), so B is repainted at 3.
    await act(async () => {
      first.resolve(
        res({
          ok: true,
          relocated_from: 1,
          grid_cell: gridCell([
            { line: 1, text: "100 x 5", athlete_authored: true },
            { line: 2, text: "A" },
          ]),
        }),
      );
    });
    await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([3]));
    expect(linesOf(result)?.find((l) => l.text === "B")?.line).toBe(3);
  });

  it("a structural verb flushes in-flight line writes before its POST", async () => {
    const { result } = setup();
    const first = deferred();
    const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(res(grid()));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "x", { intent: "new" });
    });
    act(() => {
      void result.current.addDay();
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      first.resolve(res({ ok: true }));
    });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(String(fetchMock.mock.calls[1]![0])).toContain("/session/");
  });

  it.each(["not_a_set", "no_athlete", "skipped"])(
    "422 %s: adopts grid_cell and shows the server's error as a dismissable cell notice",
    async (code) => {
      const { result } = setup(withLines([{ line: 1, text: "cue" }]));
      globalThis.fetch = vi.fn().mockResolvedValue(
        res({ ok: false, code, error: "Because reasons.", grid_cell: gridCell([{ line: 1, text: "cue", loggable: false }]) }, false, 422),
      ) as unknown as typeof fetch;
      act(() => {
        result.current.writeCellLine(9, 1, 1, "cue", { intent: "edit", kind: "set" });
      });
      await waitFor(() => expect(result.current.cellUi[key]?.notice).toEqual({ kind: "error", message: "Because reasons. (“cue”)" }));
      expect(linesOf(result)).toEqual([{ line: 1, text: "cue", loggable: false }]);
      act(() => {
        result.current.dismissCellNotice(9, 1);
      });
      expect(result.current.cellUi[key]).toBeUndefined();
    },
  );

  it("422 no_free_line keeps the text as a refusal draft with the server's message", async () => {
    const { result } = setup(withLines([{ line: 1, text: "cue" }]));
    globalThis.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, code: "no_free_line", error: "No free line left.", grid_cell: gridCell([{ line: 1, text: "cue" }]) }, false, 422),
    ) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "extra", { intent: "new" });
    });
    await waitFor(() =>
      expect(result.current.cellUi[key]?.refusals).toEqual([
        { id: expect.any(Number), text: "extra", message: "No free line left.", canAdd: false },
      ]),
    );
    expect(result.current.cellUi[key]?.notice).toBeUndefined();
  });

  it("a network failure keeps the text on screen, marks the line unsaved, and retry re-sends then clears the mark", async () => {
    const { result } = setup(withLines([{ line: 1, text: "old" }]));
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue(res({ ok: true, grid_cell: gridCell([{ line: 1, text: "new text" }]) })) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "new text", { intent: "edit" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([1]));
    expect(linesOf(result)).toEqual([{ line: 1, text: "new text" }]);
    expect(result.current.saveError).not.toBe(null);

    act(() => {
      result.current.retryCellLine(9, 1, 1);
    });
    expect(result.current.cellUi[key]).toBeUndefined();
    expect(sentBody(1)).toEqual({ week_id: 1, line: 1, text: "new text", intent: "edit" });
    await flush();
    expect(result.current.cellUi[key]).toBeUndefined();
    expect(linesOf(result)).toEqual([{ line: 1, text: "new text" }]);
  });

  it.each([
    ["5xx", res({ ok: false }, false, 503)],
    ["an unreadable 200", { ok: true, status: 200, json: async () => { throw new SyntaxError("bad"); } }],
  ])("%s marks the line unsaved", async (_label, response) => {
    const { result } = setup(withLines([{ line: 1, text: "old" }]));
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(response) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "typed", { intent: "edit" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([1]));
    expect(linesOf(result)).toEqual([{ line: 1, text: "typed" }]);
  });

  it("an unsaved line survives a later successful write's adoption", async () => {
    const { result } = setup(withLines([{ line: 1, text: "a" }, { line: 2, text: "b" }]));
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue(
        res({ ok: true, grid_cell: gridCell([{ line: 1, text: "a" }, { line: 2, text: "b2" }]) }),
      ) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "a-unsaved", { intent: "edit" });
    });
    await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([1]));
    act(() => {
      result.current.writeCellLine(9, 1, 2, "b2", { intent: "edit" });
    });
    await flush();
    expect(linesOf(result)).toEqual([
      { line: 1, text: "a-unsaved" },
      { line: 2, text: "b2" },
    ]);
    expect(result.current.cellUi[key]?.unsaved).toEqual([1]);
  });

  it("a structural refetch landing mid-write keeps the optimistic line on screen until its response arrives", async () => {
    const { result } = setup(withLines([{ line: 1, text: "old" }]));
    const inflight = deferred();
    const fetched = withLines([{ line: 1, text: "old" }]);
    globalThis.fetch = vi
      .fn()
      .mockReturnValueOnce(inflight.promise)
      .mockResolvedValue(res(fetched)) as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 2, "typed", { intent: "new" });
    });
    await act(async () => {
      await result.current.refetchGrid();
    });
    expect(linesOf(result)).toEqual([
      { line: 1, text: "old" },
      { line: 2, text: "typed", athlete_authored: false },
    ]);
    await act(async () => {
      inflight.resolve(res({ ok: true, grid_cell: gridCell([{ line: 1, text: "old" }, { line: 2, text: "typed" }]) }));
    });
    await flush();
    expect(linesOf(result)).toEqual([
      { line: 1, text: "old" },
      { line: 2, text: "typed" },
    ]);
  });

  describe("an unsaved write is never painted over a line someone else now holds", () => {
    const athleteTook = () =>
      withLines([{ line: 1, text: "100 x 5", athlete_authored: true }], { athlete });

    it("new write: ghost write fails, the athlete takes line 1, a refetch -> text on line 2 with a retry mark, line 1 stays the athlete's", async () => {
      const { result } = setup(withLines([]));
      vi.spyOn(console, "error").mockImplementation(() => {});
      globalThis.fetch = vi
        .fn()
        .mockRejectedValueOnce(new TypeError("offline"))
        .mockResolvedValue(res(athleteTook())) as unknown as typeof fetch;
      act(() => {
        result.current.writeCellLine(9, 1, 1, "my cue", { intent: "new" });
      });
      await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([1]));
      await act(async () => {
        await result.current.refetchGrid();
      });
      expect(linesOf(result)).toEqual([
        { line: 1, text: "100 x 5", athlete_authored: true },
        { line: 2, text: "my cue", athlete_authored: false },
      ]);
      expect(result.current.cellUi[key]?.unsaved).toEqual([2]);
      expect(result.current.cellUi[key]?.refusals).toBeUndefined();
    });

    it("edit write: the text moves into the refusal list (Add/Discard) and leaves the unsaved set", async () => {
      const { result } = setup(withLines([{ line: 1, text: "old cue" }], { athlete }));
      vi.spyOn(console, "error").mockImplementation(() => {});
      globalThis.fetch = vi
        .fn()
        .mockRejectedValueOnce(new TypeError("offline"))
        .mockResolvedValue(res(athleteTook())) as unknown as typeof fetch;
      act(() => {
        result.current.writeCellLine(9, 1, 1, "brace harder", { intent: "edit" });
      });
      await waitFor(() => expect(result.current.cellUi[key]?.unsaved).toEqual([1]));
      await act(async () => {
        await result.current.refetchGrid();
      });
      expect(linesOf(result)).toEqual([{ line: 1, text: "100 x 5", athlete_authored: true }]);
      expect(result.current.cellUi[key]?.unsaved ?? []).toEqual([]);
      expect(result.current.cellUi[key]?.refusals).toEqual([
        {
          id: expect.any(Number),
          text: "brace harder",
          message: "Dana logged on this line — your unsaved text is below it.",
          canAdd: true,
        },
      ]);
    });

    it("no refusal row is added for blank text", async () => {
      const { result } = setup(withLines([{ line: 1, text: "old" }]));
      globalThis.fetch = vi.fn().mockResolvedValue(
        res({ ok: false, code: "athlete_line", error: "x", grid_cell: gridCell([{ line: 1, text: "theirs", athlete_authored: true }]) }, false, 422),
      ) as unknown as typeof fetch;
      act(() => {
        result.current.writeCellLine(9, 1, 1, "", { intent: "edit" });
      });
      await flush();
      expect(result.current.cellUi[key]?.refusals).toBeUndefined();
    });
  });

  it("flushes in-flight line writes before fillAcrossWeeks", async () => {
    const { result } = setup();
    const first = deferred();
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(res(grid()));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    act(() => {
      result.current.writeCellLine(9, 1, 1, "x", { intent: "new" });
    });
    act(() => {
      void result.current.fillAcrossWeeks(100);
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1); // the fill waits for the line write
    await act(async () => {
      first.resolve(res({ ok: true }));
    });
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/fill/"))).toBe(true));
  });
});

describe("saveError banner state (#709)", () => {
  it("a failed patchCell sets saveError (and still console.errors); dismissSaveError clears it", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 500)) as unknown as typeof fetch;
    expect(result.current.saveError).toBe(null);
    act(() => {
      result.current.patchCell(100, { text: "4 x 6" });
    });
    await waitFor(() => expect(result.current.saveError).toMatch(/Couldn't save your last change/));
    expect(console.error).toHaveBeenCalled();
    expect(result.current.grid?.days[0]?.rows[0]?.cells["1"]?.text).toBe("4 x 6");
    act(() => {
      result.current.dismissSaveError();
    });
    expect(result.current.saveError).toBe(null);
  });

  it("a failed structural verb sets saveError", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 500)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.addDay();
    });
    expect(result.current.saveError).not.toBe(null);
  });

  it("a failed refetch sets saveError", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 500)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.refetchGrid();
    });
    expect(result.current.saveError).not.toBe(null);
  });
});

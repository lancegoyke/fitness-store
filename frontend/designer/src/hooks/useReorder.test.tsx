// Specs for useReorder (Phase 4, docs/meso/designer-framework-plan.md +
// scratchpad phase4-spec.md "Frontend") — the dnd-kit designer's reorder
// hook. Does NOT exist yet; this file is RED until a later agent implements
// ../hooks/useReorder. dnd-kit's own pointer/keyboard drag mechanics are
// browser-verified, not jsdom-simulated (spec's explicit call-out) — these
// specs pin the SEAMS instead: given a dnd-kit-SHAPED `DragEndEvent` (built
// by hand below, the same way a real `onDragEnd={reorder.onDragEnd}` wired
// into `<DndContext>` would receive one), useReorder must produce the right
// optimistic array update, the right POST body, envelope adoption via
// applyPlanData, failure re-fetch, and one shared in-flight guard.
//
// === API decisions this file pins (spec left these open) ===
// 1. Hook signature: `useReorder({ planId, csrf, viewedWeekId, program,
//    setProgram, applyPlanData })` -> `{ reordering, onDragEnd }`.
//    `setProgram` is a NEW primitive `usePlanData` must export (a raw
//    `Dispatch<SetStateAction<Day[]>>`) — investigated first: `patchExercise`
//    only merges a partial patch onto ONE exercise matched by id, and
//    `updateExerciseField` only writes one field at a fixed (dayIndex,
//    exIndex); neither can reorder an array, splice an item out of one day's
//    array and into another's, or reorder the days array itself. A raw
//    setState escape hatch is the smallest primitive that covers all three
//    drop shapes, so useReorder owns its own optimistic-update logic instead
//    of usePlanData growing three bespoke reorder verbs. This file does NOT
//    modify usePlanData.ts (RED-phase scope: failing specs only) — the green
//    agent adds the one-line `setProgram` export to its return object.
// 2. Drag event shape (`ReorderDragEndEvent`, modeled on dnd-kit's actual
//    `DragEndEvent`): `{ active: { id, data: { current } }, over: { id,
//    data: { current } } | null }`. `data.current` is a discriminated union
//    `ReorderDragData = { type: "exercise"; dayId; prescriptionId } |
//    { type: "day"; sessionId }` — carried by each sortable item via
//    dnd-kit's `useSortable({ id, data })`, so onDragEnd never needs to
//    re-derive a row's parent day by scanning `program`. ONE `onDragEnd`
//    handles both the per-day exercise SortableContexts and the day-strip
//    SortableContext (routed by `data.current.type`) — simpler for WeekGrid
//    to wire (one DndContext, one callback) than coordinating two handlers.
// 3. No-op rule: `!over || active.id === over.id` -> return before ANY
//    optimistic state change or fetch (spec: "same position / null
//    over-target"). A same-type match (exercise-over-exercise, day-over-day)
//    is handled directly. A CROSS-type match is RESOLVED rather than
//    discarded — see decisions 8 and 9 below, added after live-browser
//    testing showed dnd-kit's closestCenter routinely resolves `over` to
//    the wrong item TYPE (a day drag's `over` lands on an exercise row far
//    more often than on another day card, since rows' centers sit closer to
//    the pointer) — treating that as a no-op made day reordering
//    unreachable in practice.
// 4. Cross-day `index` semantics: the index POSTed to prescription-move is
//    the target day's CURRENT (pre-insertion, moved-row-excluded)
//    `exercises.findIndex(overPrescriptionId)` — this matches the backend's
//    own insertion semantics exactly (`prescription_move` builds
//    `target_rows` from the target session's live rows BEFORE inserting the
//    moved row, then clamps+inserts at the posted index).
// 5. Failure handling: console.error, then GET the viewed week
//    (`/meso/api/plan/<id>/week/<viewedWeekId>/`, mirroring usePlanData's
//    `switchWeek` — no request body/options) and `applyPlanData` the reply.
//    If that re-fetch ALSO throws, it is swallowed with its own
//    console.error (no unhandled rejection) — a double-failure leaves the
//    optimistic state as the last-known UI, an accepted edge case this file
//    does not test.
// 6. In-flight guard: a single `reordering` ref+state shared across all
//    three POST paths (mirrors useDeletes' `deletingRef`), checked
//    SYNCHRONOUSLY at the top of `onDragEnd` — a second drop while one is
//    pending is a full no-op (no optimistic update either, not just "no
//    fetch"), since committing a second optimistic change before the first
//    POST's authoritative reply lands would double-desync the array.
// 7. `onDragEnd` returns `void | Promise<void>` — undefined for every
//    synchronous no-op path, a Promise for every path that awaits a fetch —
//    so a caller (or a test) that always `await`s the return value works
//    either way.
// 8. Day-over-exercise resolution: when `active.type === "day"` and
//    `over.type === "exercise"`, resolve `over` to that exercise's OWNING
//    day (`over.data.current.dayId`) and run the same day-reorder as a
//    direct day-over-day drop — no-op (no optimistic update, no fetch) if
//    the resolved day is the dragged day itself.
// 9. Exercise-over-day resolution (append): when `active.type ===
//    "exercise"` and `over.type === "day"`, the drop has no specific row to
//    land on — an EMPTY day, or a drop past a day's last row where the day
//    card's own droppable (not a row's) is `closestCenter`'s pick — treated
//    as "append to the end of that day." Cross-day: POST prescription-move
//    with `index` = the target day's CURRENT (pre-insertion)
//    `exercises.length` (same insertion semantics as decision 4, just at
//    the tail). Same-day: a session-reorder with the dragged row moved to
//    the end of its own array; no-op if it's already last.
// 10. Stale-week guard: a reorder POSTed while viewing week A can resolve
//    AFTER the coach has switched to week B — applying week A's envelope
//    (or re-fetching week A on failure) would yank the view back. Mirrors
//    usePlanData's `viewedWeekIdRef` idiom exactly: a ref holds the CURRENT
//    `viewedWeekId`, reassigned unconditionally every render (not just on
//    change). `postReorder` snapshots the ref's value synchronously at drop
//    time; on resolve (success OR failure-path re-fetch), it re-reads the
//    ref and proceeds ONLY if unchanged — otherwise skips entirely (no
//    `applyPlanData`, and for the failure path, no re-fetch GET at all).
//    The in-flight `reordering` guard still clears either way (decision 6
//    is unaffected — a stale reply still releases the lock for the next
//    drop).
import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import { useReorder } from "./useReorder";
import type { ReorderDragEndEvent } from "./useReorder";
import type { Day, PlanEnvelope } from "../lib/api";
import type { Id } from "./usePlanData";

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function fetchCall(n = 0) {
  const mockFetch = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  return mockFetch.mock.calls[n] as [string, RequestInit | undefined];
}

function sentBody(n = 0) {
  const [, opts] = fetchCall(n);
  const body = opts?.body;
  return body == null ? null : JSON.parse(body as string);
}

function planEnvelope(overrides: Partial<PlanEnvelope> = {}): PlanEnvelope {
  return {
    ok: true,
    program: [],
    weeks: [],
    phases: [],
    viewing: 55,
    history: { can_undo: true, can_redo: false, undo_label: "Reordered exercises", redo_label: null },
    ...overrides,
  };
}

// Day 101: exercises 1 (Squat), 2 (Bench). Day 102: exercise 3 (Deadlift).
function twoDayProgram(): Day[] {
  return [
    {
      id: 101,
      n: 1,
      name: "Day 1",
      exercises: [
        { id: 1, name: "Squat", sets: "3", reps: "5", load: "100" },
        { id: 2, name: "Bench", sets: "3", reps: "5", load: "80" },
      ],
    },
    {
      id: 102,
      n: 2,
      name: "Day 2",
      exercises: [{ id: 3, name: "Deadlift", sets: "3", reps: "5", load: "120" }],
    },
  ];
}

/** Builds a dnd-kit-shaped exercise drag event (see decision 2 above). */
function exerciseDragEvent(
  activeId: Id,
  activeDayId: Id,
  overId: Id | null,
  overDayId: Id = activeDayId,
): ReorderDragEndEvent {
  return {
    active: { id: activeId, data: { current: { type: "exercise", dayId: activeDayId, prescriptionId: activeId } } },
    over:
      overId == null
        ? null
        : { id: overId, data: { current: { type: "exercise", dayId: overDayId, prescriptionId: overId } } },
  };
}

/** Builds a dnd-kit-shaped day-strip drag event (see decision 2 above). */
function dayDragEvent(activeId: Id, overId: Id | null): ReorderDragEndEvent {
  return {
    active: { id: activeId, data: { current: { type: "day", sessionId: activeId } } },
    over: overId == null ? null : { id: overId, data: { current: { type: "day", sessionId: overId } } },
  };
}

/** Builds a dnd-kit-shaped event: a DAY active, resolved `over` an EXERCISE
 * row — dnd-kit's closestCenter routinely does this in practice (decision
 * 8 above). `overDayId` is the row's OWNING day, which may differ from the
 * dragged day. */
function dayOverExerciseDragEvent(
  activeSessionId: Id,
  overPrescriptionId: Id,
  overDayId: Id,
): ReorderDragEndEvent {
  return {
    active: { id: activeSessionId, data: { current: { type: "day", sessionId: activeSessionId } } },
    over: {
      id: overPrescriptionId,
      data: { current: { type: "exercise", dayId: overDayId, prescriptionId: overPrescriptionId } },
    },
  };
}

/** Builds a dnd-kit-shaped event: an EXERCISE active, dropped `over` a DAY's
 * own droppable — its background, not a specific row (an empty day, or past
 * the last row; decision 9 above). */
function exerciseOverDayDragEvent(
  activePrescriptionId: Id,
  activeDayId: Id,
  overSessionId: Id,
): ReorderDragEndEvent {
  return {
    active: {
      id: activePrescriptionId,
      data: { current: { type: "exercise", dayId: activeDayId, prescriptionId: activePrescriptionId } },
    },
    over: { id: `day-${overSessionId}`, data: { current: { type: "day", sessionId: overSessionId } } },
  };
}

// Day 101: exercises 1 (Squat), 2 (Bench). Day 102: exercise 3 (Deadlift).
// Day 103: no exercises — an empty day has no per-row droppable to hit.
function threeDayProgramWithEmptyDay(): Day[] {
  return [...twoDayProgram(), { id: 103, n: 3, name: "Day 3", exercises: [] }];
}

function setup(initialProgram: Day[], initialViewedWeekId: Id | null = 55) {
  const applyPlanData = vi.fn();
  const hook = renderHook(
    (props: { viewedWeekId: Id | null }) => {
      const [program, setProgram] = useState<Day[]>(initialProgram);
      const reorder = useReorder({
        planId: 7,
        csrf: "tok",
        viewedWeekId: props.viewedWeekId,
        program,
        setProgram,
        applyPlanData,
      });
      return { ...reorder, program };
    },
    { initialProps: { viewedWeekId: initialViewedWeekId } },
  );
  return { ...hook, applyPlanData };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("within-day reorder", () => {
  it("optimistically reorders the day's exercises before the POST resolves, then adopts the envelope reply", async () => {
    const { result, applyPlanData } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let pending!: void | Promise<void>;
    act(() => {
      pending = result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });
    // Optimistic reorder is visible immediately, before the network resolves.
    expect(result.current.program[0]!.exercises.map((e) => e.id)).toEqual([2, 1]);
    expect(result.current.program[1]!.exercises.map((e) => e.id)).toEqual([3]);
    expect(result.current.reordering).toBe(true);

    const reply = planEnvelope();
    await act(async () => {
      resolveFetch(res(reply));
      await pending;
    });
    expect(applyPlanData).toHaveBeenCalledWith(reply);
    expect(result.current.reordering).toBe(false);
  });

  it("POSTs the session-reorder endpoint with the day's full id list in the new order", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/session/101/reorder/");
    expect(opts!.method).toBe("POST");
    expect((opts!.headers as Record<string, string>)["X-CSRFToken"]).toBe("tok");
    expect(sentBody()).toEqual({ order: [2, 1] });
  });
});

describe("cross-day move", () => {
  it("optimistically removes from the source day and inserts into the target day at the over row's index, before the POST resolves", () => {
    const { result } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let pending!: void | Promise<void>;
    act(() => {
      // Drag exercise 1 (day 101) onto exercise 3, which sits at index 0 of
      // day 102 — the moved row lands at index 0, pushing exercise 3 to 1.
      pending = result.current.onDragEnd(exerciseDragEvent(1, 101, 3, 102));
    });
    expect(result.current.program[0]!.exercises.map((e) => e.id)).toEqual([2]);
    expect(result.current.program[1]!.exercises.map((e) => e.id)).toEqual([1, 3]);

    act(() => {
      resolveFetch(res(planEnvelope()));
    });
    return pending;
  });

  it("POSTs prescription-move with {session_id, index} using the target day's pre-insertion index", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, 3, 102));
    });
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/prescription/1/move/");
    expect(opts!.method).toBe("POST");
    expect(sentBody()).toEqual({ session_id: 102, index: 0 });
  });

  it("dropping onto the second row of the target day lands the moved row at index 1", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    // Move exercise 3 (day 102, alone) onto exercise 2 (day 101, index 1).
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(3, 102, 2, 101));
    });
    expect(sentBody()).toEqual({ session_id: 101, index: 1 });
  });
});

describe("day reorder", () => {
  it("optimistically reorders the day strip before the POST resolves, then adopts the envelope reply", async () => {
    const { result, applyPlanData } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let pending!: void | Promise<void>;
    act(() => {
      pending = result.current.onDragEnd(dayDragEvent(101, 102));
    });
    expect(result.current.program.map((d) => d.id)).toEqual([102, 101]);

    const reply = planEnvelope();
    await act(async () => {
      resolveFetch(res(reply));
      await pending;
    });
    expect(applyPlanData).toHaveBeenCalledWith(reply);
  });

  it("POSTs the week-reorder endpoint with the week's full session id list in the new order", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(dayDragEvent(101, 102));
    });
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/week/55/reorder/");
    expect(opts!.method).toBe("POST");
    expect(sentBody()).toEqual({ order: [102, 101] });
  });
});

describe("day drag resolved over an exercise row (decision 8)", () => {
  it("reorders days relative to the exercise row's owning day, POSTing the same week-reorder endpoint as a direct day-over-day drop", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      // Day 101 dragged; dnd-kit's closestCenter resolves `over` to exercise
      // 3, which lives in day 102 — same outcome as dragging day 101 onto
      // day 102 directly.
      await result.current.onDragEnd(dayOverExerciseDragEvent(101, 3, 102));
    });
    expect(result.current.program.map((d) => d.id)).toEqual([102, 101]);
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/week/55/reorder/");
    expect(opts!.method).toBe("POST");
    expect(sentBody()).toEqual({ order: [102, 101] });
  });
});

describe("exercise drag resolved over a day, append-to-end (decision 9)", () => {
  it("optimistically appends across days before the POST resolves, then adopts the envelope reply", async () => {
    const { result, applyPlanData } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let pending!: void | Promise<void>;
    act(() => {
      // Exercise 1 (day 101) dropped on day 102's own droppable (not a
      // specific row) — appends after day 102's existing exercise 3.
      pending = result.current.onDragEnd(exerciseOverDayDragEvent(1, 101, 102));
    });
    expect(result.current.program[0]!.exercises.map((e) => e.id)).toEqual([2]);
    expect(result.current.program[1]!.exercises.map((e) => e.id)).toEqual([3, 1]);

    const reply = planEnvelope();
    await act(async () => {
      resolveFetch(res(reply));
      await pending;
    });
    expect(applyPlanData).toHaveBeenCalledWith(reply);
  });

  it("POSTs prescription-move with index = the target day's pre-insertion exercises.length", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseOverDayDragEvent(1, 101, 102));
    });
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/prescription/1/move/");
    expect(opts!.method).toBe("POST");
    expect(sentBody()).toEqual({ session_id: 102, index: 1 });
  });

  it("appends into an EMPTY target day at index 0", async () => {
    const { result } = setup(threeDayProgramWithEmptyDay());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      // Day 103 has no exercises — no per-row droppable exists to hit.
      await result.current.onDragEnd(exerciseOverDayDragEvent(3, 102, 103));
    });
    expect(result.current.program[1]!.exercises).toEqual([]);
    expect(result.current.program[2]!.exercises.map((e) => e.id)).toEqual([3]);
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/prescription/3/move/");
    expect(opts!.method).toBe("POST");
    expect(sentBody()).toEqual({ session_id: 103, index: 0 });
  });

  it("moves a same-day exercise to the end via session-reorder when dropped on its own day", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      // Exercise 1 (index 0 of day 101) dropped on day 101's own droppable.
      await result.current.onDragEnd(exerciseOverDayDragEvent(1, 101, 101));
    });
    expect(result.current.program[0]!.exercises.map((e) => e.id)).toEqual([2, 1]);
    const [url, opts] = fetchCall(0);
    expect(url).toBe("/meso/api/plan/7/session/101/reorder/");
    expect(opts!.method).toBe("POST");
    expect(sentBody()).toEqual({ order: [2, 1] });
  });
});

describe("stale-week guard (decision 10)", () => {
  it("skips applyPlanData entirely when the coach switched weeks while a reorder POST was in flight", async () => {
    const { result, rerender, applyPlanData } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let pending!: void | Promise<void>;
    act(() => {
      pending = result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });
    // Coach switches to a different week while the POST is still pending.
    act(() => {
      rerender({ viewedWeekId: 999 });
    });
    await act(async () => {
      resolveFetch(res(planEnvelope()));
      await pending;
    });
    expect(applyPlanData).not.toHaveBeenCalled();
    // The in-flight guard still releases, independent of the stale check.
    expect(result.current.reordering).toBe(false);
  });

  it("skips the failure-path re-fetch entirely (no GET, no applyPlanData) when the coach switched weeks before the POST rejects", async () => {
    const { result, rerender, applyPlanData } = setup(twoDayProgram());
    vi.spyOn(console, "error").mockImplementation(() => {});
    // Queued (not hand-controlled) so an unguarded re-fetch resolves instead
    // of hanging the test — the behavior under test is the CALL COUNT below,
    // not resolution timing.
    const wouldBeRefetch = planEnvelope({ program: [{ id: 101, n: 1, name: "Day 1", exercises: [] }] });
    globalThis.fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue(res(wouldBeRefetch)) as unknown as typeof fetch;

    let pending!: void | Promise<void>;
    act(() => {
      pending = result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });
    act(() => {
      rerender({ viewedWeekId: 999 });
    });
    await act(async () => {
      await pending;
    });
    // Only the original (now-failed) POST — no re-fetch GET for the
    // no-longer-viewed week.
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(applyPlanData).not.toHaveBeenCalled();
    expect(console.error).toHaveBeenCalledTimes(1);
    expect(result.current.reordering).toBe(false);
  });

  it("still applies the reply when the viewed week is unchanged at resolve time (control case)", async () => {
    const { result, rerender, applyPlanData } = setup(twoDayProgram());
    // Rerender with the SAME viewedWeekId — not a week switch.
    act(() => {
      rerender({ viewedWeekId: 55 });
    });
    const reply = planEnvelope();
    globalThis.fetch = vi.fn().mockResolvedValue(res(reply)) as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });
    expect(applyPlanData).toHaveBeenCalledWith(reply);
  });
});

describe("no-op drop", () => {
  it("does nothing when an exercise is dropped on itself (same position)", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, 1, 101));
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.program[0]!.exercises.map((e) => e.id)).toEqual([1, 2]);
  });

  it("does nothing when over is null (dropped outside any droppable)", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, null));
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("does nothing when a day is dropped on itself", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(dayDragEvent(101, 101));
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.program.map((d) => d.id)).toEqual([101, 102]);
  });

  it("does nothing when a day is dropped over an exercise row belonging to that same day (resolution decision 8, same-day case)", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      // Exercise 1 belongs to day 101 — the same day being dragged.
      await result.current.onDragEnd(dayOverExerciseDragEvent(101, 1, 101));
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.program.map((d) => d.id)).toEqual([101, 102]);
  });

  it("does nothing when an exercise dropped on its own day is already last (resolution decision 9, same-day case)", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    await act(async () => {
      // Exercise 2 is already the last row of day 101.
      await result.current.onDragEnd(exerciseOverDayDragEvent(2, 101, 101));
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.program[0]!.exercises.map((e) => e.id)).toEqual([1, 2]);
  });
});

describe("failure handling", () => {
  it("on POST failure: console.errors and re-fetches the viewed week, applying the reply", async () => {
    const { result, applyPlanData } = setup(twoDayProgram());
    vi.spyOn(console, "error").mockImplementation(() => {});
    const refreshed = planEnvelope({ program: [{ id: 101, n: 1, name: "Day 1", exercises: [] }] });
    globalThis.fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(res(refreshed)) as unknown as typeof fetch;

    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });

    expect(console.error).toHaveBeenCalled();
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    const [refetchUrl, refetchOpts] = fetchCall(1);
    expect(refetchUrl).toBe("/meso/api/plan/7/week/55/");
    expect(refetchOpts).toBeUndefined();
    expect(applyPlanData).toHaveBeenCalledWith(refreshed);
    expect(result.current.reordering).toBe(false);
  });

  it("swallows a failed re-fetch with its own console.error (no unhandled rejection)", async () => {
    const { result, applyPlanData } = setup(twoDayProgram());
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch")) as unknown as typeof fetch;

    await act(async () => {
      await result.current.onDragEnd(dayDragEvent(101, 102));
    });

    expect(console.error).toHaveBeenCalledTimes(2);
    expect(applyPlanData).not.toHaveBeenCalled();
    expect(result.current.reordering).toBe(false);
  });
});

describe("shared in-flight guard", () => {
  it("a second drop while one POST is pending fires no extra fetch", async () => {
    const { result } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let first!: void | Promise<void>;
    let second!: void | Promise<void>;
    act(() => {
      first = result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
      second = result.current.onDragEnd(dayDragEvent(101, 102));
    });
    await act(async () => {
      resolveFetch(res(planEnvelope()));
      await Promise.all([first, second]);
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it("the guard is a full no-op: the second drop's optimistic update never applies", async () => {
    const { result } = setup(twoDayProgram());
    let resolveFetch!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((r) => {
          resolveFetch = r;
        }),
    ) as unknown as typeof fetch;

    let first!: void | Promise<void>;
    let second!: void | Promise<void>;
    act(() => {
      first = result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
      // Day drag while the exercise reorder is in flight: ignored outright.
      second = result.current.onDragEnd(dayDragEvent(101, 102));
    });
    expect(result.current.program.map((d) => d.id)).toEqual([101, 102]);
    await act(async () => {
      resolveFetch(res(planEnvelope()));
      await Promise.all([first, second]);
    });
  });

  it("a retry after the in-flight POST resolves fires a fresh fetch", async () => {
    const { result } = setup(twoDayProgram());
    globalThis.fetch = vi.fn().mockResolvedValue(res(planEnvelope())) as unknown as typeof fetch;
    await act(async () => {
      await result.current.onDragEnd(exerciseDragEvent(1, 101, 2, 101));
    });
    expect(result.current.reordering).toBe(false);
    await act(async () => {
      await result.current.onDragEnd(dayDragEvent(101, 102));
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });
});

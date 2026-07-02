// Tests for the meso designer's remove controls (app/store_project/static/js/meso.js),
// Phase 0a of the designer framework plan (issue #401).
//
// Three delete verbs share one shape: an exercise row (`removeExercise`, no
// confirm — a stray row is cheap to re-add), and a training day / a whole week
// (armed via `requestRemoveDay`/`requestRemoveWeek` into `pendingDelete`, then
// executed by `confirmPendingDelete` or dropped by `cancelPendingDelete` — no
// native `confirm()`). Every delete action shares one in-flight guard
// (`deleting`) so a double-click — or arming a second delete while the first is
// still in flight — can't fire two requests.

import { createMeso } from "../app/store_project/static/js/meso.js";

// A meso component wired enough to run the delete verbs without a live Alpine
// runtime (mirrors frontend/meso.test.js's makeMeso()).
function makeMeso(overrides = {}) {
  const c = createMeso();
  c.$nextTick = (fn) => fn && fn();
  c.$refs = {};
  c.sleep = () => Promise.resolve();
  c.csrf = "tok";
  c.live = true;
  c.planId = 7;
  return Object.assign(c, overrides);
}

function res({ ok = true, status = 200, body = {} } = {}) {
  return { ok, status, json: async () => body };
}

// A re-serialized plan payload in the shape applyPlanData consumes.
function planData(overrides = {}) {
  return Object.assign(
    {
      ok: true,
      program: [],
      weeks: [],
      phases: [],
      viewing: null,
    },
    overrides,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("delete state defaults", () => {
  it("createMeso starts with no pending delete and the in-flight guard clear", () => {
    const c = createMeso();
    expect(c.deleting).toBe(false);
    expect(c.pendingDelete).toBe(null);
  });
});

describe("removeExercise", () => {
  function twoExerciseDay() {
    return [
      {
        id: 1,
        n: 1,
        name: "Day 1",
        exercises: [
          { id: 9, name: "Squat" },
          { id: 10, name: "Bench" },
        ],
      },
    ];
  }

  it("live: posts to the prescription-delete URL and applies the reply via applyPlanData", async () => {
    const c = makeMeso();
    c.program = twoExerciseDay();
    const data = planData({
      program: [{ id: 1, n: 1, name: "Day 1", exercises: [{ id: 10, name: "Bench" }] }],
      weeks: [{ id: 2, index: 1, label: "Wk 1", current: true }],
      viewing: 2,
    });
    global.fetch = vi.fn().mockResolvedValue(res({ body: data }));

    await c.removeExercise(0, 0);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/prescription/9/delete/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(opts.body).toBe(null); // no body — a bare POST
    expect(c.program).toEqual(data.program);
    expect(c.weeks).toEqual(data.weeks);
    expect(c.viewedWeekId).toBe(2);
    expect(c.deleting).toBe(false); // guard cleared afterward
  });

  it("non-live: splices the row locally without a network call", async () => {
    const c = makeMeso({ live: false });
    c.program = twoExerciseDay();
    global.fetch = vi.fn();

    await c.removeExercise(0, 0);

    expect(global.fetch).not.toHaveBeenCalled();
    expect(c.program[0].exercises).toHaveLength(1);
    expect(c.program[0].exercises[0].id).toBe(10);
  });

  it("in-flight guard: a second removeExercise call while one is pending fires no extra fetch", async () => {
    const c = makeMeso();
    c.program = twoExerciseDay();
    let resolveFetch;
    global.fetch = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );

    const first = c.removeExercise(0, 0);
    const second = c.removeExercise(0, 0);
    resolveFetch(res({ body: planData() }));
    await Promise.all([first, second]);

    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("error path: console.error, program unchanged, guard cleared so a retry works", async () => {
    const c = makeMeso();
    c.program = twoExerciseDay();
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    await c.removeExercise(0, 0);

    expect(c.program[0].exercises).toHaveLength(2); // unchanged
    expect(c.deleting).toBe(false);

    global.fetch = vi.fn().mockResolvedValue(res({ body: planData() }));
    await c.removeExercise(0, 0);
    expect(global.fetch).toHaveBeenCalledTimes(1); // the guard let the retry through
  });
});

describe("pending delete confirm flow (day / week removal)", () => {
  function twoDayProgram() {
    return [
      { id: 1, n: 1, name: "Day 1", exercises: [] },
      { id: 5, n: 2, name: "Day 2", exercises: [] },
    ];
  }

  it("requestRemoveDay arms pendingDelete with the day index", () => {
    const c = makeMeso();
    c.requestRemoveDay(1);
    expect(c.pendingDelete).toEqual({ type: "day", di: 1 });
  });

  it("requestRemoveWeek arms pendingDelete with the week id, replacing an armed day", () => {
    const c = makeMeso();
    c.requestRemoveDay(0);
    c.requestRemoveWeek(4);
    expect(c.pendingDelete).toEqual({ type: "week", weekId: 4 });
  });

  it("requestRemoveDay replaces an armed week", () => {
    const c = makeMeso();
    c.requestRemoveWeek(4);
    c.requestRemoveDay(0);
    expect(c.pendingDelete).toEqual({ type: "day", di: 0 });
  });

  it("cancelPendingDelete disarms without firing a request", () => {
    const c = makeMeso();
    c.requestRemoveDay(0);
    global.fetch = vi.fn();

    c.cancelPendingDelete();

    expect(c.pendingDelete).toBe(null);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("confirmPendingDelete (day, live): posts to the session-delete URL, applies the reply, disarms", async () => {
    const c = makeMeso();
    c.program = twoDayProgram();
    const data = planData({
      program: [{ id: 1, n: 1, name: "Day 1", exercises: [] }],
      viewing: 3,
    });
    global.fetch = vi.fn().mockResolvedValue(res({ body: data }));

    c.requestRemoveDay(1); // program[1].id === 5
    await c.confirmPendingDelete();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/session/5/delete/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(c.program).toEqual(data.program);
    expect(c.pendingDelete).toBe(null);
  });

  it("confirmPendingDelete (week, live): posts to the week-delete URL, applies the reply, disarms", async () => {
    const c = makeMeso();
    c.weeks = [
      { id: 1, index: 1, label: "Wk 1", current: true },
      { id: 2, index: 2, label: "Wk 2", current: false },
    ];
    const data = planData({
      weeks: [{ id: 1, index: 1, label: "Wk 1", current: true }],
      viewing: 1,
    });
    global.fetch = vi.fn().mockResolvedValue(res({ body: data }));

    c.requestRemoveWeek(2);
    await c.confirmPendingDelete();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/week/2/delete/");
    expect(opts.method).toBe("POST");
    expect(c.weeks).toEqual(data.weeks);
    expect(c.pendingDelete).toBe(null);
  });

  it("confirmPendingDelete (day, non-live): splices the day locally without a network call", async () => {
    const c = makeMeso({ live: false });
    c.program = twoDayProgram();
    global.fetch = vi.fn();

    c.requestRemoveDay(1);
    await c.confirmPendingDelete();

    expect(global.fetch).not.toHaveBeenCalled();
    expect(c.program).toHaveLength(1);
    expect(c.program[0].id).toBe(1);
    expect(c.pendingDelete).toBe(null);
  });

  it("confirmPendingDelete (week, non-live): removes the week locally without a network call", async () => {
    const c = makeMeso({ live: false });
    c.weeks = [
      { id: "w1", index: 1, label: "Wk 1", current: true },
      { id: "w2", index: 2, label: "Wk 2", current: false },
    ];
    global.fetch = vi.fn();

    c.requestRemoveWeek("w2");
    await c.confirmPendingDelete();

    expect(global.fetch).not.toHaveBeenCalled();
    expect(c.weeks.map((w) => w.id)).toEqual(["w1"]);
    expect(c.pendingDelete).toBe(null);
  });

  it("confirmPendingDelete is a no-op with nothing armed", async () => {
    const c = makeMeso();
    global.fetch = vi.fn();
    await c.confirmPendingDelete();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("in-flight guard: a second confirmPendingDelete while one is pending fires no extra fetch", async () => {
    const c = makeMeso();
    c.program = twoDayProgram();
    let resolveFetch;
    global.fetch = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );

    c.requestRemoveDay(1);
    const first = c.confirmPendingDelete();
    const second = c.confirmPendingDelete();
    resolveFetch(res({ body: planData() }));
    await Promise.all([first, second]);

    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("in-flight guard is shared: confirmPendingDelete no-ops while a removeExercise is still in flight", async () => {
    const c = makeMeso();
    c.program = [{ id: 1, n: 1, name: "Day 1", exercises: [{ id: 9, name: "Squat" }] }];
    let resolveFetch;
    global.fetch = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );

    const first = c.removeExercise(0, 0);
    c.requestRemoveWeek(3);
    const second = c.confirmPendingDelete();
    resolveFetch(res({ body: planData() }));
    await Promise.all([first, second]);

    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("error path: console.error, grid unchanged, guard and pendingDelete cleared for a later retry", async () => {
    const c = makeMeso();
    c.program = twoDayProgram();
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    c.requestRemoveDay(1);
    await c.confirmPendingDelete();

    expect(c.program).toHaveLength(2); // unchanged
    expect(c.deleting).toBe(false);
    expect(c.pendingDelete).toBe(null);

    global.fetch = vi.fn().mockResolvedValue(res({ body: planData() }));
    c.requestRemoveDay(1);
    await c.confirmPendingDelete();
    expect(global.fetch).toHaveBeenCalledTimes(1); // the guard let the retry through
  });
});

describe("pendingDelete disarms on grid swap", () => {
  // Arming Day 2, switching weeks, then pressing the (stale) Confirm must not
  // delete whatever now renders at that index — any applyPlanData (week
  // switch, add, delete, undo) invalidates the armed row, so it disarms.
  it("applyPlanData clears an armed delete", () => {
    const c = makeMeso();
    c.requestRemoveDay(1);
    c.applyPlanData(planData());
    expect(c.pendingDelete).toBe(null);
  });

  it("a confirm after the swap is a no-op", async () => {
    const c = makeMeso();
    c.requestRemoveDay(1);
    c.applyPlanData(planData());
    global.fetch = vi.fn();
    await c.confirmPendingDelete();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

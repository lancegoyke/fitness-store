// Tests for the meso designer's undo/redo controls (app/store_project/static/js/meso.js),
// Phase 1 of the designer framework plan (docs/meso/designer-framework-plan.md).
//
// Undo is a *backend* feature: the endpoints restore a server-side snapshot and
// reply with the same re-serialized plan payload every week endpoint returns, so
// the component's whole job is two guarded verbs (`undo`/`redo` — POST, then
// `applyPlanData`), a `history` availability object that rides every payload,
// and one keyboard entry point (`handleUndoKey`) the template binds to
// Ctrl/Cmd+Z and Shift+Ctrl/Cmd+Z. The two verbs share one in-flight guard
// (`undoing`) so a key-repeat can't stack requests.

import { createMeso } from "../app/store_project/static/js/meso.js";

// A meso component wired enough to run the undo verbs without a live Alpine
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

const HISTORY_NONE = {
  can_undo: false,
  can_redo: false,
  undo_label: null,
  redo_label: null,
};

const HISTORY_BOTH = {
  can_undo: true,
  can_redo: true,
  undo_label: "Edited Box Squat",
  redo_label: "Deleted Day 2",
};

// A re-serialized plan payload in the shape the undo/redo endpoints return.
function planData(overrides = {}) {
  return Object.assign(
    {
      ok: true,
      program: [{ id: 10, n: 1, name: "Lower", exercises: [] }],
      weeks: [{ id: 2, index: 1, label: "Wk 1", current: true }],
      phases: [],
      viewing: 2,
      history: HISTORY_BOTH,
    },
    overrides,
  );
}

// A keydown-ish event for handleUndoKey. Real Ctrl+Z arrives with key "z";
// real Shift+Ctrl+Z arrives with key "Z" — the shifted cases below use the
// uppercase form on purpose.
function keyEvent(overrides = {}) {
  return Object.assign(
    {
      key: "z",
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
      target: { tagName: "DIV", isContentEditable: false },
      preventDefault: () => {},
    },
    overrides,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("undo state defaults", () => {
  it("createMeso starts with an empty history and the in-flight guard clear", () => {
    const c = createMeso();
    expect(c.undoing).toBe(false);
    expect(c.history).toEqual(HISTORY_NONE);
  });

  it("applyPlanData adopts the payload's history object", () => {
    const c = makeMeso();
    c.applyPlanData(planData());
    expect(c.history).toEqual(HISTORY_BOTH);
  });

  it("applyPlanData falls back to an empty history when the payload has none", () => {
    const c = makeMeso();
    c.history = HISTORY_BOTH;
    const data = planData();
    delete data.history;
    c.applyPlanData(data);
    expect(c.history).toEqual(HISTORY_NONE);
  });
});

describe("undo", () => {
  it("POSTs the viewed week to the undo endpoint and applies the reply", async () => {
    const c = makeMeso({ history: { ...HISTORY_BOTH }, viewedWeekId: 5 });
    const data = planData({ viewing: 5 });
    global.fetch = vi.fn().mockResolvedValue(res({ body: data }));

    await c.undo();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/undo/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(JSON.parse(opts.body)).toEqual({ week_id: 5 });
    expect(c.program).toEqual(data.program);
    expect(c.history).toEqual(data.history);
    expect(c.undoing).toBe(false);
  });

  it("does not fetch when history says there is nothing to undo", async () => {
    const c = makeMeso({ history: { ...HISTORY_NONE } });
    global.fetch = vi.fn();
    await c.undo();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("does not fetch while another undo/redo is in flight", async () => {
    const c = makeMeso({ history: { ...HISTORY_BOTH }, undoing: true });
    global.fetch = vi.fn();
    await c.undo();
    await c.redo();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("is a no-op when the grid is not live", async () => {
    const c = makeMeso({ live: false, history: { ...HISTORY_BOTH } });
    global.fetch = vi.fn();
    await c.undo();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("leaves state unchanged and clears the guard when the fetch fails", async () => {
    const c = makeMeso({ history: { ...HISTORY_BOTH }, viewedWeekId: 5 });
    c.program = [{ id: 99 }];
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockRejectedValue(new Error("boom"));

    await c.undo();

    expect(c.program).toEqual([{ id: 99 }]);
    expect(c.history).toEqual(HISTORY_BOTH);
    expect(c.undoing).toBe(false);
    expect(console.error).toHaveBeenCalled();

    // The guard really is clear: a later undo goes through.
    global.fetch = vi.fn().mockResolvedValue(res({ body: planData() }));
    await c.undo();
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("redo", () => {
  it("POSTs the viewed week to the redo endpoint and applies the reply", async () => {
    const c = makeMeso({ history: { ...HISTORY_BOTH }, viewedWeekId: 3 });
    const data = planData({ history: { ...HISTORY_BOTH, can_redo: false } });
    global.fetch = vi.fn().mockResolvedValue(res({ body: data }));

    await c.redo();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/redo/");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ week_id: 3 });
    expect(c.history.can_redo).toBe(false);
  });

  it("does not fetch when history says there is nothing to redo", async () => {
    const c = makeMeso({ history: { ...HISTORY_BOTH, can_redo: false } });
    global.fetch = vi.fn();
    await c.redo();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("handleUndoKey", () => {
  function armed() {
    const c = makeMeso({ history: { ...HISTORY_BOTH }, viewedWeekId: 2 });
    c.undo = vi.fn();
    c.redo = vi.fn();
    return c;
  }

  it("Ctrl+Z undoes", () => {
    const c = armed();
    c.handleUndoKey(keyEvent({ ctrlKey: true }));
    expect(c.undo).toHaveBeenCalledTimes(1);
    expect(c.redo).not.toHaveBeenCalled();
  });

  it("Cmd+Z undoes", () => {
    const c = armed();
    c.handleUndoKey(keyEvent({ metaKey: true }));
    expect(c.undo).toHaveBeenCalledTimes(1);
    expect(c.redo).not.toHaveBeenCalled();
  });

  it("Shift+Ctrl+Z redoes", () => {
    const c = armed();
    c.handleUndoKey(keyEvent({ key: "Z", ctrlKey: true, shiftKey: true }));
    expect(c.redo).toHaveBeenCalledTimes(1);
    expect(c.undo).not.toHaveBeenCalled();
  });

  it("Shift+Cmd+Z redoes", () => {
    const c = armed();
    c.handleUndoKey(keyEvent({ key: "Z", metaKey: true, shiftKey: true }));
    expect(c.redo).toHaveBeenCalledTimes(1);
    expect(c.undo).not.toHaveBeenCalled();
  });

  it("a plain z does nothing", () => {
    const c = armed();
    c.handleUndoKey(keyEvent());
    expect(c.undo).not.toHaveBeenCalled();
    expect(c.redo).not.toHaveBeenCalled();
  });

  it("other modified keys do nothing", () => {
    const c = armed();
    c.handleUndoKey(keyEvent({ key: "s", ctrlKey: true }));
    expect(c.undo).not.toHaveBeenCalled();
    expect(c.redo).not.toHaveBeenCalled();
  });

  it("ignores keystrokes from form fields, where native undo must win", () => {
    const c = armed();
    for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
      c.handleUndoKey(keyEvent({ ctrlKey: true, target: { tagName, isContentEditable: false } }));
    }
    c.handleUndoKey(
      keyEvent({ ctrlKey: true, target: { tagName: "DIV", isContentEditable: true } }),
    );
    expect(c.undo).not.toHaveBeenCalled();
    expect(c.redo).not.toHaveBeenCalled();
  });
});

describe("history refresh on partial (row-level) responses", () => {
  // persistRow / addExercise / addDay / submitOverride record an undo action
  // server-side but reply with row payloads, not the full plan envelope — so
  // they carry a `history` key the client must adopt, or the Undo button
  // stays stale until the next full re-serialize.
  it("persistRow adopts the reply's history", async () => {
    const c = makeMeso();
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, prescription: { id: 9 }, history: HISTORY_BOTH } }),
    );
    await c.persistRow({ id: 9, name: "Squat" });
    expect(c.history).toEqual(HISTORY_BOTH);
  });

  it("addExercise adopts the reply's history", async () => {
    const c = makeMeso();
    c.program = [{ id: 1, exercises: [] }];
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, prescription: { id: 12 }, history: HISTORY_BOTH } }),
    );
    await c.addExercise(0);
    expect(c.program[0].exercises).toEqual([{ id: 12 }]);
    expect(c.history).toEqual(HISTORY_BOTH);
  });

  it("addDay adopts the reply's history", async () => {
    const c = makeMeso();
    c.program = [];
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, session: { id: 5, exercises: [] }, history: HISTORY_BOTH } }),
    );
    await c.addDay();
    expect(c.history).toEqual(HISTORY_BOTH);
  });

  it("submitOverride adopts the reply's history", async () => {
    const c = makeMeso();
    const ex = { id: 9 };
    c.override = { ex, saving: false, error: "" };
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, adj: null, adjusts: [], history: HISTORY_BOTH } }),
    );
    await c.submitOverride(ex, { athlete: "a", clear: true });
    expect(c.history).toEqual(HISTORY_BOTH);
  });

  it("a reply without history leaves the current history untouched", async () => {
    const c = makeMeso({ history: { ...HISTORY_BOTH } });
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, prescription: { id: 9 } } }),
    );
    await c.persistRow({ id: 9 });
    expect(c.history).toEqual(HISTORY_BOTH);
  });
});

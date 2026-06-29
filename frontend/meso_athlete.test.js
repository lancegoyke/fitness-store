// Tests for the athlete session logger (app/store_project/static/js/meso_athlete.js).
//
// Focus: the logic that is fragile and effectively impossible to verify by hand
// — the offline write queue (stash on network failure, dedupe per session,
// replay on reconnect) and the save/flush state machine. The pure helpers
// (rowFilled / buildPayload / syncFromLog) are covered too since the queue
// payloads are built from them.

import {
  createLogger,
  epleyOneRm,
  roundToStep,
  loadForPercent,
} from "../app/store_project/static/js/meso_athlete.js";

const LOG_URL = "/meso/api/me/session/42/log/";
const ONE_RM_URL = "/meso/api/me/session/42/one-rm/";

// A minimal logger with two exercises (one prescription each, two sets each).
function makeLogger(overrides = {}) {
  const c = createLogger();
  c.logUrl = LOG_URL;
  c.csrf = "tok";
  c.status = "pending";
  c.exercises = [
    {
      id: 1,
      set_rows: [
        { set_number: 1, reps: "", load: "", rpe: "", done: false },
        { set_number: 2, reps: "", load: "", rpe: "", done: false },
      ],
    },
    {
      id: 2,
      set_rows: [{ set_number: 1, reps: "", load: "", rpe: "", done: false }],
    },
  ];
  return Object.assign(c, overrides);
}

// Build a fetch Response stub. `body` is returned from .json().
function res({ ok = true, status = 200, redirected = false, body = {} } = {}) {
  return { ok, status, redirected, json: async () => body };
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("rowFilled", () => {
  const c = createLogger();
  it("is true when the row is checked", () => {
    expect(c.rowFilled({ done: true })).toBe(true);
  });
  it("is true when any entry is present", () => {
    expect(c.rowFilled({ done: false, reps: "5" })).toBe(true);
    expect(c.rowFilled({ done: false, load: "100" })).toBe(true);
    expect(c.rowFilled({ done: false, rpe: "8" })).toBe(true);
  });
  it("is false for an empty, unchecked row", () => {
    expect(c.rowFilled({ done: false, reps: "", load: "", rpe: "" })).toBe(
      false,
    );
  });
});

describe("buildPayload", () => {
  it("collects only filled rows in the endpoint's shape", () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    c.exercises[0].set_rows[1].reps = "5";
    // exercise 2's only row stays empty → excluded.
    const payload = c.buildPayload(false);
    expect(payload.sets).toEqual([
      { prescription: 1, set_number: 1, reps: "", load: "", rpe: "" },
      { prescription: 1, set_number: 2, reps: "5", load: "", rpe: "" },
    ]);
  });

  it("stamps status 'done' only when markDone is set", () => {
    const c = makeLogger({ status: "pending" });
    expect(c.buildPayload(true).status).toBe("done");
    // Save-progress on an already-logged session must not downgrade it.
    const logged = makeLogger({ status: "done" });
    expect(logged.buildPayload(false).status).toBe("done");
  });
});

describe("syncFromLog", () => {
  it("reconciles row check state to exactly what the server persisted", () => {
    const c = makeLogger();
    c.exercises[0].set_rows[1].done = true; // will be cleared (not in log)
    c.syncFromLog({
      sets: [
        { prescription: 1, set_number: 1 },
        { prescription: 2, set_number: 1 },
      ],
    });
    expect(c.exercises[0].set_rows[0].done).toBe(true);
    expect(c.exercises[0].set_rows[1].done).toBe(false);
    expect(c.exercises[1].set_rows[0].done).toBe(true);
  });
});

describe("offline queue", () => {
  it("round-trips through localStorage", () => {
    const c = makeLogger();
    c.writeQueue([{ url: "/a", body: { x: 1 } }]);
    expect(c.readQueue()).toEqual([{ url: "/a", body: { x: 1 } }]);
  });

  it("returns [] when storage holds corrupt JSON", () => {
    const c = makeLogger();
    localStorage.setItem(c.queueKey, "{not json");
    expect(c.readQueue()).toEqual([]);
  });

  it("keeps at most one queued save per session (latest wins)", () => {
    const c = makeLogger();
    c.enqueue({ status: "pending", sets: [1] });
    c.enqueue({ status: "done", sets: [1, 2] });
    const q = c.readQueue();
    expect(q).toHaveLength(1);
    expect(q[0].url).toBe(LOG_URL);
    expect(q[0].body.status).toBe("done");
  });

  it("does not clobber another session's queued save", () => {
    const c = makeLogger();
    c.writeQueue([{ url: "/meso/api/me/session/99/log/", body: { sets: [] } }]);
    c.enqueue({ status: "pending", sets: [] });
    expect(c.readQueue()).toHaveLength(2);
  });
});

describe("save", () => {
  it("queues the write (not an error) when the network is unreachable", async () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.save(true);
    expect(c.queued).toBe(true);
    expect(c.error).toBe(false);
    expect(c.saving).toBe(false);
    expect(c.readQueue()).toHaveLength(1);
  });

  it("queues the write when the request is redirected to login", async () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    global.fetch = vi.fn().mockResolvedValue(res({ redirected: true }));
    await c.save(false);
    expect(c.queued).toBe(true);
    expect(c.error).toBe(false);
    expect(c.readQueue()).toHaveLength(1);
  });

  it("surfaces an HTTP error the athlete should retry", async () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    await c.save(false);
    expect(c.error).toBe(true);
    expect(c.queued).toBe(false);
    expect(c.readQueue()).toHaveLength(0);
  });

  it("reflects the server's log on success", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          log: { status: "done", sets: [{ prescription: 1, set_number: 1 }] },
        },
      }),
    );
    await c.save(true);
    expect(c.status).toBe("done");
    expect(c.saved).toBe(true);
    expect(c.error).toBe(false);
    // syncFromLog applied the server's truth.
    expect(c.exercises[0].set_rows[0].done).toBe(true);
  });

  it("is a no-op while a save is already in flight", async () => {
    const c = makeLogger({ saving: true });
    global.fetch = vi.fn();
    await c.save(true);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("flushQueue", () => {
  it("replays a queued save and clears it on success", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    c.enqueue({ status: "done", sets: [{ prescription: 1, set_number: 1 }] });
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          log: { status: "done", sets: [{ prescription: 1, set_number: 1 }] },
        },
      }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.queued).toBe(false);
    expect(c.status).toBe("done");
  });

  it("keeps the item queued when still offline", async () => {
    const c = makeLogger();
    c.enqueue({ status: "pending", sets: [] });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
  });

  it("keeps the item queued when bounced to login (redirect)", async () => {
    const c = makeLogger();
    c.enqueue({ status: "pending", sets: [] });
    global.fetch = vi.fn().mockResolvedValue(res({ redirected: true }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
  });

  it("does nothing when the queue is empty", async () => {
    const c = makeLogger();
    global.fetch = vi.fn();
    await c.flushQueue();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

// ---- %1RM logging ergonomics (S2 Phase 2b) ----
// A %1RM target is an intensity, not a weight. These helpers turn the coach's
// "75%" into a bar load (given the athlete's estimated 1RM) and back — the
// estimate is entered in the logger and persisted client-side.

describe("epleyOneRm", () => {
  it("returns the load itself for a single rep", () => {
    expect(epleyOneRm("100", "1")).toBe(100);
  });
  it("estimates 1RM from reps via Epley", () => {
    // 100 × (1 + 5/30) = 116.666…
    expect(epleyOneRm("100", "5")).toBeCloseTo(116.667, 2);
  });
  it("is null for a non-numeric load or reps (BW, AMRAP, ranges)", () => {
    expect(epleyOneRm("BW", "5")).toBeNull();
    expect(epleyOneRm("100", "AMRAP")).toBeNull();
    expect(epleyOneRm("100", "8-10")).toBeNull();
    expect(epleyOneRm("", "")).toBeNull();
  });
  it("is null for non-positive load or sub-1 reps", () => {
    expect(epleyOneRm("0", "5")).toBeNull();
    expect(epleyOneRm("100", "0")).toBeNull();
  });
});

describe("roundToStep", () => {
  it("rounds to the nearest plate step", () => {
    expect(roundToStep(91.2, 2.5)).toBe(90);
    expect(roundToStep(81.3, 2.5)).toBe(82.5);
  });
});

describe("loadForPercent", () => {
  it("scales an estimated 1RM by a percent, rounded to a loadable plate", () => {
    expect(loadForPercent("120", "75")).toBe(90); // 0.75 × 120 = 90
    expect(loadForPercent("100", "82")).toBe(82.5); // 82 → nearest 2.5
  });
  it("is null without a usable 1RM or percent", () => {
    expect(loadForPercent("", "75")).toBeNull();
    expect(loadForPercent("120", "")).toBeNull();
    expect(loadForPercent("0", "75")).toBeNull();
  });
});

describe("isPercentLift / suggestedLoad / setImpliedOneRm", () => {
  function pctLogger() {
    const c = createLogger();
    c.unit = "kg";
    c.exercises = [
      { id: 1, load: "75", load_type: "pct", e1rm: "120", set_rows: [] },
      { id: 2, load: "70", load_type: "abs", e1rm: "", set_rows: [] },
    ];
    return c;
  }

  it("identifies a %1RM lift", () => {
    const c = pctLogger();
    expect(c.isPercentLift(c.exercises[0])).toBe(true);
    expect(c.isPercentLift(c.exercises[1])).toBe(false);
  });

  it("suggests a bar load (with unit) for a %1RM lift with a known 1RM", () => {
    const c = pctLogger();
    expect(c.suggestedLoad(c.exercises[0])).toBe("90 kg");
  });

  it("suggests nothing for an absolute lift or a missing 1RM", () => {
    const c = pctLogger();
    expect(c.suggestedLoad(c.exercises[1])).toBe("");
    c.exercises[0].e1rm = "";
    expect(c.suggestedLoad(c.exercises[0])).toBe("");
  });

  it("shows the implied 1RM from a logged set", () => {
    const c = pctLogger();
    expect(c.setImpliedOneRm({ load: "100", reps: "1" })).toBe("100 kg");
    expect(c.setImpliedOneRm({ load: "", reps: "" })).toBe("");
  });
});

describe("server-derived 1RM (effectiveOneRm / usingDerivedOneRm)", () => {
  function logger(ex) {
    const c = createLogger();
    c.unit = "kg";
    c.exercises = [ex];
    return c;
  }

  it("uses the server's derived 1RM when no value is typed", () => {
    const c = logger({ load: "75", load_type: "pct", one_rm: "120", e1rm: "" });
    expect(c.effectiveOneRm(c.exercises[0])).toBe("120");
    expect(c.suggestedLoad(c.exercises[0])).toBe("90 kg"); // 75% of 120
  });

  it("lets a typed estimate override the derived value", () => {
    const c = logger({
      load: "75",
      load_type: "pct",
      one_rm: "120",
      e1rm: "200",
    });
    expect(c.effectiveOneRm(c.exercises[0])).toBe("200");
    expect(c.suggestedLoad(c.exercises[0])).toBe("150 kg"); // 75% of 200
  });

  it("falls back to derived when the typed value is non-numeric", () => {
    const c = logger({
      load: "75",
      load_type: "pct",
      one_rm: "120",
      e1rm: "abc",
    });
    expect(c.effectiveOneRm(c.exercises[0])).toBe("120");
  });

  it("flags when the suggestion is sized off the derived 1RM", () => {
    const c = logger({ load: "75", load_type: "pct", one_rm: "120", e1rm: "" });
    expect(c.usingDerivedOneRm(c.exercises[0])).toBe(true);
    c.exercises[0].e1rm = "200";
    expect(c.usingDerivedOneRm(c.exercises[0])).toBe(false);
    c.exercises[0].e1rm = "";
    c.exercises[0].one_rm = "";
    expect(c.usingDerivedOneRm(c.exercises[0])).toBe(false);
  });

  it("hydrates a log-derived 1RM as the placeholder, input blank", () => {
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        one_rm_url: ONE_RM_URL,
        status: "pending",
        unit: "kg",
        exercises: [
          {
            id: 7,
            load: "75",
            load_type: "pct",
            one_rm: "142.5",
            one_rm_source: "logged",
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    expect(c.exercises[0].one_rm).toBe("142.5"); // the suggested-load default
    expect(c.exercises[0].e1rm).toBe(""); // input empty, derived value is a placeholder
    expect(c.effectiveOneRm(c.exercises[0])).toBe("142.5");
  });

  it("hydrates a manual 1RM into the editable input", () => {
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        one_rm_url: ONE_RM_URL,
        status: "pending",
        unit: "kg",
        exercises: [
          {
            id: 7,
            load: "75",
            load_type: "pct",
            one_rm: "150",
            one_rm_source: "manual",
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    expect(c.exercises[0].e1rm).toBe("150"); // the athlete's own number
    expect(c.exercises[0].one_rm).toBe(""); // no separate derived value to show
    expect(c.effectiveOneRm(c.exercises[0])).toBe("150");
  });
});

describe("manual 1RM persistence (server-side, Phase 2)", () => {
  function logger(ex) {
    const c = createLogger();
    c.unit = "kg";
    c.csrf = "tok";
    c.oneRmUrl = ONE_RM_URL;
    c.exercises = [ex];
    return c;
  }

  it("POSTs the typed value to the one-rm endpoint", async () => {
    const c = logger({ id: 7, e1rm: "140", one_rm: "" });
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "140", source: "manual" } }));
    await c._postOneRm(c.exercises[0]);
    expect(global.fetch).toHaveBeenCalledWith(
      ONE_RM_URL,
      expect.objectContaining({ method: "POST" }),
    );
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body).toEqual({ prescription: 7, value: "140" });
  });

  it("keeps a saved manual value in the input", async () => {
    const c = logger({ id: 7, e1rm: "140", one_rm: "120" });
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "140", source: "manual" } }));
    await c._postOneRm(c.exercises[0]);
    expect(c.exercises[0].e1rm).toBe("140");
    expect(c.exercises[0].one_rm).toBe(""); // no separate derived value while manual
  });

  it("reverts to the log-derived estimate when cleared", async () => {
    const c = logger({ id: 7, e1rm: "", one_rm: "" });
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "120", source: "logged" } }));
    await c._postOneRm(c.exercises[0]);
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body.value).toBe(""); // a blank value clears it
    expect(c.exercises[0].e1rm).toBe("");
    expect(c.exercises[0].one_rm).toBe("120"); // the server's re-derived value
  });

  it("does not POST a half-typed non-numeric value", async () => {
    const c = logger({ id: 7, e1rm: "ab", one_rm: "" });
    global.fetch = vi.fn();
    await c._postOneRm(c.exercises[0]);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("keeps the typed value in-session when the network is unreachable", async () => {
    const c = logger({ id: 7, e1rm: "140", one_rm: "" });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c._postOneRm(c.exercises[0]);
    expect(c.exercises[0].e1rm).toBe("140"); // not wiped — retries on next edit
  });

  it("does not reconcile on an HTTP error", async () => {
    const c = logger({ id: 7, e1rm: "140", one_rm: "120" });
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 400 }));
    await c._postOneRm(c.exercises[0]);
    expect(c.exercises[0].e1rm).toBe("140");
    expect(c.exercises[0].one_rm).toBe("120");
  });

  it("debounces rapid edits into a single POST", async () => {
    vi.useFakeTimers();
    const c = logger({ id: 7, e1rm: "1", one_rm: "" });
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "140", source: "manual" } }));
    c.saveOneRm(c.exercises[0]);
    c.exercises[0].e1rm = "14";
    c.saveOneRm(c.exercises[0]);
    c.exercises[0].e1rm = "140";
    c.saveOneRm(c.exercises[0]);
    expect(global.fetch).not.toHaveBeenCalled(); // still within the debounce window
    await vi.runAllTimersAsync();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body.value).toBe("140"); // the latest edit wins
  });

  it("drops a stale response that a newer edit superseded", async () => {
    const c = logger({ id: 7, e1rm: "", one_rm: "120" });
    // Response A (a lagging clear) then B (the newer manual value).
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ body: { one_rm: "120", source: "logged" } }))
      .mockResolvedValueOnce(
        res({ body: { one_rm: "140", source: "manual" } }),
      );
    const pA = c._postOneRm(c.exercises[0]); // gen 1: a clear
    c.exercises[0].e1rm = "140"; // athlete types again before A lands
    const pB = c._postOneRm(c.exercises[0]); // gen 2: supersedes A
    await Promise.all([pA, pB]);
    // A's stale clear must not wipe the value B set.
    expect(c.exercises[0].e1rm).toBe("140");
    expect(c.exercises[0].one_rm).toBe(""); // B's manual reconcile applied
  });
});

describe("pre-Phase-2 override migration", () => {
  it("promotes a legacy meso-e1rm value to the server, then drops the store", () => {
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        one_rm_url: ONE_RM_URL,
        status: "pending",
        unit: "kg",
        exercises: [
          {
            id: 7,
            load: "75",
            load_type: "pct",
            one_rm: "",
            one_rm_source: "",
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    localStorage.setItem("meso-e1rm", JSON.stringify({ 7: "150" }));
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "150", source: "manual" } }));
    const c = createLogger();
    c.init();
    // Seeded into the editable input...
    expect(c.exercises[0].e1rm).toBe("150");
    // ...and posted to the server (fire-and-forget within init)...
    expect(global.fetch).toHaveBeenCalledWith(
      ONE_RM_URL,
      expect.objectContaining({ method: "POST" }),
    );
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body).toEqual({ prescription: 7, value: "150" });
    // ...with the legacy store dropped so it can't resurrect over a later clear.
    expect(localStorage.getItem("meso-e1rm")).toBe(null);
  });

  it("does not override an existing server-side manual value", () => {
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        one_rm_url: ONE_RM_URL,
        status: "pending",
        unit: "kg",
        exercises: [
          {
            id: 7,
            load: "75",
            load_type: "pct",
            one_rm: "200",
            one_rm_source: "manual",
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    localStorage.setItem("meso-e1rm", JSON.stringify({ 7: "150" }));
    global.fetch = vi.fn();
    const c = createLogger();
    c.init();
    expect(c.exercises[0].e1rm).toBe("200"); // server value kept, legacy ignored
    expect(global.fetch).not.toHaveBeenCalled();
    expect(localStorage.getItem("meso-e1rm")).toBe(null); // still cleared
  });
});

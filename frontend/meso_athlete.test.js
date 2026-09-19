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

// Issue #451: logging the coach's own session can auto-advance the guided tour
// server-side (`advance_self_step_if_complete("results")` in
// `athlete_log_session`), but the log POST is a fetch (no reload), so the
// mounted meso_tour.js driver can't see it. The "results" step advances on a
// `done` log, so the nudge keys off the log status the *server returned*, not
// the button pressed: a completed save fires the `meso:tour-refresh` document
// event; a re-save of an already-done session (which persists `done` even via
// "Save progress") fires too; a pending save, an offline queue, or an outright
// failure stays silent (no spurious re-render / SR re-announcement).
describe("save → tour refresh nudge (#451)", () => {
  it("dispatches meso:tour-refresh after a completed log (save(true))", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          log: { status: "done", sets: [{ prescription: 1, set_number: 1 }] },
        },
      }),
    );
    await c.save(true);
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not dispatch for a pending 'save progress' (save(false))", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          log: {
            status: "pending",
            sets: [{ prescription: 1, set_number: 1 }],
          },
        },
      }),
    );
    await c.save(false);
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).not.toHaveBeenCalled();
  });

  it("dispatches when a 'save progress' re-saves an already-done session", async () => {
    // Codex #451: on an already-completed session, `buildPayload(false)`
    // preserves `status: "done"`, so the server still persists a done log and
    // can advance the "results" step — keying off `data.log.status` (not the
    // button) keeps the card from going stale.
    vi.useFakeTimers();
    const c = makeLogger({ status: "done" });
    c.exercises[0].set_rows[0].done = true;
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          log: { status: "done", sets: [{ prescription: 1, set_number: 1 }] },
        },
      }),
    );
    await c.save(false);
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not dispatch when the save fails (HTTP error)", async () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    await c.save(true);
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).not.toHaveBeenCalled();
  });

  it("does not dispatch when the save is queued offline", async () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.save(true);
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).not.toHaveBeenCalled();
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
      { id: 1, text: "3 x 5, 75%", e1rm: "120", set_rows: [] },
      { id: 2, text: "3 x 10, 70", e1rm: "", set_rows: [] },
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
    const c = logger({ text: "3 x 5, 75%", one_rm: "120", e1rm: "" });
    expect(c.effectiveOneRm(c.exercises[0])).toBe("120");
    expect(c.suggestedLoad(c.exercises[0])).toBe("90 kg"); // 75% of 120
  });

  it("lets a typed estimate override the derived value", () => {
    const c = logger({
      text: "3 x 5, 75%",
      one_rm: "120",
      e1rm: "200",
    });
    expect(c.effectiveOneRm(c.exercises[0])).toBe("200");
    expect(c.suggestedLoad(c.exercises[0])).toBe("150 kg"); // 75% of 200
  });

  it("falls back to derived when the typed value is non-numeric", () => {
    const c = logger({
      text: "3 x 5, 75%",
      one_rm: "120",
      e1rm: "abc",
    });
    expect(c.effectiveOneRm(c.exercises[0])).toBe("120");
  });

  it("flags when the suggestion is sized off the derived 1RM", () => {
    const c = logger({ text: "3 x 5, 75%", one_rm: "120", e1rm: "" });
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
            text: "3 x 5, 75%",
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
            text: "3 x 5, 75%",
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

  it("reflects the server's normalized manual value in the input", async () => {
    const c = logger({ id: 7, e1rm: "140.999", one_rm: "" });
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "141", source: "manual" } }));
    await c._postOneRm(c.exercises[0]);
    expect(c.exercises[0].e1rm).toBe("141"); // server quantized 140.999 -> 141
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
    const pA = c._postOneRm(c.exercises[0]); // sends the clear (value "")
    c.exercises[0].e1rm = "140"; // athlete types again before A lands
    const pB = c._postOneRm(c.exercises[0]); // sends "140" — supersedes A
    await Promise.all([pA, pB]);
    // A's stale clear must not wipe the value B set.
    expect(c.exercises[0].e1rm).toBe("140");
    expect(c.exercises[0].one_rm).toBe(""); // B's manual reconcile applied
  });

  it("does not let an in-flight clear wipe a value typed during the debounce", async () => {
    // The clear's POST is already sent, but the athlete types a new value before
    // its response lands and before the *next* debounced save fires. The lagging
    // clear must not wipe the in-progress value — the field no longer matches what
    // the clear sent.
    const c = logger({ id: 7, e1rm: "", one_rm: "120" });
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { one_rm: "120", source: "logged" } }));
    const pClear = c._postOneRm(c.exercises[0]); // sends value ""
    c.exercises[0].e1rm = "140"; // typed during the clear's flight
    await pClear;
    expect(c.exercises[0].e1rm).toBe("140"); // not wiped by the stale clear
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
            text: "3 x 5, 75%",
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
            text: "3 x 5, 75%",
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

// ---- freeform sub-line tracking (Phase 4a) ----
// Under each exercise the athlete keeps an editable sub-line stack — a free
// input per line, saved on blur to the per-cell endpoint. `saveCell` POSTs one
// (exercise_id, line, text) cell (modeled on `_postOneRm`: graceful on network
// failure, the typed value stays in-session), and `addLine` appends an empty
// sub-line up to MAX_CELL_LINE. Hydration threads `cell_url` + each exercise's
// `sub_lines` off the log payload.

const CELL_URL = "/meso/api/me/session/42/cell/";

function cellLogger(overrides = {}) {
  const c = createLogger();
  c.cellUrl = CELL_URL;
  c.csrf = "tok";
  c.exercises = [
    { id: 1, sub_lines: [{ line: 1, text: "RPE 8" }], set_rows: [] },
  ];
  return Object.assign(c, overrides);
}

describe("saveCell", () => {
  it("posts {exercise_id, line, text} to the cell url with the CSRF header", async () => {
    const c = cellLogger();
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { id: 5, line: 1, text: "RPE 8" } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledWith(
      CELL_URL,
      expect.objectContaining({ method: "POST" }),
    );
    const opts = global.fetch.mock.calls[0][1];
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(JSON.parse(opts.body)).toEqual({
      exercise_id: 1,
      line: 1,
      text: "RPE 8",
    });
  });

  it("is graceful when the network is unreachable — no throw, value kept", async () => {
    const c = cellLogger();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1); // must not throw
    expect(c.exercises[0].sub_lines[0].text).toBe("RPE 8"); // retained in-session
  });

  it("posts a blank text (clear semantics)", async () => {
    const c = cellLogger({
      exercises: [{ id: 1, sub_lines: [{ line: 2, text: "" }], set_rows: [] }],
    });
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { id: 9, line: 2, text: "" } } }),
    );
    await c.saveCell(c.exercises[0], 2);
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({
      exercise_id: 1,
      line: 2,
      text: "",
    });
  });

  // -- 5a §8: derive-on-read warn, mirrored from the cell response ----------

  it("sets the sub-line's warn flag from the response's cell.warn", async () => {
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "225 x" }], set_rows: [] },
      ],
    });
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: { ok: true, cell: { id: 5, line: 1, text: "225 x", warn: true } },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.exercises[0].sub_lines[0].warn).toBe(true);
  });

  it("clears a stale warn once the response reports it resolved", async () => {
    const c = cellLogger({
      exercises: [
        {
          id: 1,
          sub_lines: [{ line: 1, text: "225 x 5", warn: true }],
          set_rows: [],
        },
      ],
    });
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          ok: true,
          cell: { id: 5, line: 1, text: "225 x 5", warn: false },
        },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.exercises[0].sub_lines[0].warn).toBe(false);
  });

  it("ignores a stale response whose text is no longer in the input", async () => {
    // Two saves for one sub-line can be in flight at once and the OLDER reply
    // can land last. Without a guard, correcting "225 x" to "225 x 5" re-applies
    // the first reply's warn and strands the tint on text that no longer exists.
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "225 x" }], set_rows: [] },
      ],
    });
    global.fetch = vi.fn().mockImplementation(async () => {
      // While the request is in flight the athlete finishes typing.
      c.exercises[0].sub_lines[0].text = "225 x 5";
      return res({
        body: { ok: true, cell: { id: 5, line: 1, text: "225 x", warn: true } },
      });
    });

    await c.saveCell(c.exercises[0], 1);

    expect(c.exercises[0].sub_lines[0].warn).toBeFalsy();
  });

  it("serializes overlapping saves so the server writes them in order", async () => {
    // Ignoring a stale RESPONSE isn't enough — by then the server has already
    // written the stale text and re-parsed its LoggedSet from it. What matters
    // is the order the server FINISHES the writes in, so `applied` records
    // completion, not dispatch: unchained, the older request is still in flight
    // when the newer one lands, so the older one finishes LAST and its text
    // wins in the database while the UI shows the correction.
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "225 x" }], set_rows: [] },
      ],
    });

    const applied = [];
    let releaseFirst;
    const firstInFlight = new Promise((r) => {
      releaseFirst = r;
    });
    let call = 0;
    global.fetch = vi.fn().mockImplementation(async (_url, opts) => {
      const body = JSON.parse(opts.body);
      if (call++ === 0) await firstInFlight; // hold the OLDER request open
      applied.push(body.text); // the write lands here
      return res({
        body: { ok: true, cell: { id: 5, line: 1, text: body.text, warn: false } },
      });
    });

    const first = c.saveCell(c.exercises[0], 1);
    c.exercises[0].sub_lines[0].text = "225 x 5";
    const second = c.saveCell(c.exercises[0], 1);

    releaseFirst();
    await Promise.all([first, second]);

    // The corrected text must be what the server wrote LAST.
    expect(applied[applied.length - 1]).toBe("225 x 5");
  });

  // -- 5a §7: optimistic PR toast off a cell blur ----------------------------

  // The page-top card belongs to `save()` — "Log session" is a whole-session
  // act. A blur happens wherever the athlete is typing, so its celebration is
  // marked on the line that earned it; UAT found the card firing off-screen
  // every time.

  it("marks the line that earned the record, not the page-top card", async () => {
    const c = cellLogger();
    const pr = {
      key: "name:back squat",
      name: "Back Squat",
      value: "140",
      unit: "kg",
    };
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          ok: true,
          cell: { id: 5, line: 1, text: "RPE 8", warn: false },
          new_records: [pr],
        },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    const entry = c.exercises[0].sub_lines.find((l) => l.line === 1);
    expect(entry.pr).toBe("140 kg");
    expect(c.newRecords).toEqual([]); // the card is `save()`'s, untouched here
  });

  it("clears the line's mark once it no longer wins anything", async () => {
    const c = cellLogger();
    const entry = c.exercises[0].sub_lines.find((l) => l.line === 1);
    entry.pr = "140 kg";
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          ok: true,
          cell: { id: 5, line: 1, text: "RPE 8", warn: false },
          new_records: [],
        },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(entry.pr).toBe("");
  });

  it("leaves a card raised by Log session alone", async () => {
    const existing = [{ key: "name:bench", name: "Bench", value: "100" }];
    const c = cellLogger({ newRecords: existing });
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          ok: true,
          cell: { id: 5, line: 1, text: "RPE 8", warn: false },
          new_records: [],
        },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.newRecords).toBe(existing); // untouched, not reset to []
  });
});

describe("addLine", () => {
  it("appends an empty sub-line and stops at MAX_CELL_LINE", () => {
    const c = cellLogger({
      exercises: [{ id: 1, sub_lines: [], set_rows: [] }],
    });
    for (let i = 0; i < 25; i++) c.addLine(c.exercises[0]);
    const lines = c.exercises[0].sub_lines;
    expect(lines[0]).toMatchObject({ line: 1, text: "" });
    expect(lines.length).toBe(20); // capped at MAX_CELL_LINE
    expect(lines[lines.length - 1].line).toBe(20);
  });

  it("numbers past the max existing line, not the length (sparse stack)", () => {
    // The server dropped cleared line 1 but line 2 has text — a sparse stack.
    // Numbering by length+1 would fabricate a duplicate line 2; number off the
    // max existing line instead.
    const c = cellLogger({
      exercises: [{ id: 1, sub_lines: [{ line: 2, text: "x" }], set_rows: [] }],
    });
    c.addLine(c.exercises[0]);
    const lines = c.exercises[0].sub_lines;
    expect(lines.length).toBe(2);
    expect(lines[1].line).toBe(3); // max(2) + 1, not length(1) + 1 = 2
  });

  it("caps against the max existing line, not the length", () => {
    // A single line already at MAX_CELL_LINE — a length-based cap (1 < 20)
    // would wrongly allow another; cap off the max line value instead.
    const c = cellLogger({
      exercises: [{ id: 1, sub_lines: [{ line: 20, text: "x" }], set_rows: [] }],
    });
    c.addLine(c.exercises[0]);
    expect(c.exercises[0].sub_lines.length).toBe(1); // no-op at the cap
  });
});

describe("sub-line hydration", () => {
  it("hydrates cellUrl and each exercise's sub_lines from the payload", () => {
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        one_rm_url: ONE_RM_URL,
        cell_url: CELL_URL,
        status: "pending",
        unit: "kg",
        exercises: [
          {
            id: 7,
            text: "3 x 5",
            one_rm: "",
            one_rm_source: "",
            sub_lines: [{ line: 1, text: "RPE 8" }],
            set_rows: [],
          },
          {
            id: 8,
            text: "3 x 10",
            one_rm: "",
            one_rm_source: "",
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    expect(c.cellUrl).toBe(CELL_URL);
    expect(c.exercises[0].sub_lines).toMatchObject([{ line: 1, text: "RPE 8" }]);
    // An exercise with nothing typed yet OPENS with a line per prescribed set
    // rather than an empty stack. Blank cells aren't persisted, so "no
    // sub-lines" is the normal state — and it rendered as a bare "+ add a line"
    // button beneath three labelled set inputs, which made the freeform path
    // invisible. (No set_rows here, so the floor of one applies.)
    expect(c.exercises[1].sub_lines).toMatchObject([{ line: 1, text: "" }]);
  });

  it("opens with one empty line per prescribed set", () => {
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "3 x 10",
            one_rm: "",
            one_rm_source: "",
            set_rows: [{ set_number: 1 }, { set_number: 2 }, { set_number: 3 }],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    expect(c.exercises[0].sub_lines).toMatchObject([
      { line: 1, text: "" },
      { line: 2, text: "" },
      { line: 3, text: "" },
    ]);
  });

  it("fills gaps by number and keeps what the athlete typed", () => {
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "3 x 10",
            one_rm: "",
            one_rm_source: "",
            set_rows: [{ set_number: 1 }, { set_number: 2 }, { set_number: 3 }],
            // line 2 was cleared, so the server dropped it and kept line 3
            sub_lines: [{ line: 3, text: "100 x 5" }],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    // Rebuilt by NUMBER, so `line` (and the parsed set_number it becomes) is
    // identical on every reload.
    expect(c.exercises[0].sub_lines).toMatchObject([
      { line: 1, text: "" },
      { line: 2, text: "" },
      { line: 3, text: "100 x 5" },
    ]);
  });

  it("keeps lines the athlete added beyond the prescription", () => {
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "1 x 10",
            one_rm: "",
            one_rm_source: "",
            set_rows: [{ set_number: 1 }],
            sub_lines: [
              { line: 1, text: "100 x 5" },
              { line: 2, text: "105 x 5" },
            ],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    expect(c.exercises[0].sub_lines).toHaveLength(2);
  });

  it("does not add a second empty line when one already exists", () => {
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "3 x 10",
            one_rm: "",
            one_rm_source: "",
            set_rows: [],
            sub_lines: [{ line: 1, text: "" }],
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    expect(c.exercises[0].sub_lines).toMatchObject([{ line: 1, text: "" }]);
  });
});

// ---- offline queue — sub-line cells (issue #527) ---------------------------
//
// A line typed under "what you did" saves on blur (saveCell → _postCell), but
// only `save()`'s whole-session payload was ever queued when the network was
// down — a failed cell write showed "couldn't save" and nothing retried it,
// so a set typed offline was lost while the page still said "Saved ✓". These
// pin the contract the fix implements: a failed cell write gets its own
// `kind: "cell"` entry in the SAME `meso-log-queue` (latest text per line
// wins), `flushQueue` drains every cell before the log — one request at a
// time, stopping at the first failure — `init()` folds an already-queued
// cell back onto its line before flushing, and `save()` waits on the cell
// queue before it POSTs its own log.

const OTHER_SESSION_CELL_URL = "/meso/api/me/session/99/cell/";

describe("saveCell — latest-wins queue keying (#527)", () => {
  it("keeps exactly one cell entry per line, holding the latest text", async () => {
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "100 x 5" }], set_rows: [] },
      ],
    });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);
    c.exercises[0].sub_lines[0].text = "110 x 5"; // corrected before it ever synced
    await c.saveCell(c.exercises[0], 1);

    const cellEntries = c.readQueue().filter((i) => i.kind === "cell");
    expect(cellEntries).toHaveLength(1);
    expect(cellEntries[0]).toMatchObject({
      kind: "cell",
      url: CELL_URL,
      body: { exercise_id: 1, line: 1, text: "110 x 5" },
    });
  });

  it("queues two different lines as two separate entries", async () => {
    const c = cellLogger({
      exercises: [
        {
          id: 1,
          sub_lines: [
            { line: 1, text: "100 x 5" },
            { line: 2, text: "RPE 8" },
          ],
          set_rows: [],
        },
      ],
    });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);
    await c.saveCell(c.exercises[0], 2);

    const cellEntries = c.readQueue().filter((i) => i.kind === "cell");
    expect(cellEntries).toHaveLength(2);
    expect(cellEntries.map((i) => i.body.line).sort()).toEqual([1, 2]);
  });

  it("leaves another session's cell entry untouched", async () => {
    const c = cellLogger();
    c.writeQueue([
      {
        kind: "cell",
        url: OTHER_SESSION_CELL_URL,
        body: { exercise_id: 1, line: 1, text: "someone else's line" },
      },
    ]);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);

    const queue = c.readQueue();
    expect(queue).toHaveLength(2);
    const other = queue.find((i) => i.url === OTHER_SESSION_CELL_URL);
    expect(other.body.text).toBe("someone else's line");
  });

  it("leaves an already-queued log entry untouched", async () => {
    const c = cellLogger({ logUrl: LOG_URL });
    c.writeQueue([{ url: LOG_URL, body: { status: "done", sets: [] } }]);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);

    const queue = c.readQueue();
    expect(queue).toHaveLength(2);
    expect(queue.find((i) => i.url === LOG_URL).body).toEqual({
      status: "done",
      sets: [],
    });
  });
});

describe("saveCell — outcomes that decide whether the write gets queued (#527)", () => {
  it.each([400, 404])(
    "a %i response is a real rejection — not queued, saveError set",
    async (status) => {
      const c = cellLogger();
      global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status }));
      await c.saveCell(c.exercises[0], 1);
      expect(c.readQueue()).toHaveLength(0);
      expect(c.exercises[0].sub_lines[0].saveError).toBe(true);
      expect(c.exercises[0].sub_lines[0].queued).toBeFalsy();
    },
  );

  it("a 4xx drops an older queued entry for the same cell", async () => {
    const c = cellLogger();
    c.writeQueue([
      { kind: "cell", url: CELL_URL, body: { exercise_id: 1, line: 1, text: "stale" } },
    ]);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 400 }));
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toHaveLength(0);
  });

  it("a 5xx is queued, like a network failure — not a saveError", async () => {
    const c = cellLogger();
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
    expect(c.exercises[0].sub_lines[0].saveError).toBeFalsy();
  });

  it("a 403 is queued, like a network failure — not a saveError", async () => {
    const c = cellLogger();
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 403 }));
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
    expect(c.exercises[0].sub_lines[0].saveError).toBeFalsy();
  });

  it("contrast: a rejected fetch is queued, not a saveError", async () => {
    const c = cellLogger();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
    expect(c.exercises[0].sub_lines[0].saveError).toBeFalsy();
  });

  it("contrast: a redirect (expired session) is queued, not a saveError", async () => {
    const c = cellLogger();
    global.fetch = vi.fn().mockResolvedValue(res({ redirected: true }));
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
  });
});

describe("flushQueue — cells drain before the log, one request at a time (#527)", () => {
  it("sends every kind:'cell' entry before the log, whatever order they were stored in", async () => {
    const c = cellLogger({ logUrl: LOG_URL });
    // The log was queued FIRST (an earlier failed "Log session"); a cell
    // failed later — cells still go first once a flush actually runs.
    c.writeQueue([
      { url: LOG_URL, body: { status: "done", sets: [] } },
      {
        kind: "cell",
        url: CELL_URL,
        body: { exercise_id: 1, line: 1, text: "100 x 5" },
      },
    ]);
    const calls = [];
    global.fetch = vi.fn().mockImplementation(async (url) => {
      calls.push(url);
      return res({
        body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } },
      });
    });
    await c.flushQueue();
    expect(calls).toEqual([CELL_URL, LOG_URL]);
  });

  it("sends one request at a time — the next fetch waits for the previous to resolve", async () => {
    const c = cellLogger({ logUrl: LOG_URL });
    c.writeQueue([
      {
        kind: "cell",
        url: CELL_URL,
        body: { exercise_id: 1, line: 1, text: "100 x 5" },
      },
      {
        kind: "cell",
        url: CELL_URL,
        body: { exercise_id: 1, line: 2, text: "RPE 8" },
      },
    ]);
    let releaseFirst;
    const held = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    let calls = 0;
    global.fetch = vi.fn().mockImplementation(async () => {
      calls += 1;
      if (calls === 1) await held; // hold the first request open
      return res({
        body: { ok: true, cell: { line: 1, text: "x", warn: false } },
      });
    });

    const flushed = c.flushQueue();
    await vi.waitFor(() => expect(calls).toBe(1));
    // Give a concurrent second request every chance to start.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toBe(1);
    releaseFirst();
    await flushed;
    expect(calls).toBe(2);
  });

  it("stops at the first failure — a cell that fails offline blocks the log behind it", async () => {
    const c = cellLogger({ logUrl: LOG_URL });
    c.writeQueue([
      {
        kind: "cell",
        url: CELL_URL,
        body: { exercise_id: 1, line: 1, text: "100 x 5" },
      },
      { url: LOG_URL, body: { status: "done", sets: [] } },
    ]);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.flushQueue();
    expect(global.fetch).toHaveBeenCalledTimes(1); // the log was never attempted
    expect(c.readQueue()).toHaveLength(2); // both remain queued, in order
  });

  it("a 4xx on a queued cell drops it and flags the page's line — the pass continues", async () => {
    const c = cellLogger({
      logUrl: LOG_URL,
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "bad", queued: true }], set_rows: [] },
      ],
    });
    c.writeQueue([
      { kind: "cell", url: CELL_URL, body: { exercise_id: 1, line: 1, text: "bad" } },
      { url: LOG_URL, body: { status: "done", sets: [] } },
    ]);
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 400 });
      return res({ body: { log: { status: "done", sets: [] } } });
    });
    await c.flushQueue();
    const line = c.exercises[0].sub_lines[0];
    expect(line.saveError).toBe(true);
    expect(line.queued).toBe(false);
    expect(c.readQueue()).toHaveLength(0); // the bad cell is gone; the log still ran
  });

  it("a synced cell reconciles the page's line: queued clears, savedText and warn apply", async () => {
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "225 x", queued: true }], set_rows: [] },
      ],
    });
    c.writeQueue([
      { kind: "cell", url: CELL_URL, body: { exercise_id: 1, line: 1, text: "225 x" } },
    ]);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "225 x", warn: true } } }),
    );
    await c.flushQueue();
    const line = c.exercises[0].sub_lines[0];
    expect(line.queued).toBe(false);
    expect(line.savedText).toBe("225 x");
    expect(line.warn).toBe(true);
    expect(c.readQueue()).toHaveLength(0);
  });

  it("does nothing when the queue is empty (no cells either)", async () => {
    const c = cellLogger();
    global.fetch = vi.fn();
    await c.flushQueue();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("init — folds a queued cell onto its line, then flushes it (#527)", () => {
  it("hydrates the queued text onto the matching line and marks it queued", async () => {
    localStorage.setItem(
      "meso-log-queue",
      JSON.stringify([
        {
          kind: "cell",
          url: CELL_URL,
          body: { exercise_id: 7, line: 1, text: "100 x 5" },
        },
      ]),
    );
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "3 x 5",
            one_rm: "",
            one_rm_source: "",
            sub_lines: [{ line: 1, text: "" }], // what the server last rendered
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    const c = createLogger();
    c.init();

    const line = c.exercises[0].sub_lines.find((l) => l.line === 1);
    expect(line.text).toBe("100 x 5");
    expect(line.queued).toBe(true);

    await c.flushQueue(); // let the fold-in's own flush pass land
    expect(c.readQueue()).toHaveLength(0);
  });

  it("creates the line when the queued cell isn't in sub_lines yet", () => {
    localStorage.setItem(
      "meso-log-queue",
      JSON.stringify([
        {
          kind: "cell",
          url: CELL_URL,
          body: { exercise_id: 7, line: 4, text: "extra note" },
        },
      ]),
    );
    document.body.innerHTML =
      '<span id="meso-csrf" data-token="tok"></span>' +
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "3 x 5",
            one_rm: "",
            one_rm_source: "",
            sub_lines: [{ line: 1, text: "" }],
            set_rows: [],
          },
        ],
      }) +
      "</script>";
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch")); // stays offline
    const c = createLogger();
    c.init();

    const line = c.exercises[0].sub_lines.find((l) => l.line === 4);
    expect(line).toBeTruthy();
    expect(line.text).toBe("extra note");
    expect(line.queued).toBe(true);
  });
});

describe("dirty check — a blur that changed nothing posts nothing (#527)", () => {
  function initLoggerWithHydratedLines(subLines, setRows) {
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          {
            id: 7,
            text: "3 x 5",
            one_rm: "",
            one_rm_source: "",
            sub_lines: subLines,
            set_rows: setRows,
          },
        ],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    return c;
  }

  it("makes no fetch tabbing through an untouched coach line or a padded blank line", async () => {
    // "RPE 8" is the coach's server-rendered cue; line 2 is the blank pad
    // init() adds for the second prescribed set. Neither was typed into.
    const c = initLoggerWithHydratedLines(
      [{ line: 1, text: "RPE 8" }],
      [{ set_number: 1 }, { set_number: 2 }],
    );
    // Configured with a real response (not a bare `vi.fn()`) so that if the
    // dirty check is missing and a fetch fires anyway, the assertion below
    // fails cleanly instead of throwing on an unmocked `res.redirected`.
    global.fetch = vi.fn().mockResolvedValue(res({ body: { ok: true, cell: {} } }));
    await c.saveCell(c.exercises[0], 1);
    await c.saveCell(c.exercises[0], 2);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("makes no fetch re-saving the same text after a successful save", async () => {
    const c = initLoggerWithHydratedLines([{ line: 1, text: "" }], []);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    c.exercises[0].sub_lines[0].text = "100 x 5";
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);

    global.fetch.mockClear();
    await c.saveCell(c.exercises[0], 1); // tabbed through again, unchanged
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("fetches when the text actually changed", async () => {
    const c = initLoggerWithHydratedLines([{ line: 1, text: "RPE 8" }], []);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "RPE 9", warn: false } } }),
    );
    c.exercises[0].sub_lines[0].text = "RPE 9";
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("still fetches a queued line even though its text still equals savedText", async () => {
    // An earlier save is stuck offline (queued=true); the dirty check must
    // not skip it just because the text matches what the server last
    // confirmed — that confirmation is stale.
    const c = initLoggerWithHydratedLines([{ line: 1, text: "100 x 5" }], []);
    c.exercises[0].sub_lines[0].queued = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("still posts a line with no savedText (this file's plain cellLogger fixtures)", async () => {
    // savedText is only set by init()/a successful save; a line built by
    // hand (as cellLogger() does) has none, so it must post as today.
    const c = cellLogger();
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "RPE 8", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("queue ownership — one athlete never flushes another's writes (#527)", () => {
  // localStorage outlasts a logout. Sent under the next athlete's login, the
  // last one's queued line comes back 404 ("not your session") and a refused
  // line is dropped: their only copy of the set, gone.
  const OTHER = "athlete-b";

  it("stamps each queued write with the signed-in athlete", async () => {
    const c = cellLogger({ owner: "athlete-a", logUrl: LOG_URL });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);
    c.enqueue({ status: "done", sets: [] });
    expect(c.readQueue().map((i) => i.owner)).toEqual(["athlete-a", "athlete-a"]);
  });

  it("leaves another athlete's entries queued and unsent", async () => {
    const c = cellLogger({ owner: "athlete-a", logUrl: LOG_URL });
    const theirs = [
      {
        kind: "cell",
        url: OTHER_SESSION_CELL_URL,
        body: { exercise_id: 3, line: 1, text: "90 x 8" },
        owner: OTHER,
      },
      { url: "/meso/api/me/session/99/log/", body: { sets: [] }, owner: OTHER },
    ];
    const mine = {
      kind: "cell",
      url: CELL_URL,
      body: { exercise_id: 1, line: 1, text: "RPE 8" },
      owner: "athlete-a",
    };
    c.writeQueue([...theirs, mine]);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 404 }));
    await c.flushQueue();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch.mock.calls[0][0]).toBe(CELL_URL);
    expect(c.readQueue()).toEqual(theirs);
  });

  it("doesn't fold another athlete's line onto this page", () => {
    localStorage.setItem(
      "meso-log-queue",
      JSON.stringify([
        {
          kind: "cell",
          url: CELL_URL,
          body: { exercise_id: 7, line: 1, text: "90 x 8" },
          owner: OTHER,
        },
      ]),
    );
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        owner: "athlete-a",
        status: "pending",
        exercises: [{ id: 7, sub_lines: [], set_rows: [] }],
      }) +
      "</script>";
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const c = createLogger();
    c.init();
    expect(c.exercises[0].sub_lines[0].text).toBe("");
    expect(c.exercises[0].sub_lines[0].queued).toBe(false);
  });

  it("still flushes an entry queued before entries had an owner", async () => {
    const c = cellLogger({ owner: "athlete-a", logUrl: LOG_URL });
    c.writeQueue([{ url: LOG_URL, body: { status: "done", sets: [] } }]);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { log: { status: "done", sets: [] } } }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("edges: a warned line, a second tab, full storage (#527)", () => {
  it("still posts an unchanged line that carries a warning", async () => {
    // Set-shaped text saved while its row was skipped has no set; once the
    // coach un-skips the row, re-sending the same text is what creates it.
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.text = "100 x 5";
    line.savedText = "100 x 5";
    line.warn = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(line.warn).toBe(false);
  });

  it("keeps a newer entry another tab queued while this write was in flight", async () => {
    const c = cellLogger();
    c.exercises[0].sub_lines[0].text = "100 x 5";
    const newer = {
      kind: "cell",
      url: CELL_URL,
      body: { exercise_id: 1, line: 1, text: "110 x 5" },
    };
    global.fetch = vi.fn().mockImplementation(async () => {
      c.writeQueue([newer]); // the other tab, offline, retyped the line
      return res({
        body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } },
      });
    });
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toEqual([newer]);
  });

  it("doesn't overwrite a newer entry another tab queued while this write failed", async () => {
    const c = cellLogger();
    c.exercises[0].sub_lines[0].text = "100 x 5";
    const newer = {
      kind: "cell",
      url: CELL_URL,
      body: { exercise_id: 1, line: 1, text: "110 x 5" },
    };
    global.fetch = vi.fn().mockImplementation(async () => {
      c.writeQueue([newer]);
      throw new TypeError("Failed to fetch");
    });
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toEqual([newer]);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
  });

  it("still queues a failed write when another tab flushed the old entry away", async () => {
    const c = cellLogger();
    c.exercises[0].sub_lines[0].text = "110 x 5";
    const old = c.enqueueCell({ exercise_id: 1, line: 1, text: "100 x 5" });
    global.fetch = vi.fn().mockImplementation(async () => {
      c.dropEntry(old); // the other tab synced "100 x 5" and drops that entry
      throw new TypeError("Failed to fetch");
    });
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue().map((i) => i.body.text)).toEqual(["110 x 5"]);
  });

  it("puts a line in the outbox before its request goes out", async () => {
    // Closing the page mid-request (a POST stalled on gym wifi) must not
    // lose the line: it's already queued, and only a landed save takes it out.
    const c = cellLogger();
    c.exercises[0].sub_lines[0].text = "100 x 5";
    let land;
    global.fetch = vi.fn().mockImplementation(
      () =>
        new Promise((resolve) => {
          land = () =>
            resolve(
              res({ body: { ok: true, cell: { line: 1, text: "100 x 5" } } }),
            );
        }),
    );
    const saving = c.saveCell(c.exercises[0], 1);
    await vi.waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(c.readQueue().map((i) => i.body.text)).toEqual(["100 x 5"]);
    expect(c.exercises[0].sub_lines[0].queued).toBeFalsy(); // sending, not waiting
    land();
    await saving;
    expect(c.readQueue()).toHaveLength(0);
  });

  it("clears the footer's 'will sync' once a queued line saves on a later blur", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines[0].text = "100 x 5";
    // The line's blur got a 500 and waits in the queue; "Log session" landed
    // (its flush retried the line, which failed again).
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 500 });
      return res({ body: { log: { status: "done", sets: [] } } });
    });
    await c.saveCell(c.exercises[0], 1);
    await c.save(true);
    expect(c.queued).toBe(true);

    // The server recovers; the athlete blurs the line again.
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue()).toHaveLength(0);
    expect(c.queued).toBe(false);
    expect(c.saved).toBe(true);
  });

  it("tells a later entry with the same text from the one it replaced", async () => {
    // "90 x 5" was queued; this tab sends "100 x 5"; meanwhile the other tab
    // goes back to "90 x 5". Same text as the old entry, but a newer write.
    const c = cellLogger();
    c.exercises[0].sub_lines[0].text = "100 x 5";
    c.enqueueCell({ exercise_id: 1, line: 1, text: "90 x 5" });
    global.fetch = vi.fn().mockImplementation(async () => {
      const other = createLogger();
      other.cellUrl = CELL_URL;
      other.enqueueCell({ exercise_id: 1, line: 1, text: "90 x 5" });
      throw new TypeError("Failed to fetch");
    });
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue().map((i) => i.body.text)).toEqual(["90 x 5"]);
  });

  it("keeps a line dirty when a saved response can't be read", async () => {
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.savedText = "RPE 8";
    line.text = "225 x";
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      redirected: false,
      json: async () => {
        throw new DOMException("The operation was aborted.", "AbortError");
      },
    });
    await c.saveCell(c.exercises[0], 1);
    expect(line.queued).toBe(false);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "225 x", warn: true } } }),
    );
    await c.saveCell(c.exercises[0], 1); // tabbed through, unchanged
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(line.warn).toBe(true);
  });

  it("waits for a line blurred while Log session is settling", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines.push({ line: 2, text: "110 x 5" });
    const calls = [];
    const release = {};
    global.fetch = vi.fn().mockImplementation(async (url, opts) => {
      if (url !== CELL_URL) {
        calls.push("log");
        return res({ body: { log: { status: "done", sets: [] } } });
      }
      const { line } = JSON.parse(opts.body);
      calls.push(line);
      await new Promise((r) => {
        release[line] = r;
      });
      return res({ body: { ok: true, cell: { warn: false } } });
    });
    c.saveCell(c.exercises[0], 1); // in flight when Log session is pressed
    const saving = c.save(true);
    await vi.waitFor(() => expect(calls).toEqual([1]));
    c.saveCell(c.exercises[0], 2); // blurred while save() waits
    await vi.waitFor(() => expect(calls).toEqual([1, 2]));
    release[1]();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toEqual([1, 2]); // the log waits for line 2 to land
    release[2]();
    await saving;
    expect(calls).toEqual([1, 2, "log"]);
  });

  it("waits for a line blurred while Log session flushes the queue", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines.push({ line: 2, text: "110 x 5" });
    c.exercises[0].sub_lines[0].queued = true;
    c.enqueueCell({ exercise_id: 1, line: 1, text: "RPE 8" });
    const calls = [];
    const release = {};
    global.fetch = vi.fn().mockImplementation(async (url, opts) => {
      if (url !== CELL_URL) {
        calls.push("log");
        return res({ body: { log: { status: "done", sets: [] } } });
      }
      const { line } = JSON.parse(opts.body);
      calls.push(line);
      await new Promise((r) => {
        release[line] = r;
      });
      return res({ body: { ok: true, cell: { warn: false } } });
    });
    const saving = c.save(true);
    await vi.waitFor(() => expect(calls).toEqual([1])); // the queued line
    c.saveCell(c.exercises[0], 2); // blurred mid-flush
    await vi.waitFor(() => expect(calls).toEqual([1, 2]));
    release[1]();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toEqual([1, 2]); // the log waits for line 2 to land
    release[2]();
    await saving;
    expect(calls).toEqual([1, 2, "log"]);
  });

  it("takes 'Saved ✓' down when a line fails after it went up", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines[0].text = "100 x 5";
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { log: { status: "done", sets: [] } } }),
    );
    await c.save(true);
    expect(c.saved).toBe(true);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1); // a blur that lands after the log
    expect(c.saved).toBe(false);
    expect(c.queued).toBe(true);
  });

  it("says a line couldn't save when storage refuses the queue", async () => {
    const c = cellLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);
    const line = c.exercises[0].sub_lines[0];
    expect(line.queued).toBe(false);
    expect(line.saveError).toBe(true);
  });

  it("says the session couldn't save when storage refuses the queue", async () => {
    const c = makeLogger();
    c.exercises[0].set_rows[0].done = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.save(true);
    expect(c.queued).toBe(false);
    expect(c.error).toBe(true);
  });
});

describe("a write that never answers counts as offline (#527)", () => {
  // Gym wifi can connect and then never answer, and fetch has no timeout.
  // "Log session" waits for the lines, so an unbounded one would hold it on
  // "Saving…" forever with nothing queued.
  // A request that never answers; only aborting it ends it.
  function hangs(url, opts) {
    return new Promise((_, reject) => {
      if (!opts.signal) return; // nothing can ever end it
      opts.signal.addEventListener("abort", () =>
        reject(new DOMException("The operation was aborted.", "AbortError")),
      );
    });
  }

  it("queues a stalled line and lets Log session finish", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines[0].text = "100 x 5";
    global.fetch = vi.fn().mockImplementation((url, opts) => {
      if (url === CELL_URL) return hangs(url, opts);
      return Promise.resolve(res({ body: { log: { status: "done", sets: [] } } }));
    });
    c.saveCell(c.exercises[0], 1); // the blur, still waiting on an answer
    const saving = c.save(true);
    await vi.advanceTimersByTimeAsync(15000); // the blur gives up
    await vi.advanceTimersByTimeAsync(15000); // the flush's retry does too
    await saving;
    expect(c.saving).toBe(false);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
    expect(c.readQueue().filter((i) => i.kind === "cell")).toHaveLength(1);
    expect(c.queued).toBe(true); // the line still waits, so no "Saved ✓"
    expect(c.saved).toBe(false);
  });

  it("ends a flush pass that stalls, so the next one can run", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.writeQueue([{ url: LOG_URL, body: { status: "done", sets: [] } }]);
    global.fetch = vi.fn().mockImplementation(hangs);
    const first = c.flushQueue();
    await vi.advanceTimersByTimeAsync(15000);
    await first;
    expect(c.readQueue()).toHaveLength(1);

    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { log: { status: "done", sets: [] } } }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("a refused line is dropped where the athlete can see it (#527)", () => {
  const theirs = {
    kind: "cell",
    url: OTHER_SESSION_CELL_URL,
    body: { exercise_id: 3, line: 1, text: "90 x 8" },
  };

  it("keeps another session's refused line for that session's page", async () => {
    const c = cellLogger({ logUrl: LOG_URL });
    c.writeQueue([theirs]);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 400 }));
    await c.flushQueue();
    expect(c.readQueue()).toEqual([theirs]);
  });

  it("drops it on its own page once its row is gone", async () => {
    const c = cellLogger({ logUrl: LOG_URL, cellUrl: OTHER_SESSION_CELL_URL });
    c.writeQueue([theirs]);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 400 }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("footer line error clears once the line saves (#527)", () => {
  it("drops 'a line above couldn't save' when the refused line is fixed", async () => {
    const c = cellLogger({ logUrl: LOG_URL });
    const line = c.exercises[0].sub_lines[0];
    line.saveError = true;
    c.lineError = true;
    line.text = "100 x 5";
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(line.saveError).toBe(false);
    expect(c.lineError).toBe(false);
  });

  it("keeps it while another line is still refused", async () => {
    const c = cellLogger({
      logUrl: LOG_URL,
      exercises: [
        {
          id: 1,
          sub_lines: [
            { line: 1, text: "100 x 5", saveError: true },
            { line: 2, text: "bad", saveError: true },
          ],
          set_rows: [],
        },
      ],
    });
    c.lineError = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.lineError).toBe(true);
  });
});

describe("save() — waits on the cell queue before its own log POST (#527)", () => {
  function loggerWithAQueuedLine() {
    const c = makeLogger();
    c.cellUrl = CELL_URL;
    c.exercises[0].sub_lines = [{ line: 1, text: "100 x 5", queued: true }];
    c.writeQueue([
      {
        kind: "cell",
        url: CELL_URL,
        body: { exercise_id: c.exercises[0].id, line: 1, text: "100 x 5" },
      },
    ]);
    return c;
  }

  it("fetches the cell before the log, and reports Saved once both land", async () => {
    vi.useFakeTimers();
    const c = loggerWithAQueuedLine();
    const calls = [];
    global.fetch = vi.fn().mockImplementation(async (url) => {
      calls.push(url);
      if (url === CELL_URL) {
        return res({
          body: { ok: true, cell: { line: 1, text: "100 x 5", warn: false } },
        });
      }
      return res({ body: { log: { status: "pending", sets: [] } } });
    });
    await c.save(false);
    expect(calls).toEqual([CELL_URL, LOG_URL]);
    expect(c.saved).toBe(true);
    expect(c.queued).toBe(false);
  });

  it("offline: the flush's cell attempt and save's own log POST both fail, so it stays queued", async () => {
    const c = loggerWithAQueuedLine();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.save(false);
    expect(c.queued).toBe(true);
    expect(c.saved).toBe(false);
  });

  it("a stuck cell (500) beats a successful log — save() must not claim Saved", async () => {
    // This is the heart of #527: the log endpoint alone succeeding used to
    // be enough for save() to say "Saved ✓" even though a line's own write
    // was still failing behind it.
    const c = loggerWithAQueuedLine();
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 500 });
      return res({ body: { log: { status: "pending", sets: [] } } });
    });
    await c.save(false);
    expect(c.saved).toBe(false);
    expect(c.queued).toBe(true);
  });

  it("a line the server refused keeps the tick off and says so", async () => {
    // The log landed, but the refused line didn't: "Saved ✓" would claim both.
    const c = loggerWithAQueuedLine();
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 400 });
      return res({ body: { log: { status: "done", sets: [] } } });
    });
    await c.save(true);
    expect(c.exercises[0].sub_lines[0].saveError).toBe(true);
    expect(c.saved).toBe(false);
    expect(c.queued).toBe(false);
    expect(c.lineError).toBe(true);
  });
});

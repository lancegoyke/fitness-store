// Tests for the athlete session logger (app/store_project/static/js/meso_athlete.js).
//
// Focus: the logic that is fragile and effectively impossible to verify by hand
// — the offline write queue (stash on network failure, dedupe per session,
// replay on reconnect), the typed-line save path, and the finish/flush state
// machine. The athlete logs by typing lines; `finish()` only stamps the session
// done, and the server's `progress` count rides back on every response.

import {
  createLogger,
  epleyOneRm,
  roundToStep,
  loadForPercent,
} from "../app/store_project/static/js/meso_athlete.js";

const LOG_URL = "/meso/api/me/session/42/log/";
const ONE_RM_URL = "/meso/api/me/session/42/one-rm/";

// A minimal logger with two exercises.
function makeLogger(overrides = {}) {
  const c = createLogger();
  c.logUrl = LOG_URL;
  c.csrf = "tok";
  c.status = "pending";
  c.exercises = [
    { id: 1, sub_lines: [] },
    { id: 2, sub_lines: [] },
  ];
  return Object.assign(c, overrides);
}

// Build a fetch Response stub. `body` is returned from .json(); `jsonError`
// makes .json() reject, like a real fetch on a non-JSON body.
function res({ ok = true, status = 200, redirected = false, body = {}, jsonError = false } = {}) {
  return {
    ok,
    status,
    redirected,
    json: async () => {
      if (jsonError) throw new SyntaxError("Unexpected token < in JSON");
      return body;
    },
  };
}

// The 200 body of the log endpoint (contract): the log, plus the session's count.
function logBody(status = "done", progress = { logged: 3, prescribed: 4 }) {
  return { ok: true, log: { id: 1, status, date: null, notes: "" }, progress };
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
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
    c.enqueue({ status: "pending" });
    c.enqueue({ status: "done" });
    const q = c.readQueue();
    expect(q).toHaveLength(1);
    expect(q[0].url).toBe(LOG_URL);
    expect(q[0].body.status).toBe("done");
  });

  it("does not clobber another session's queued save", () => {
    const c = makeLogger();
    c.writeQueue([{ url: "/meso/api/me/session/99/log/", body: { status: "done" } }]);
    c.enqueue({ status: "pending" });
    expect(c.readQueue()).toHaveLength(2);
  });
});

describe("progress (applyProgress / progressLabel)", () => {
  it.each([
    [{ logged: 0, prescribed: 4 }, "0 of 4 sets logged"],
    [{ logged: 1, prescribed: 1 }, "1 of 1 set logged"],
    [{ logged: 3, prescribed: 4 }, "3 of 4 sets logged"],
    [{ logged: 0, prescribed: 0 }, "0 sets logged"],
    [{ logged: 1, prescribed: 0 }, "1 set logged"],
    [{ logged: 5, prescribed: 3 }, "5 of 3 sets logged"],
  ])("formats %j as %s", (progress, label) => {
    const c = makeLogger();
    c.applyProgress(progress);
    expect(c.progressLabel).toBe(label);
  });

  it("starts at 0 sets logged", () => {
    expect(createLogger().progressLabel).toBe("0 sets logged");
  });

  it.each([
    undefined,
    null,
    "3 of 4",
    [],
    {},
    { logged: 1 },
    { prescribed: 4 },
    { logged: "1", prescribed: 4 },
    { logged: 1.5, prescribed: 4 },
    { logged: -1, prescribed: 4 },
    { logged: 1, prescribed: -4 },
    { logged: NaN, prescribed: 4 },
  ])("ignores malformed progress %j", (bad) => {
    const c = makeLogger();
    c.applyProgress({ logged: 2, prescribed: 4 });
    c.applyProgress(bad);
    expect(c.progress).toEqual({ logged: 2, prescribed: 4 });
  });

  it("init() reads progress from the injected page data", () => {
    const el = document.createElement("script");
    el.id = "meso-log-data";
    el.type = "application/json";
    el.textContent = JSON.stringify({
      log_url: LOG_URL,
      status: "pending",
      progress: { logged: 2, prescribed: 6 },
      exercises: [],
    });
    document.body.appendChild(el);
    try {
      vi.spyOn(window, "addEventListener").mockImplementation(() => {});
      global.fetch = vi.fn();
      const c = createLogger();
      c.init();
      expect(c.progressLabel).toBe("2 of 6 sets logged");
    } finally {
      el.remove();
    }
  });
});

describe("finish", () => {
  it("posts exactly {status:'done'} (no sets) after the lines settle", async () => {
    vi.useFakeTimers();
    const c = makeLogger({ cellUrl: "/meso/api/me/session/42/cell/" });
    c.exercises = [{ id: 1, sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] }];
    const calls = [];
    global.fetch = vi.fn(async (url, opts) => {
      calls.push({ url, body: JSON.parse(opts.body) });
      if (url === LOG_URL) return res({ body: logBody("done", { logged: 1, prescribed: 4 }) });
      return res({ body: { cell: { warn: false } } });
    });
    const pending = c.saveCell(c.exercises[0], 1); // the blur the tap causes
    const finishing = c.finish();
    await pending;
    await finishing;
    const urls = calls.map((x) => x.url);
    expect(urls.indexOf("/meso/api/me/session/42/cell/")).toBeLessThan(urls.indexOf(LOG_URL));
    const log = calls.find((x) => x.url === LOG_URL);
    expect(log.body).toEqual({ status: "done" });
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("1 of 4 sets logged");
    expect(c.readQueue()).toHaveLength(0);
  });

  it("queues the write (not an error) when the network is unreachable", async () => {
    const c = makeLogger();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.finish();
    expect(c.queued).toBe(true);
    expect(c.error).toBe(false);
    expect(c.saving).toBe(false);
    expect(c.readQueue()).toHaveLength(1);
    // Storage TOOK the queued write, so the optimistic "done" stands.
    expect(c.status).toBe("done");
    expect(c.statusBeforeQueued).toBe("pending");
    expect(c.readQueue()[0].body).toEqual({ status: "done" });
  });

  it("a later flush landing applies the response's status and progress", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.finish();
    expect(c.progressLabel).toBe("0 sets logged");
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: logBody("done", { logged: 3, prescribed: 4 }) }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.queued).toBe(false);
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("3 of 4 sets logged");
  });

  it("queues the write when the request is redirected to login", async () => {
    const c = makeLogger();
    global.fetch = vi.fn().mockResolvedValue(res({ redirected: true }));
    await c.finish();
    expect(c.queued).toBe(true);
    expect(c.error).toBe(false);
    expect(c.readQueue()).toHaveLength(1);
    // Same pin as the network case: storage took it, so nothing is put back.
    expect(c.status).toBe("done");
  });

  it("surfaces a non-retryable HTTP error and brings the button back", async () => {
    const c = makeLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 404 }));
    await c.finish();
    expect(c.error).toBe(true);
    expect(c.status).toBe("pending");
    expect(c.queued).toBe(false);
    expect(c.readQueue()).toHaveLength(0);
  });

  // #570: a refusal is deterministic — retrying the same payload can only fail
  // again — so the optimistic "done" has to come back off (nothing was kept).
  it("takes the optimistic status back off when the server refuses", async () => {
    const c = makeLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, status: 400, body: { ok: false, error: "nope" } }),
    );
    await c.finish();
    expect(c.error).toBe(true);
    expect(c.status).toBe("pending"); // back to what it was before
    expect(c.readQueue()).toHaveLength(0);
  });

  // The Finish button hides once the status is "done", so a failure must leave
  // either a queued retry or a visible button — never neither.
  it.each([500, 503, 408, 429])(
    "queues the write for a retryable %i and keeps the optimistic status",
    async (status) => {
      const c = makeLogger();
      global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status }));
      await c.finish();
      expect(c.status).toBe("done");
      expect(c.statusBeforeQueued).toBe("pending");
      expect(c.error).toBe(false);
      expect(c.queued).toBe(true);
      expect(c.readQueue()).toHaveLength(1);
      expect(c.readQueue()[0].body).toEqual({ status: "done" });
    },
  );

  it("a later flush delivers a finish that got a 503, applying status and progress", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 503 }));
    await c.finish();
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: logBody("done", { logged: 2, prescribed: 4 }) }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("2 of 4 sets logged");
  });

  it("takes the status back off when a retryable status can't be queued (storage full)", async () => {
    const c = makeLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 503 }));
    await c.finish();
    expect(c.status).toBe("pending"); // the button comes back
    expect(c.error).toBe(true);
  });

  // A 200 we can't read is not proof the server stored the write (a proxy can
  // answer 200 with HTML or `{}`), so it is queued like a network failure.
  it.each([
    ["an unparseable body", { jsonError: true }],
    ["a JSON body with no log", { body: {} }],
  ])("keeps the write queued when a 200 has %s", async (_name, reply) => {
    const c = makeLogger();
    global.fetch = vi.fn().mockResolvedValue(res(reply));
    await c.finish();
    expect(c.status).toBe("done");
    expect(c.queued).toBe(true);
    expect(c.statusBeforeQueued).toBe("pending");
    expect(c.error).toBe(false);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ status: "done" });
  });

  it.each([
    ["an unparseable body", { jsonError: true }],
    ["a JSON body with no log", { body: {} }],
  ])("a later flush with a good 200 settles a finish that got %s", async (_name, reply) => {
    vi.useFakeTimers();
    const c = makeLogger();
    global.fetch = vi.fn().mockResolvedValue(res(reply));
    await c.finish();
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: logBody("done", { logged: 2, prescribed: 4 }) }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("2 of 4 sets logged");
  });

  it("takes the status back off when a 200 can't be read and storage is full", async () => {
    const c = makeLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    global.fetch = vi.fn().mockResolvedValue(res({ jsonError: true }));
    await c.finish();
    expect(c.status).toBe("pending"); // nothing holds the write: the button returns
    expect(c.error).toBe(true);
  });

  it("reflects the server's log and progress on success", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: logBody("done", { logged: 4, prescribed: 4 }) }),
    );
    await c.finish();
    expect(c.status).toBe("done");
    expect(c.saved).toBe(true);
    expect(c.error).toBe(false);
    expect(c.statusBeforeQueued).toBe("");
    expect(c.progressLabel).toBe("4 of 4 sets logged");
  });

  it("keeps the count when the response carries no (or bad) progress", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    c.applyProgress({ logged: 2, prescribed: 4 });
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, log: { status: "done" } } }),
    );
    await c.finish();
    expect(c.progressLabel).toBe("2 of 4 sets logged");
  });

  it("is a no-op while a save is already in flight", async () => {
    const c = makeLogger({ saving: true });
    global.fetch = vi.fn();
    await c.finish();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

// Issue #451: finishing the coach's own session can auto-advance the guided tour
// server-side, but the log POST is a fetch (no reload), so the mounted
// meso_tour.js driver can't see it. The nudge keys off the log status the
// *server returned*: a done log fires the `meso:tour-refresh` document event; a
// pending reply, an offline queue, or an outright failure stays silent.
describe("finish → tour refresh nudge (#451)", () => {
  it("dispatches meso:tour-refresh after a completed log", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    await c.finish();
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not dispatch when the server answers pending", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    await c.finish();
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).not.toHaveBeenCalled();
  });

  it("does not dispatch when the request fails (HTTP error)", async () => {
    const c = makeLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    await c.finish();
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).not.toHaveBeenCalled();
  });

  it("does not dispatch when the write is queued offline", async () => {
    const c = makeLogger();
    const handler = vi.fn();
    document.addEventListener("meso:tour-refresh", handler);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.finish();
    document.removeEventListener("meso:tour-refresh", handler);
    expect(handler).not.toHaveBeenCalled();
  });
});

describe("flushQueue", () => {
  it("replays a queued save and clears it on success", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    c.enqueue({ status: "done" });
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.queued).toBe(false);
    expect(c.status).toBe("done");
  });

  // A queue written by pre-deploy JS carries the old body, `sets` and all. It is
  // replayed verbatim (the server ignores `sets` with a 200) and the reply's
  // status and progress land as for any other.
  it("replays a pre-deploy queued entry verbatim and applies the reply", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    const body = {
      status: "done",
      sets: [{ prescription: 1, set_number: 1, reps: "5", load: "225", rpe: "", id: 11 }],
    };
    c.writeQueue([{ url: LOG_URL, body }]);
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: logBody("done", { logged: 1, prescribed: 3 }) }),
    );
    await c.flushQueue();
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual(body);
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("1 of 3 sets logged");
  });

  it("keeps the item queued when still offline", async () => {
    const c = makeLogger();
    c.enqueue({ status: "pending" });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
  });

  it("keeps the item queued when bounced to login (redirect)", async () => {
    const c = makeLogger();
    c.enqueue({ status: "pending" });
    global.fetch = vi.fn().mockResolvedValue(res({ redirected: true }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
  });

  // #570 round 3: a refusal won't change on retry, so it ends the entry rather
  // than being re-POSTed on every `online` event behind a "will sync" footer.
  it("drops this session's log on a refusal", async () => {
    const c = makeLogger();
    c.enqueue({ status: "done" });
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, status: 400, body: { ok: false, error: "nope" } }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0); // never retried again
    expect(c.error).toBe(true);
    expect(c.queued).toBe(false);
    expect(c.saved).toBe(false); // a refusal outranks the tick
  });

  it("keeps this session's log queued for a retryable status", async () => {
    const c = makeLogger();
    c.enqueue({ status: "done" });
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 503 }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1); // the server failed, not the write
    expect(c.error).toBe(false);
  });

  // The refusal split must not swallow the two statuses that mean "not postable
  // as this account right now": `csrf` is captured once at page load, so a
  // re-login elsewhere rotates the token and the next flush 403s. A queued log
  // is the only copy of a session finished offline.
  it.each([403, 409])("keeps this session's log queued on a %i", async (status) => {
    const c = makeLogger();
    c.enqueue({ status: "done" });
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
    expect(c.error).toBe(false);
  });

  // ...and when the entry IS dropped for good, the optimistic badge goes with
  // it: "Logged" with nothing on the server and nothing left to retry.
  it("takes the optimistic status back off when a flushed log is refused", async () => {
    const c = makeLogger();
    c.status = "pending";
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.finish();
    expect(c.status).toBe("done"); // optimistic, and legitimately queued
    expect(c.statusBeforeQueued).toBe("pending");

    global.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, status: 400, body: { ok: false, error: "nope" } }),
    );
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("pending"); // the badge no longer claims Logged
  });

  it("keeps ANOTHER session's refused log queued, with nothing here to show it", async () => {
    const c = makeLogger();
    c.writeQueue([
      c.stamp({ url: "/meso/api/me/session/99/log/", body: { status: "done" } }),
    ]);
    global.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, status: 400, body: { ok: false, error: "nope" } }),
    );
    await c.flushQueue();
    // Dropping it here would lose it silently: this page has nothing to report
    // the refusal on. Its own page will refuse it again.
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

describe("isPercentLift / suggestedLoad", () => {
  function pctLogger() {
    const c = createLogger();
    c.unit = "kg";
    c.exercises = [
      { id: 1, text: "3 x 5, 75%", e1rm: "120" },
      { id: 2, text: "3 x 10, 70", e1rm: "" },
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
    { id: 1, sub_lines: [{ line: 1, text: "RPE 8" }] },
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
      exercises: [{ id: 1, sub_lines: [{ line: 2, text: "" }] }],
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
        { id: 1, sub_lines: [{ line: 1, text: "225 x" }] },
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
        { id: 1, sub_lines: [{ line: 1, text: "225 x" }] },
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
        { id: 1, sub_lines: [{ line: 1, text: "225 x" }] },
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

  // A blur happens wherever the athlete is typing, so its celebration is
  // marked on the line that earned it; UAT found a page-top card firing
  // off-screen every time.

  it("marks the line that earned the record", async () => {
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

  // -- #571: the server now answers 503 (not 200) when it can't confirm a
  // write actually landed, precisely so this client behaviour kicks in.
  // `isRetryableStatus` already treats any `>= 500` as outcome "kept" — the
  // server failed, not the write — so a 503 must leave the line queued for
  // the next retry exactly like an ordinary 500 does. This pins that
  // existing behaviour, which #571's server fix now depends on.
  it("a 503 (server couldn't confirm the save) leaves the cell queued for retry", async () => {
    const c = cellLogger();
    const entry = c.exercises[0].sub_lines[0];
    entry.savedText = "RPE 7"; // this line was already saved once
    entry.text = "RPE 8"; // then edited, so the blur has something to send
    global.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, status: 503, body: { ok: false, error: "nope" } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.readQueue().map((i) => i.body.text)).toEqual(["RPE 8"]);
    expect(entry.queued).toBe(true);
    expect(entry.savedText).toBeUndefined(); // so the next blur reposts it
  });

  // -- #572: `warn_reason` tells a cross-day-move tint ("elsewhere" — the
  // performance is already logged, on the day the coach dragged this
  // exercise off) apart from every other warn cause (a repost is the
  // repair). `_lineNeedsSending`'s dirty check treats them differently even
  // when the text hasn't changed at all.

  it("does not repost an unchanged line whose warn_reason is 'elsewhere'", async () => {
    const c = cellLogger({
      exercises: [
        {
          id: 1,
          sub_lines: [
            {
              line: 1,
              text: "225 x 5",
              savedText: "225 x 5",
              warn: true,
              warn_reason: "elsewhere",
            },
          ],
        },
      ],
    });
    global.fetch = vi.fn();
    const outcome = await c.saveCell(c.exercises[0], 1);
    expect(outcome).toBe("skipped");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it.each(["skipped", "unlogged", "", undefined])(
    "still reposts an unchanged warned line whose reason is %j",
    async (reason) => {
      // "" is what an older server sends mid rolling deploy — pinned
      // alongside the real reasons because it must keep TODAY's behavior
      // (repost), not the new "elsewhere" suppression. `undefined` is a line
      // never hydrated with the key at all — e.g. one `addLine` just
      // appended (`{ line, text: "", savedText: "" }`, no `warn`/`warn_reason`
      // at all) — and `_lineNeedsSending`'s `warn_reason !== "elsewhere"`
      // check is exactly as true for `undefined` as for any other non-
      // "elsewhere" value, so it belongs in this same list, not a separate
      // one.
      const c = cellLogger({
        exercises: [
          {
            id: 1,
            sub_lines: [
              {
                line: 1,
                text: "225 x 5",
                savedText: "225 x 5",
                warn: true,
                warn_reason: reason,
              },
            ],
          },
        ],
      });
      global.fetch = vi.fn().mockResolvedValue(
        res({
          body: {
            ok: true,
            cell: { id: 5, line: 1, text: "225 x 5", warn: true, warn_reason: reason },
          },
        }),
      );
      const outcome = await c.saveCell(c.exercises[0], 1);
      expect(outcome).toBe("saved");
      expect(global.fetch).toHaveBeenCalledTimes(1);
    },
  );

  it("copies the response's warn_reason onto the entry", async () => {
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "225 x 5" }] },
      ],
    });
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          ok: true,
          cell: { id: 5, line: 1, text: "225 x 5", warn: true, warn_reason: "unlogged" },
        },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.exercises[0].sub_lines[0].warn_reason).toBe("unlogged");
  });

  it("clears warn_reason to \"\" when the response omits it (an older server)", async () => {
    const c = cellLogger({
      exercises: [
        {
          id: 1,
          sub_lines: [{ line: 1, text: "225 x 5", warn_reason: "unlogged" }],
        },
      ],
    });
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { id: 5, line: 1, text: "225 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.exercises[0].sub_lines[0].warn_reason).toBe("");
  });
});

describe("addLine", () => {
  it("appends an empty sub-line and stops at MAX_CELL_LINE", () => {
    const c = cellLogger({
      exercises: [{ id: 1, sub_lines: [] }],
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
      exercises: [{ id: 1, sub_lines: [{ line: 2, text: "x" }] }],
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
      exercises: [{ id: 1, sub_lines: [{ line: 20, text: "x" }] }],
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
          },
          {
            id: 8,
            text: "3 x 10",
            one_rm: "",
            one_rm_source: "",
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
    // button beneath three labelled set inputs (the retired Set rows), which made the freeform path
    // invisible. (No pad_lines here, so the floor of one applies.)
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
            pad_lines: 3,
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

  it.each([
    [0, 1],
    [-4, 1],
    [1, 1],
    [12, 12],
    [20, 20],
    [99, 20],
    [undefined, 1],
    ["3", 1],
    [2.5, 1],
  ])("clamps pad_lines %j to %i empty lines (1..20)", (padLines, count) => {
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [{ id: 7, text: "3 x 10", pad_lines: padLines }],
      }) +
      "</script>";
    const c = createLogger();
    c.init();
    const lines = c.exercises[0].sub_lines;
    expect(lines).toHaveLength(count);
    expect(lines.map((l) => l.line)).toEqual(Array.from({ length: count }, (_, i) => i + 1));
    expect(lines.every((l) => l.text === "")).toBe(true);
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
            pad_lines: 3,
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
            pad_lines: 1,
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
// only `finish()`'s whole-session payload was ever queued when the network was
// down — a failed cell write showed "couldn't save" and nothing retried it,
// so a set typed offline was lost while the page still said "Saved ✓". These
// pin the contract the fix implements: a failed cell write gets its own
// `kind: "cell"` entry in the SAME `meso-log-queue` (latest text per line
// wins), `flushQueue` drains every cell before the log — one request at a
// time, stopping at the first failure — `init()` folds an already-queued
// cell back onto its line before flushing, and `finish()` waits on the cell
// queue before it POSTs its own log.

const OTHER_SESSION_CELL_URL = "/meso/api/me/session/99/cell/";

describe("saveCell — latest-wins queue keying (#527)", () => {
  it("keeps exactly one cell entry per line, holding the latest text", async () => {
    const c = cellLogger({
      exercises: [
        { id: 1, sub_lines: [{ line: 1, text: "100 x 5" }] },
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
    // The log was queued FIRST (an earlier failed "Finish session"); a cell
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
        { id: 1, sub_lines: [{ line: 1, text: "bad", queued: true }] },
      ],
    });
    c.writeQueue([
      { kind: "cell", url: CELL_URL, body: { exercise_id: 1, line: 1, text: "bad" } },
      { url: LOG_URL, body: { status: "done", sets: [] } },
    ]);
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 400 });
      return res({ body: logBody("done") });
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
        { id: 1, sub_lines: [{ line: 1, text: "225 x", queued: true }] },
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
  function initLoggerWithHydratedLines(subLines, padLines) {
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
            pad_lines: padLines,
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
      2,
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
    const c = initLoggerWithHydratedLines([{ line: 1, text: "" }], 1);
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
    const c = initLoggerWithHydratedLines([{ line: 1, text: "RPE 8" }], 1);
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
    const c = initLoggerWithHydratedLines([{ line: 1, text: "100 x 5" }], 1);
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
        exercises: [{ id: 7, sub_lines: [] }],
      }) +
      "</script>";
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const c = createLogger();
    c.init();
    expect(c.exercises[0].sub_lines[0].text).toBe("");
    expect(c.exercises[0].sub_lines[0].queued).toBe(false);
  });

  it("sends the owner with a line write", async () => {
    const c = cellLogger({ owner: "athlete-a" });
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "RPE 8" } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(JSON.parse(global.fetch.mock.calls[0][1].body).owner).toBe("athlete-a");
  });

  it("keeps a line queued when the server says another account is signed in", async () => {
    // Athlete A's page, left open, flushes after B signed in on another tab.
    const c = cellLogger({ owner: "athlete-a", logUrl: LOG_URL });
    c.enqueueCell({ exercise_id: 1, line: 1, text: "100 x 5" });
    c.enqueue({ status: "done", sets: [] });
    c.exercises[0].sub_lines[0].queued = true;
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 409 }));
    await c.flushQueue();
    expect(global.fetch).toHaveBeenCalledTimes(1); // the pass stops there
    expect(c.readQueue()).toHaveLength(2);
    expect(c.exercises[0].sub_lines[0].queued).toBe(true);
    expect(c.exercises[0].sub_lines[0].saveError).toBeFalsy();
  });

  it("still flushes an entry queued before entries had an owner", async () => {
    const c = cellLogger({ owner: "athlete-a", logUrl: LOG_URL });
    c.writeQueue([{ url: LOG_URL, body: { status: "done", sets: [] } }]);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: logBody("done") }),
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
    // The line's blur got a 500 and waits in the queue; "Finish session" landed
    // (its flush retried the line, which failed again).
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 500 });
      return res({ body: logBody("done") });
    });
    await c.saveCell(c.exercises[0], 1);
    await c.finish();
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

  it("waits for a line blurred while Finish session is settling", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines.push({ line: 2, text: "110 x 5" });
    const calls = [];
    const release = {};
    global.fetch = vi.fn().mockImplementation(async (url, opts) => {
      if (url !== CELL_URL) {
        calls.push("log");
        return res({ body: logBody("done") });
      }
      const { line } = JSON.parse(opts.body);
      calls.push(line);
      await new Promise((r) => {
        release[line] = r;
      });
      return res({ body: { ok: true, cell: { warn: false } } });
    });
    c.saveCell(c.exercises[0], 1); // in flight when Finish session is pressed
    const saving = c.finish();
    await vi.waitFor(() => expect(calls).toEqual([1]));
    c.saveCell(c.exercises[0], 2); // blurred while finish() waits
    await vi.waitFor(() => expect(calls).toEqual([1, 2]));
    release[1]();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toEqual([1, 2]); // the log waits for line 2 to land
    release[2]();
    await saving;
    expect(calls).toEqual([1, 2, "log"]);
  });

  it("waits for a line blurred while Finish session flushes the queue", async () => {
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
        return res({ body: logBody("done") });
      }
      const { line } = JSON.parse(opts.body);
      calls.push(line);
      await new Promise((r) => {
        release[line] = r;
      });
      return res({ body: { ok: true, cell: { warn: false } } });
    });
    const saving = c.finish();
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
      res({ body: logBody("done") }),
    );
    await c.finish();
    expect(c.saved).toBe(true);
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1); // a blur that lands after the log
    expect(c.saved).toBe(false);
    expect(c.queued).toBe(true);
  });

  describe("storage too full to replace a line's older entry", () => {
    // A full store refuses any write that doesn't shrink the queue.
    function fillUp(c) {
      c.enqueueCell({ exercise_id: 1, line: 1, text: "100 x 5" });
      c.exercises[0].sub_lines[0].text = "110 x 5";
      const setItem = Storage.prototype.setItem;
      vi.spyOn(console, "error").mockImplementation(() => {});
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (k, v) {
        if (v.length >= (this.getItem(k) || "").length) {
          throw new DOMException("full", "QuotaExceededError");
        }
        return setItem.call(this, k, v);
      });
    }

    it("drops the older text so it can't replay over a save that landed", async () => {
      const c = cellLogger();
      fillUp(c);
      global.fetch = vi.fn().mockResolvedValue(
        res({ body: { ok: true, cell: { line: 1, text: "110 x 5" } } }),
      );
      await c.saveCell(c.exercises[0], 1);
      expect(c.readQueue()).toHaveLength(0);
    });

    it("says the new text couldn't save rather than passing off the old", async () => {
      const c = cellLogger();
      fillUp(c);
      global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
      await c.saveCell(c.exercises[0], 1);
      expect(c.readQueue()).toHaveLength(0);
      expect(c.exercises[0].sub_lines[0].queued).toBe(false);
      expect(c.exercises[0].sub_lines[0].saveError).toBe(true);
    });
  });

  it("shows another tab's replayed text on a line this tab never touched", async () => {
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.text = "100 x 5";
    line.savedText = "100 x 5";
    c.writeQueue([
      {
        kind: "cell",
        url: CELL_URL,
        body: { exercise_id: 1, line: 1, text: "110 x 5" },
        id: "queued-by-the-other-tab",
      },
    ]);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "110 x 5", warn: false } } }),
    );
    await c.flushQueue();
    expect(line.text).toBe("110 x 5");
    global.fetch.mockClear();
    await c.saveCell(c.exercises[0], 1); // tabbed through: nothing to send
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("leaves a line this tab is editing alone when a replay lands", async () => {
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.text = "120 x 5"; // typed here, not yet blurred
    line.savedText = "100 x 5";
    c.writeQueue([
      { kind: "cell", url: CELL_URL, body: { exercise_id: 1, line: 1, text: "110 x 5" } },
    ]);
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "110 x 5", warn: false } } }),
    );
    await c.flushQueue();
    expect(line.text).toBe("120 x 5");
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
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.finish();
    expect(c.queued).toBe(false);
    expect(c.error).toBe(true);
    // #570 round 2: storage refused the queue, so NOTHING holds this save --
    // not the server, not the outbox -- and the optimistic "done" set at the
    // top of finish() has to come back off, or the badge claims "Logged" over
    // a write that landed nowhere at all.
    expect(c.status).toBe("pending");
  });

  it("also takes the optimistic status back off when storage refuses a login-redirect queue", async () => {
    // Same failure, the OTHER call site `keepForLater` guards: a login
    // redirect means the write never reached the endpoint either, so a
    // storage refusal here is exactly as total a loss as the network-failure
    // case above.
    const c = makeLogger();
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    global.fetch = vi.fn().mockResolvedValue(res({ redirected: true }));
    await c.finish();
    expect(c.queued).toBe(false);
    expect(c.error).toBe(true);
    expect(c.status).toBe("pending");
  });
});

describe("a write that never answers counts as offline (#527)", () => {
  // Gym wifi can connect and then never answer, and fetch has no timeout.
  // "Finish session" waits for the lines, so an unbounded one would hold it on
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

  it("queues a stalled line and lets Finish session finish", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines[0].text = "100 x 5";
    global.fetch = vi.fn().mockImplementation((url, opts) => {
      if (url === CELL_URL) return hangs(url, opts);
      return Promise.resolve(res({ body: logBody("done") }));
    });
    c.saveCell(c.exercises[0], 1); // the blur, still waiting on an answer
    const saving = c.finish();
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
      res({ body: logBody("done") }),
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

describe("a replay, a stalled save, an unread response, a junk outbox (#527)", () => {
  function held() {
    const calls = [];
    const pending = [];
    const fetchMock = vi.fn().mockImplementation(
      (url, opts) =>
        new Promise((resolve) => {
          calls.push(JSON.parse(opts.body));
          pending.push(resolve);
        }),
    );
    const land = (body) => pending.shift()(res({ body }));
    return { calls, fetchMock, land };
  }

  it("keeps an edit back to the saved text made while its own replay runs", async () => {
    // The server has "225 x 5"; "225 x 6" was queued offline and is being
    // replayed on this page's load when the athlete changes it back.
    localStorage.setItem(
      "meso-log-queue",
      JSON.stringify([
        {
          kind: "cell",
          url: CELL_URL,
          body: { exercise_id: 7, line: 1, text: "225 x 6" },
          id: "queued-here-earlier",
        },
      ]),
    );
    document.body.innerHTML =
      '<script id="meso-log-data" type="application/json">' +
      JSON.stringify({
        log_url: LOG_URL,
        cell_url: CELL_URL,
        status: "pending",
        exercises: [
          { id: 7, sub_lines: [{ line: 1, text: "225 x 5" }] },
        ],
      }) +
      "</script>";
    const { calls, fetchMock, land } = held();
    global.fetch = fetchMock;
    const c = createLogger();
    c.init();
    const line = c.exercises[0].sub_lines[0];
    await vi.waitFor(() => expect(calls).toHaveLength(1)); // the replay
    line.text = "225 x 5";
    c.saveCell(c.exercises[0], 1);
    land({ ok: true, cell: { line: 1, text: "225 x 6", warn: false } });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].text).toBe("225 x 5");
    expect(line.text).toBe("225 x 5");
  });

  it("queues a line at the blur while an earlier save of it is in flight", async () => {
    // Closing the page before the earlier save times out must not lose it.
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    const { calls, fetchMock } = held();
    global.fetch = fetchMock;
    line.text = "225 x 5";
    c.saveCell(c.exercises[0], 1);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    line.text = "225 x 6";
    c.saveCell(c.exercises[0], 1); // waits behind the stalled one
    expect(c.readQueue().map((i) => i.body.text)).toEqual(["225 x 6"]);
  });

  it("sends a revert after a save whose response couldn't be read", async () => {
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.savedText = "225 x 5";
    line.text = "235 x 5";
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      redirected: false,
      json: async () => {
        throw new DOMException("The operation was aborted.", "AbortError");
      },
    });
    await c.saveCell(c.exercises[0], 1); // the server now has "235 x 5"
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "225 x 5", warn: false } } }),
    );
    line.text = "225 x 5";
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("sends a revert after a response that went stale in flight", async () => {
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    const { calls, fetchMock, land } = held();
    global.fetch = fetchMock;
    line.text = "225 x";
    const first = c.saveCell(c.exercises[0], 1);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    line.text = "225 x 50"; // typing on while it's in flight
    land({ ok: true, cell: { line: 1, text: "225 x", warn: true } });
    await first;
    line.text = "225 x"; // …and back
    c.saveCell(c.exercises[0], 1);
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    land({ ok: true, cell: { line: 1, text: "225 x", warn: true } });
    await vi.waitFor(() => expect(line.warn).toBe(true));
  });

  it("saves the session even when the outbox holds junk", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    localStorage.setItem(c.queueKey, JSON.stringify([null, 7, { url: "/x" }]));
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    await c.finish();
    expect(c.saving).toBe(false);
    expect(global.fetch).toHaveBeenCalledWith(LOG_URL, expect.anything());
  });

  it("supersedes an older queued log of the session instead of replaying it first", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    // An earlier write of this session is still queued (pre-deploy shape).
    c.enqueue({
      status: "pending",
      sets: [{ prescription: 1, set_number: 1, reps: "", load: "", rpe: "" }],
    });
    const bodies = [];
    global.fetch = vi.fn().mockImplementation(async (url, opts) => {
      bodies.push(JSON.parse(opts.body));
      return res({ body: logBody("done") });
    });
    await c.finish();
    expect(bodies).toEqual([{ status: "done" }]);
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("a newer blur, an unknown outcome, a slow Finish session (#527)", () => {
  function held() {
    const calls = [];
    const pending = [];
    const fetchMock = vi.fn().mockImplementation(
      (url, opts) =>
        new Promise((resolve) => {
          calls.push({ url, body: JSON.parse(opts.body) });
          pending.push(resolve);
        }),
    );
    const land = (body) => pending.shift()(res({ body }));
    return { calls, fetchMock, land };
  }

  it("sends the line as it is when a blur queued newer text mid-save", async () => {
    // "100 x 5" is in flight; a typo "100 x 55" is blurred (queued behind
    // it); the athlete deletes the extra 5 before the first save lands.
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.savedText = "";
    const { calls, fetchMock, land } = held();
    global.fetch = fetchMock;
    line.text = "100 x 5";
    c.saveCell(c.exercises[0], 1);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    line.text = "100 x 55";
    c.saveCell(c.exercises[0], 1);
    line.text = "100 x 5";
    land({ ok: true, cell: { line: 1, text: "100 x 5", warn: false } });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body.text).toBe("100 x 5");
    land({ ok: true, cell: { line: 1, text: "100 x 5", warn: false } });
    await vi.waitFor(() => expect(c.readQueue()).toHaveLength(0));
  });

  it("sends a revert after a write whose outcome was never known", async () => {
    // "225 x 6" may have landed (no answer); a later write was refused; the
    // athlete goes back to "225 x 5", which the server may no longer hold.
    const c = cellLogger();
    const line = c.exercises[0].sub_lines[0];
    line.savedText = "225 x 5";
    line.text = "225 x 6";
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(c.exercises[0], 1);
    line.text = "x".repeat(3000);
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 400 }));
    await c.saveCell(c.exercises[0], 1);
    expect(line.saveError).toBe(true);
    line.text = "225 x 5";
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { line: 1, text: "225 x 5", warn: false } } }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("keeps the session's log in the outbox while Finish session waits", async () => {
    // Leaving the page during a slow "Saving…" must not lose the finish.
    const c = cellLogger({ logUrl: LOG_URL });
    c.exercises[0].sub_lines[0].text = "RPE 8";
    const { calls, fetchMock, land } = held();
    global.fetch = fetchMock;
    c.saveCell(c.exercises[0], 1); // a line save that's slow to answer
    const saving = c.finish();
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    const log = c.readQueue().find((i) => i.url === LOG_URL);
    expect(log.body).toEqual({ status: "done" });
    land({ ok: true, cell: { line: 1, text: "RPE 8", warn: false } });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toEqual({ status: "done" });
    land(logBody("done"));
    await saving;
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("Finish session and an older log of the session (#527)", () => {
  // Each fetch waits until the test answers it by index.
  function controlled() {
    const calls = [];
    const fetchMock = vi.fn().mockImplementation(
      (url, opts) =>
        new Promise((resolve) => {
          calls.push({ url, body: JSON.parse(opts.body), resolve });
        }),
    );
    const answer = (i, reply) => calls[i].resolve(reply);
    const logReply = (body) => res({ body: logBody(body.status) });
    return { calls, fetchMock, answer, logReply };
  }

  it("sends its own log after an older log that is already out when it starts", async () => {
    vi.useFakeTimers();
    const c = makeLogger();
    // An older write was left queued (pre-deploy shape, `sets` and all).
    c.enqueue({
      status: "pending",
      sets: [{ prescription: 1, set_number: 1, reps: "", load: "", rpe: "" }],
    });
    const { calls, fetchMock, answer, logReply } = controlled();
    global.fetch = fetchMock;
    const flushing = c.flushQueue(); // signal's back: the older log goes out
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    const saving = c.finish(); // tapped while it's in flight
    answer(0, logReply(calls[0].body));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    answer(1, logReply(calls[1].body));
    await saving;
    await flushing;
    expect(calls[1].body).toEqual({ status: "done" });
    expect(c.status).toBe("done");
  });

  it("ignores an older log's reply body that lands after the tap", async () => {
    // Its headers arrived before Finish session was tapped; its body after.
    // Its "pending" must not take the badge off the finish in flight.
    vi.useFakeTimers();
    const c = makeLogger({ status: "pending" });
    c.enqueue({ status: "pending" });
    let bodyLands;
    const bodies = [];
    global.fetch = vi.fn().mockImplementation(async (url, opts) => {
      const body = JSON.parse(opts.body);
      bodies.push(body);
      const reply = logBody(body.status, { logged: bodies.length, prescribed: 9 });
      if (bodies.length > 1) return res({ body: reply });
      return {
        ok: true,
        status: 200,
        redirected: false,
        json: () =>
          new Promise((resolve) => {
            bodyLands = () => resolve(reply);
          }),
      };
    });
    const flushing = c.flushQueue();
    await vi.waitFor(() => expect(bodyLands).toBeTypeOf("function"));
    const saving = c.finish();
    expect(c.status).toBe("done");
    bodyLands();
    await saving;
    await flushing;
    expect(bodies[1]).toEqual({ status: "done" });
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("2 of 9 sets logged"); // finish's own reply
  });

  it("doesn't replay a log that finish() sent while a flush pass was busy", async () => {
    vi.useFakeTimers();
    const c = cellLogger({ logUrl: LOG_URL });
    // A line whose earlier write got a 5xx waits in the outbox.
    c.enqueueCell({ exercise_id: 1, line: 1, text: "RPE 8" });
    c.exercises[0].sub_lines[0].queued = true;
    const { calls, fetchMock, answer, logReply } = controlled();
    global.fetch = fetchMock;
    const saving = c.finish();
    await vi.waitFor(() => expect(calls).toHaveLength(1)); // the line, again
    answer(0, res({ ok: false, status: 500 })); // still failing: kept
    await vi.waitFor(() => expect(calls).toHaveLength(2)); // finish's log
    const flushing = c.flushQueue(); // an `online` event mid-POST
    await vi.waitFor(() => expect(calls).toHaveLength(3)); // the line
    answer(1, logReply(calls[1].body)); // finish's log lands
    await saving;
    answer(2, res({ ok: false, status: 500 }));
    // Either the pass ends, or it replays the log finish() already sent.
    await vi.waitFor(() =>
      expect(c._flushing === null || calls.length === 4).toBe(true),
    );
    const logPosts = calls.filter((call) => call.url === LOG_URL);
    if (calls[3]) answer(3, logReply(calls[3].body)); // let a replay finish
    await flushing;
    expect(logPosts).toHaveLength(1);
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

// #570 round 3: a refusal OUTRANKS a tick. A refused finish drops its own
// outbox entry, so nothing is retrying it, and the flush that gets us here may
// have landed a log queued by ANOTHER tab on this session (`flushedMine` means
// a log for this URL landed, not that this page's did). So it never claims
// saved while a refusal stands; `finish()` clears `error` at the top of the
// next real attempt, which is the moment the refusal stops being true.
describe("reportSaved and a standing refusal", () => {
  it("does not claim saved while a refusal stands", () => {
    const c = makeLogger();
    c.error = true;
    c.reportSaved();
    expect(c.saved).toBe(false);
    expect(c.error).toBe(true);
  });

  it("claims saved again once a fresh finish() clears the refusal", async () => {
    const c = makeLogger();
    c.error = true;
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    await c.finish();
    expect(c.error).toBe(false);
    expect(c.saved).toBe(true);
  });

  it("leaves a stale refusal in place while this page's log is still queued", () => {
    const c = makeLogger();
    c.error = true;
    c.enqueue({ status: "pending" }); // this session's own log, still in the outbox
    c.reportSaved();
    expect(c.queued).toBe(true);
    expect(c.saved).toBe(false);
    expect(c.error).toBe(true);
  });

  it("leaves a stale refusal in place while a line is still refused", () => {
    const c = makeLogger();
    c.error = true;
    c.exercises[0].sub_lines = [{ line: 1, text: "100 x 5", saveError: true }];
    c.reportSaved();
    expect(c.lineError).toBe(true);
    expect(c.saved).toBe(false);
    expect(c.error).toBe(true);
  });
});

describe("finish() — waits on the cell queue before its own log POST (#527)", () => {
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
      return res({ body: logBody("pending") });
    });
    await c.finish();
    expect(calls).toEqual([CELL_URL, LOG_URL]);
    expect(c.saved).toBe(true);
    expect(c.queued).toBe(false);
  });

  it("offline: the flush's cell attempt and finish's own log POST both fail, so it stays queued", async () => {
    const c = loggerWithAQueuedLine();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.finish();
    expect(c.queued).toBe(true);
    expect(c.saved).toBe(false);
  });

  it("a stuck cell (500) beats a successful log — finish() must not claim Saved", async () => {
    // This is the heart of #527: the log endpoint alone succeeding used to
    // be enough for finish() to say "Saved ✓" even though a line's own write
    // was still failing behind it.
    const c = loggerWithAQueuedLine();
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 500 });
      return res({ body: logBody("pending") });
    });
    await c.finish();
    expect(c.saved).toBe(false);
    expect(c.queued).toBe(true);
  });

  it("a line the server refused keeps the tick off and says so", async () => {
    // The log landed, but the refused line didn't: "Saved ✓" would claim both.
    const c = loggerWithAQueuedLine();
    global.fetch = vi.fn().mockImplementation(async (url) => {
      if (url === CELL_URL) return res({ ok: false, status: 400 });
      return res({ body: logBody("done") });
    });
    await c.finish();
    expect(c.exercises[0].sub_lines[0].saveError).toBe(true);
    expect(c.saved).toBe(false);
    expect(c.queued).toBe(false);
    expect(c.lineError).toBe(true);
  });
});

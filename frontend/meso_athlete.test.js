// Tests for the athlete session logger (app/store_project/static/js/meso_athlete.js).
//
// Focus: the logic that is fragile and effectively impossible to verify by hand
// — the offline write queue (stash on network failure, dedupe per session,
// replay on reconnect), the typed-line save path, and the finish/flush state
// machine. The athlete logs by typing lines; `finish()` only stamps the session
// done, and the server's `progress` count rides back on every response.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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

  it("ignores a progress payload older than the one already applied", () => {
    const c = makeLogger();
    c.applyProgress({ logged: 2, prescribed: 2, as_of: 200 });
    c.applyProgress({ logged: 1, prescribed: 2, as_of: 100 });
    expect(c.progressLabel).toBe("2 of 2 sets logged");
  });

  it("applies a payload with an equal as_of", () => {
    const c = makeLogger();
    c.applyProgress({ logged: 1, prescribed: 2, as_of: 100 });
    c.applyProgress({ logged: 2, prescribed: 2, as_of: 100 });
    expect(c.progressLabel).toBe("2 of 2 sets logged");
  });

  it("applies a payload with no as_of (an older server), and doesn't reset the stamp", () => {
    const c = makeLogger();
    c.applyProgress({ logged: 2, prescribed: 2, as_of: 200 });
    c.applyProgress({ logged: 1, prescribed: 3 });
    expect(c.progressLabel).toBe("1 of 3 sets logged");
    c.applyProgress({ logged: 0, prescribed: 3, as_of: 150 });
    expect(c.progressLabel).toBe("1 of 3 sets logged"); // 150 < 200 still stale
  });

  it("ignores a stale cell response after a first paint with a newer as_of", async () => {
    const c = makeLogger({ cellUrl: "/meso/api/me/session/42/cell/" });
    c.exercises = [{ id: 1, sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] }];
    c.applyProgress({ logged: 2, prescribed: 4, as_of: 150 }); // as init() does
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: { ok: true, cell: { warn: false }, progress: { logged: 1, prescribed: 4, as_of: 120 } },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.progressLabel).toBe("2 of 4 sets logged");
  });

  it("applies a cell response's progress to the label", async () => {
    const c = makeLogger({ cellUrl: "/meso/api/me/session/42/cell/" });
    c.exercises = [{ id: 1, sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] }];
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: { ok: true, cell: { warn: false }, progress: { logged: 1, prescribed: 4, as_of: 300 } },
      }),
    );
    await c.saveCell(c.exercises[0], 1);
    expect(c.progressLabel).toBe("1 of 4 sets logged");
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

  // A 200 we can't read is not proof the write landed, so the entry stays.
  it.each([
    ["an unparseable body", { jsonError: true }],
    ["a JSON body with no log", { body: {} }],
  ])("keeps this session's log queued when a flushed 200 has %s", async (_n, reply) => {
    const c = makeLogger();
    c.enqueue({ status: "done" });
    c.queued = true;
    global.fetch = vi.fn().mockResolvedValue(res(reply));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
    expect(c.status).toBe("pending");
  });

  it.each([
    ["an unparseable body", { jsonError: true }],
    ["a JSON body with no log", { body: {} }],
  ])("keeps ANOTHER session's log queued when its 200 has %s", async (_n, reply) => {
    const c = makeLogger();
    c.writeQueue([c.stamp({ url: "/meso/api/me/session/99/log/", body: { status: "done" } })]);
    global.fetch = vi.fn().mockResolvedValue(res(reply));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(1);
  });

  it("drops another session's log on a valid 200, leaving this page's state alone", async () => {
    const c = makeLogger();
    c.writeQueue([c.stamp({ url: "/meso/api/me/session/99/log/", body: { status: "done" } })]);
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    await c.flushQueue();
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("pending");
    expect(c.progressLabel).toBe("0 sets logged");
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

// ---------------------------------------------------------------------------
// Coach cues after the sets, free line numbers, placeholder, session note (#524)
// ---------------------------------------------------------------------------

const NOTE_CELL_URL = "/meso/api/me/session/42/cell/";

// Mount the page data and run init(), the way the browser does.
function initWith(data, { queue = null } = {}) {
  if (queue) localStorage.setItem("meso-log-queue", JSON.stringify(queue));
  document.body.innerHTML =
    '<span id="meso-csrf" data-token="tok"></span>' +
    '<script id="meso-log-data" type="application/json">' +
    JSON.stringify({
      log_url: LOG_URL,
      cell_url: NOTE_CELL_URL,
      status: "pending",
      exercises: [],
      ...data,
    }) +
    "</script>";
  const c = createLogger();
  c.init();
  return c;
}

const lineNumbers = (ex) => ex.sub_lines.map((l) => l.line);

describe("free line numbers around the coach's cues (#524)", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
  });

  it("a cue on line 1 with 3 pad lines gives the athlete 2, 3, 4", () => {
    const c = initWith({
      exercises: [{ id: 1, pad_lines: 3, coach_lines: [{ line: 1, text: "tempo 3-1-1" }] }],
    });
    expect(lineNumbers(c.exercises[0])).toEqual([2, 3, 4]);
  });

  it("cues on 1 and 3 with 2 pad lines give 2, 4", () => {
    const c = initWith({
      exercises: [
        {
          id: 1,
          pad_lines: 2,
          coach_lines: [
            { line: 1, text: "a" },
            { line: 3, text: "b" },
          ],
        },
      ],
    });
    expect(lineNumbers(c.exercises[0])).toEqual([2, 4]);
  });

  it("keeps the athlete's own lines and doesn't pad past them twice", () => {
    const c = initWith({
      exercises: [
        {
          id: 1,
          pad_lines: 2,
          coach_lines: [{ line: 1, text: "a" }],
          sub_lines: [{ line: 2, text: "100 x 5" }],
        },
      ],
    });
    expect(lineNumbers(c.exercises[0])).toEqual([2, 3]);
    expect(c.exercises[0].sub_lines[0].text).toBe("100 x 5");
  });

  it("coach_lines defaults to [] when the server sends none", () => {
    const c = initWith({ exercises: [{ id: 1, pad_lines: 2 }] });
    expect(c.exercises[0].coach_lines).toEqual([]);
    expect(lineNumbers(c.exercises[0])).toEqual([1, 2]);
  });

  it("addLine skips coach lines", () => {
    const c = initWith({
      exercises: [{ id: 1, pad_lines: 1, coach_lines: [{ line: 2, text: "c" }] }],
    });
    const ex = c.exercises[0];
    expect(lineNumbers(ex)).toEqual([1]);
    expect(c.nextFreeLine(ex)).toBe(3);
    c.addLine(ex);
    expect(lineNumbers(ex)).toEqual([1, 3]);
  });

  it("addLine respects the cap when the top numbers are coach lines", () => {
    const c = initWith({
      exercises: [
        {
          id: 1,
          pad_lines: 1,
          sub_lines: [{ line: 19, text: "x" }],
          coach_lines: [{ line: 20, text: "c" }],
        },
      ],
    });
    const ex = c.exercises[0];
    expect(c.nextFreeLine(ex)).toBeNull();
    const before = ex.sub_lines.length;
    c.addLine(ex);
    expect(ex.sub_lines).toHaveLength(before);
  });
});

describe("linePlaceholder (#524)", () => {
  const generic = "225 x 5, RPE 8 — or a note";

  it("the server's placeholder wins", () => {
    const c = makeLogger();
    expect(c.linePlaceholder({ placeholder: "225 x 5", placeholder_reps: "5", text: "5 x 5 @ 70%", one_rm: "200" })).toBe("225 x 5");
  });

  it("a %1RM lift with a suggested load and reps builds one", () => {
    const c = makeLogger();
    const ex = { placeholder: "", placeholder_reps: "5", text: "3 x 5 @ 67.5%", one_rm: "200", e1rm: "" };
    expect(c.linePlaceholder(ex)).toBe("135 x 5");
  });

  it("falls back to the generic hint otherwise", () => {
    const c = makeLogger();
    expect(c.linePlaceholder({ placeholder: "", placeholder_reps: "5", text: "3 x 5" })).toBe(generic); // not a %1RM lift
    expect(c.linePlaceholder({ placeholder: "", placeholder_reps: "", text: "3 x 5 @ 70%", one_rm: "200" })).toBe(generic); // no reps
    expect(c.linePlaceholder({ placeholder: "", placeholder_reps: "5", text: "3 x 5 @ 70%", one_rm: "", e1rm: "" })).toBe(generic); // no load
    expect(c.linePlaceholder({})).toBe(generic);
  });
});

describe("a coach-line refusal (422) drops the entry (#524)", () => {
  it("is permanent: entry dropped, line shows couldn't save, no retry", async () => {
    const c = makeLogger({ cellUrl: NOTE_CELL_URL });
    const ex = { id: 1, sub_lines: [{ line: 1, text: "mine", savedText: "" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockResolvedValue(
      res({ ok: false, status: 422, body: { ok: false, error: "coach line", code: "coach_line" } }),
    );
    expect(await c.saveCell(ex, 1)).toBe("rejected");
    expect(ex.sub_lines[0].saveError).toBe(true);
    expect(ex.sub_lines[0].queued).toBe(false);
    expect(c.readQueue()).toHaveLength(0);
    await c.flushQueue();
    expect(global.fetch).toHaveBeenCalledTimes(1); // nothing retried it
  });
});

describe("session note (#524)", () => {
  function noteLogger(over = {}) {
    return makeLogger({ notes: "", _notesSavedText: "", ...over });
  }
  const noteCalls = () =>
    global.fetch.mock.calls.filter(([u]) => u === LOG_URL).map(([, o]) => JSON.parse(o.body));

  it("initialises from the server payload", () => {
    const c = initWith({ notes: "sore knee", notes_max: 500 });
    expect(c.notes).toBe("sore knee");
    expect(c.notesMax).toBe(500);
    expect(initWith({}).notesMax).toBe(2000);
  });

  it("input writes ahead to the outbox before any POST", () => {
    vi.useFakeTimers();
    global.fetch = vi.fn(() => new Promise(() => {}));
    const c = noteLogger();
    c.notes = "felt heavy";
    c.noteInput();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0]).toMatchObject({ url: LOG_URL, body: { notes: "felt heavy" } });
    expect(c.noteStatus).toBe(""); // not "offline" just for typing
  });

  it("debounces: one POST with the latest text", async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const c = noteLogger();
    for (const t of ["a", "ab", "abc"]) {
      c.notes = t;
      c.noteInput();
      await vi.advanceTimersByTimeAsync(300);
    }
    expect(global.fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(700);
    expect(noteCalls()).toEqual([{ notes: "abc" }]);
    expect(c.noteStatus).toBe("saved");
    expect(c.readQueue()).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(3000);
    expect(c.noteStatus).toBe("");
  });

  it("blur posts immediately and clears the timer", async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const c = noteLogger();
    c.notes = "x";
    c.noteInput();
    await c.noteBlur();
    expect(noteCalls()).toEqual([{ notes: "x" }]);
    await vi.advanceTimersByTimeAsync(2000);
    expect(noteCalls()).toHaveLength(1);
  });

  it("a blur with nothing changed posts nothing", async () => {
    global.fetch = vi.fn();
    const c = noteLogger({ notes: "same", _notesSavedText: "same" });
    expect(await c.noteBlur()).toBe("skipped");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("never changes status: pending stays pending, done stays done", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    const c = noteLogger();
    c.notes = "n";
    await c.noteBlur();
    expect(c.status).toBe("pending"); // even if the reply says done
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    c.status = "done";
    c.notes = "n2";
    await c.noteBlur();
    expect(c.status).toBe("done");
  });

  it("applies the reply's progress", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending", { logged: 2, prescribed: 5 }) }));
    const c = noteLogger();
    c.notes = "n";
    await c.noteBlur();
    expect(c.progressLabel).toBe("2 of 5 sets logged");
  });

  it("offline: stays queued, then flushQueue delivers {notes} to the log url", async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const c = noteLogger();
    c.notes = "offline note";
    c.noteInput();
    expect(await c.noteBlur()).toBe("offline");
    expect(c.noteStatus).toBe("queued");
    expect(c.readQueue()).toHaveLength(1);

    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    await c.flushQueue();
    expect(noteCalls()).toEqual([{ notes: "offline note" }]);
    expect(c.readQueue()).toHaveLength(0);
    expect(c.noteStatus).toBe("saved");
    expect(c.status).toBe("pending");
  });

  it.each([
    ["a retryable 503", { ok: false, status: 503 }],
    ["a login redirect", { redirected: true }],
    ["a CSRF 403", { ok: false, status: 403 }],
    ["an unreadable 200", { jsonError: true }],
  ])("%s keeps the note queued", async (_n, r) => {
    global.fetch = vi.fn().mockResolvedValue(res(r));
    const c = noteLogger();
    c.notes = "keep me";
    c.noteInput();
    await c.noteBlur();
    expect(c.noteStatus).toBe("queued");
    expect(c.readQueue()[0].body).toEqual({ notes: "keep me" });
  });

  it("a stale tab replays another tab's queued note instead of overwriting it with its own blank", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const a = noteLogger(); // tab A types, then goes away
    a.notes = "knee pain";
    a.noteInput();
    const b = noteLogger(); // tab B loaded earlier with a blank box and never typed
    expect(b.notes).toBe("");
    await b.flushQueue();
    expect(noteCalls()).toEqual([{ notes: "knee pain" }]);
    expect(b.notes).toBe("knee pain");
  });

  it("text typed in THIS tab outranks a note queued by another", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const a = noteLogger();
    a.notes = "from A";
    a.noteInput();
    const b = noteLogger();
    b.notes = "from B";
    b.noteInput();
    await b.noteBlur();
    expect(noteCalls()).toEqual([{ notes: "from B" }]);
  });

  it("a 400 drops the entry and shows an error, with no retry", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 400, body: { ok: false } }));
    const c = noteLogger();
    c.notes = "too long";
    c.noteInput();
    expect(await c.noteBlur()).toBe("rejected");
    expect(c.noteStatus).toBe("error");
    expect(c.readQueue()).toHaveLength(0);
    await c.flushQueue();
    expect(noteCalls()).toHaveLength(1);
  });

  it("text typed while the POST is in flight survives it and is sent next", async () => {
    let release;
    global.fetch = vi
      .fn()
      .mockImplementationOnce(() => new Promise((r) => (release = r)))
      .mockResolvedValue(res({ body: logBody("pending") }));
    const c = noteLogger();
    c.notes = "one";
    c.noteInput();
    const first = c.noteBlur();
    await vi.waitFor(() => expect(release).toBeDefined());
    c.notes = "one two";
    c.noteInput(); // write-ahead replaces the queued "one"
    release(res({ body: logBody("pending") }));
    await first;
    expect(c.readQueue()[0].body).toEqual({ notes: "one two" }); // not dropped as "sent"
    await c.noteBlur();
    expect(noteCalls()).toEqual([{ notes: "one" }, { notes: "one two" }]);
    expect(c.readQueue()).toHaveLength(0);
  });

  it("reload with a queued note shows its text, flags it queued, and flushes it", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const c = initWith(
      { notes: "old server text" },
      { queue: [{ id: "n1", url: LOG_URL, body: { notes: "typed offline" } }] },
    );
    expect(c.notes).toBe("typed offline");
    expect(c.noteStatus).toBe("queued");
    expect(c._ownEntries.n1).toBe(true);
    await c.flushQueue();
    expect(noteCalls()).toEqual([{ notes: "typed offline" }]);
    expect(c.readQueue()).toHaveLength(0);
    expect(c.noteStatus).toBe("saved");
  });

  it("is editable after the session is done (no status gating)", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    const c = noteLogger({ status: "done" });
    c.notes = "after the fact";
    c.noteInput();
    await c.noteBlur();
    expect(noteCalls()).toEqual([{ notes: "after the fact" }]);
    expect(c.status).toBe("done");
  });

  it("does not post for a page with no log url", async () => {
    global.fetch = vi.fn();
    const c = noteLogger({ logUrl: "" });
    expect(await c.saveNotes()).toBe("skipped");
  });
});

describe("outbox: a note and a Finish both survive each other (#524)", () => {
  const logCalls = () =>
    global.fetch.mock.calls.filter(([u]) => u === LOG_URL).map(([, o]) => JSON.parse(o.body));

  it("enqueue merges bodies: a note over a queued Finish keeps both", () => {
    const c = makeLogger();
    c.enqueue({ status: "done" });
    c.enqueueNotes("n");
    const q = c.readQueue();
    expect(q).toHaveLength(1);
    expect(q[0].body).toEqual({ status: "done", notes: "n" });
  });

  it("enqueue merges: a Finish over a queued note keeps both, latest value per key wins", () => {
    const c = makeLogger();
    c.enqueueNotes("first");
    c.enqueueNotes("second");
    c.enqueue({ status: "done" });
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ notes: "second", status: "done" });
  });

  it("does not merge another athlete's entry", () => {
    const c = makeLogger({ owner: "me" });
    c.writeQueue([{ id: "x", owner: "someone-else", url: LOG_URL, body: { notes: "theirs" } }]);
    c.enqueue({ status: "done" });
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ status: "done" });
  });

  it("settleLog removes only the delivered keys, and only at the value sent", () => {
    const c = makeLogger();
    c.enqueue({ status: "done", notes: "new" });
    c.settleLog({ notes: "old" }); // the note changed since: stays
    expect(c.readQueue()[0].body).toEqual({ status: "done", notes: "new" });
    c.settleLog({ notes: "new" });
    expect(c.readQueue()[0].body).toEqual({ status: "done" });
    c.settleLog({ status: "done" });
    expect(c.readQueue()).toHaveLength(0);
  });

  it("note delivered while a Finish is queued: the Finish entry survives", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    const c = makeLogger({ notes: "n", _notesSavedText: "" });
    c.status = "done";
    c.enqueue({ status: "done" });
    c.enqueueNotes("n");
    await c.noteBlur();
    expect(logCalls()).toEqual([{ notes: "n" }]);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ status: "done" });
  });

  it("Finish after an undelivered note: the note is posted first, then the Finish", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    const c = makeLogger({ notes: "", _notesSavedText: "" });
    c.notes = "pain left shoulder";
    c.noteInput(); // queued, debounce pending
    await c.finish();
    expect(logCalls()).toEqual([{ notes: "pain left shoulder" }, { status: "done" }]);
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("done");
    expect(c._noteTimer).toBeNull();
  });

  it("Finish while the note POST is in flight waits for it", async () => {
    let release;
    global.fetch = vi
      .fn()
      .mockImplementationOnce(() => new Promise((r) => (release = r)))
      .mockResolvedValue(res({ body: logBody("done") }));
    const c = makeLogger();
    c.notes = "n";
    c.noteInput();
    const note = c.noteBlur();
    const fin = c.finish();
    await vi.waitFor(() => expect(release).toBeDefined());
    expect(global.fetch).toHaveBeenCalledTimes(1);
    release(res({ body: logBody("pending") }));
    await note;
    await fin;
    expect(logCalls()).toEqual([{ notes: "n" }, { status: "done" }]);
    expect(c.readQueue()).toHaveLength(0);
  });

  it("Finish goes through even when the note can't be delivered; the note stays queued", async () => {
    global.fetch = vi.fn(async (_u, o) => {
      if (JSON.parse(o.body).notes !== undefined) return res({ ok: false, status: 503 });
      return res({ body: logBody("done") });
    });
    const c = makeLogger();
    c.notes = "n";
    c.noteInput();
    await c.finish();
    expect(c.status).toBe("done");
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ notes: "n" }); // Finish's settle left the note
  });

  it("both offline: one merged entry; flush delivers the note, then the Finish", async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const c = makeLogger();
    c.notes = "n";
    c.noteInput();
    await c.finish();
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ notes: "n", status: "done" });
    expect(c.status).toBe("done");

    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done") }));
    await c.flushQueue();
    expect(logCalls()).toEqual([{ notes: "n" }, { status: "done" }]);
    expect(c.readQueue()).toHaveLength(0);
    expect(c.status).toBe("done");
  });

  it("a notes-only replay never downgrades a page that shows done", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const c = makeLogger({ notes: "n", _notesSavedText: "" });
    c.status = "done";
    c.enqueue({ status: "done" }); // Finish still queued
    c.enqueueNotes("n");
    await c.flushQueue();
    // The note's reply said pending, but only the {status} replay may speak for status.
    expect(logCalls()[0]).toEqual({ notes: "n" });
    expect(c.status).toBe("pending"); // the status entry's own reply, as today
  });

  it("a notes-only entry replayed by flushLog leaves status alone", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("pending") }));
    const c = makeLogger({ notes: "n", _notesSavedText: "" });
    c.status = "done";
    c.enqueueNotes("n");
    await c.flushQueue();
    expect(c.status).toBe("done");
    expect(c.readQueue()).toHaveLength(0);
  });

  it("a legacy status-only entry still applies the reply's status", async () => {
    global.fetch = vi.fn().mockResolvedValue(res({ body: logBody("done", { logged: 1, prescribed: 1 }) }));
    const c = makeLogger();
    c.enqueue({ status: "done" });
    await c.flushQueue();
    expect(c.status).toBe("done");
    expect(c.progressLabel).toBe("1 of 1 set logged");
  });

  it("a note still being typed doesn't make the footer say 'will sync'", () => {
    vi.useFakeTimers();
    global.fetch = vi.fn(() => new Promise(() => {}));
    const c = makeLogger();
    c.notes = "typing";
    c.noteInput();
    expect(c.hasQueuedWrites()).toBe(false);
  });
});

describe("athlete_session.html (#524)", () => {
  const html = readFileSync(
    resolve(process.cwd(), "app/store_project/templates/meso/athlete_session.html"),
    "utf8",
  );

  it("the note textarea is bound to notes_max and not gated on status", () => {
    const tag = html.match(/<textarea[\s\S]*?<\/textarea>/)[0];
    expect(tag).toContain(':maxlength="notesMax"');
    expect(tag).not.toContain("status");
    expect(html.indexOf('data-testid="session-note"')).toBeLessThan(html.indexOf("meso-log-actions"));
    expect(html).toContain('for="meso-session-note"');
  });

  it("coach cues are escaped text, not inputs", () => {
    const block = html.slice(html.indexOf('data-testid="coach-cues"'), html.indexOf('data-testid="session-note"'));
    expect(block).toContain('x-text="c.text"');
    expect(block).not.toContain("<input");
    expect(block).not.toContain("x-html");
  });
});

// ---- #709: a coach logs on the same session (`new`, relocation, "logged by coach") ----

describe("#709 — `new` marks a line the client believes is empty", () => {
  function oneLine(entry) {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [entry] };
    c.exercises = [ex];
    return { c, ex };
  }
  const ok = (line, text) =>
    res({ body: { ok: true, cell: { id: 5, line, text, warn: false } } });
  const sentBody = (i = 0) => JSON.parse(global.fetch.mock.calls[i][1].body);

  it("sends new:true when the server holds nothing on the line", async () => {
    const { c, ex } = oneLine({ line: 1, text: "225 x 5", savedText: "" });
    global.fetch = vi.fn().mockResolvedValue(ok(1, "225 x 5"));
    await c.saveCell(ex, 1);
    expect(sentBody()).toEqual({ exercise_id: 1, line: 1, text: "225 x 5", new: true, token: expect.any(String) });
  });

  it("does not send it when savedText is unknown", async () => {
    const { c, ex } = oneLine({ line: 1, text: "225 x 5", savedText: undefined });
    global.fetch = vi.fn().mockResolvedValue(ok(1, "225 x 5"));
    await c.saveCell(ex, 1);
    expect("new" in sentBody()).toBe(false);
  });

  it("does not send it when the server already holds text there", async () => {
    const { c, ex } = oneLine({ line: 1, text: "230 x 5", savedText: "225 x 5" });
    global.fetch = vi.fn().mockResolvedValue(ok(1, "230 x 5"));
    await c.saveCell(ex, 1);
    expect("new" in sentBody()).toBe(false);
  });

  it("the write-ahead copy and the queued replay carry it", async () => {
    const { c, ex } = oneLine({ line: 1, text: "225 x 5", savedText: "" });
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const run = c.saveCell(ex, 1);
    expect(c.readQueue()[0].body.new).toBe(true); // written at the blur itself
    await run;
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ exercise_id: 1, line: 1, text: "225 x 5", new: true, token: expect.any(String) });
    // the replay sends the flag it was queued with
    global.fetch = vi.fn().mockResolvedValue(ok(1, "225 x 5"));
    await c.flushQueue();
    expect(sentBody()).toEqual({ exercise_id: 1, line: 1, text: "225 x 5", new: true, token: expect.any(String) });
  });

  it("a blur while an earlier save runs keeps the first save's token", async () => {
    const { c, ex } = oneLine({ line: 1, text: "225 x 5", savedText: "" });
    let release;
    global.fetch = vi.fn().mockImplementationOnce(
      () => new Promise((resolve) => { release = () => resolve(ok(1, "225 x 5")); }),
    );
    const first = c.saveCell(ex, 1);
    await new Promise((r) => setTimeout(r, 0)); // the request is out
    expect(sentBody().new).toBe(true);
    const firstToken = sentBody().token;
    ex.sub_lines[0].text = "230 x 5"; // a correction while the first save runs
    global.fetch = vi.fn().mockResolvedValue(ok(1, "230 x 5"));
    const second = c.saveCell(ex, 1);
    // the first save's token is still unconfirmed: the correction rides with it
    expect(c.readQueue()[0].body.new).toBe(true);
    expect(c.readQueue()[0].body.token).toBe(firstToken);
    release();
    await first;
    await second;
    expect("new" in sentBody()).toBe(false); // savedText is "225 x 5" by then
  });

  it("an old outbox entry with no flag replays exactly as it was", async () => {
    const { c, ex } = oneLine({ line: 1, text: "", savedText: "" });
    c.writeQueue([
      { kind: "cell", url: CELL_URL, id: "old", body: { exercise_id: 1, line: 1, text: "100 x 5" } },
    ]);
    global.fetch = vi.fn().mockResolvedValue(ok(1, "100 x 5"));
    await c.flushQueue();
    expect(sentBody()).toEqual({ exercise_id: 1, line: 1, text: "100 x 5" });
  });

  it("a 422 coach_line still drops the entry and says it couldn't save", async () => {
    const { c, ex } = oneLine({ line: 1, text: "mine", savedText: undefined });
    global.fetch = vi.fn().mockResolvedValue(
      res({
        ok: false,
        status: 422,
        body: {
          ok: false,
          code: "coach_line",
          error: "coach line",
          exercise_lines: { sub_lines: [], coach_lines: [{ line: 1, text: "cue" }] },
        },
      }),
    );
    expect(await c.saveCell(ex, 1)).toBe("rejected");
    expect(ex.sub_lines[0].saveError).toBe(true);
    expect(ex.sub_lines[0].text).toBe("mine");
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("#709 — a relocated write re-keys the stack", () => {
  const lines = (ex) => ex.sub_lines.map((l) => l.line);
  const relocated = (extra) =>
    res({
      body: {
        ok: true,
        cell: { id: 9, line: 2, text: "225 x 5", warn: false, warn_reason: "", entered_by_coach: false },
        relocated_from: 1,
        ...extra,
      },
    });

  function setup(subLines) {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: subLines };
    c.exercises = [ex];
    return { c, ex };
  }

  it("moves the text onto the new number and shows the coach's cue on the old one", async () => {
    const { c, ex } = setup([{ line: 1, text: "225 x 5", savedText: "" }, { line: 3, text: "", savedText: "" }]);
    global.fetch = vi.fn().mockResolvedValue(
      relocated({
        exercise_lines: {
          sub_lines: [{ line: 2, text: "225 x 5", warn: false, warn_reason: "", entered_by_coach: false }],
          coach_lines: [{ line: 1, text: "tempo 3-1-1" }],
        },
      }),
    );
    await c.saveCell(ex, 1);
    expect(lines(ex)).toEqual([2, 3]);
    expect(ex.sub_lines[0]).toMatchObject({ text: "225 x 5", savedText: "225 x 5", queued: false });
    expect(ex.coach_lines).toEqual([{ line: 1, text: "tempo 3-1-1" }]);
    expect(c.readQueue()).toHaveLength(0);
  });

  it("shows a coach-logged set on the old number, labelled", async () => {
    const { c, ex } = setup([{ line: 1, text: "225 x 5", savedText: "" }]);
    global.fetch = vi.fn().mockResolvedValue(
      relocated({
        exercise_lines: {
          sub_lines: [
            { line: 1, text: "200 x 5", warn: false, warn_reason: "", entered_by_coach: true },
            { line: 2, text: "225 x 5", warn: false, warn_reason: "", entered_by_coach: false },
          ],
          coach_lines: [],
        },
      }),
    );
    await c.saveCell(ex, 1);
    expect(lines(ex)).toEqual([1, 2]);
    expect(ex.sub_lines[0]).toMatchObject({ text: "200 x 5", savedText: "200 x 5", entered_by_coach: true });
    expect(ex.sub_lines[1]).toMatchObject({ text: "225 x 5", entered_by_coach: false });
  });

  it("a correction typed while the write was in flight stays, on the new number", async () => {
    const { c, ex } = setup([{ line: 1, text: "225 x 5", savedText: "" }]);
    let release;
    global.fetch = vi.fn().mockImplementationOnce(
      () => new Promise((resolve) => { release = () => resolve(relocated({
        exercise_lines: {
          sub_lines: [{ line: 2, text: "225 x 5", warn: false, warn_reason: "", entered_by_coach: false }],
          coach_lines: [{ line: 1, text: "cue" }],
        },
      })); }),
    );
    const run = c.saveCell(ex, 1);
    await new Promise((r) => setTimeout(r, 0)); // the request is out
    ex.sub_lines[0].text = "230 x 5";
    release();
    await run;
    expect(lines(ex)).toEqual([2]);
    expect(ex.sub_lines[0]).toMatchObject({ text: "230 x 5", savedText: "225 x 5" });
  });

  it("an athlete edit that claims a coach's line turns the label off", async () => {
    const { c, ex } = setup([{ line: 1, text: "200 x 5", savedText: "200 x 5", entered_by_coach: true }]);
    ex.sub_lines[0].text = "205 x 5";
    global.fetch = vi.fn().mockResolvedValue(
      res({ body: { ok: true, cell: { id: 5, line: 1, text: "205 x 5", entered_by_coach: false } } }),
    );
    await c.saveCell(ex, 1);
    expect(ex.sub_lines[0].entered_by_coach).toBe(false);
  });

  it("keeps entered_by_coach from the page payload", () => {
    const c = initWith({
      exercises: [{ id: 1, pad_lines: 1, sub_lines: [{ line: 1, text: "200 x 5", entered_by_coach: true }] }],
    });
    expect(c.exercises[0].sub_lines[0].entered_by_coach).toBe(true);
  });
});

describe("#709 — applyExerciseLines", () => {
  const lines = (ex) => ex.sub_lines.map((l) => l.line);
  function setup(subLines, coach = []) {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: coach, sub_lines: subLines };
    c.exercises = [ex];
    return { c, ex };
  }
  const srv = (line, text, extra = {}) => ({
    line, text, warn: false, warn_reason: "", entered_by_coach: false, ...extra,
  });

  it("updates a clean entry from the server and clears its flags", () => {
    const { c, ex } = setup([{ line: 1, text: "old", savedText: "old", warn: true, saveError: true }]);
    c.applyExerciseLines(ex, { sub_lines: [srv(1, "new", { entered_by_coach: true })], coach_lines: [] });
    expect(ex.sub_lines).toHaveLength(1);
    expect(ex.sub_lines[0]).toMatchObject({
      text: "new", savedText: "new", warn: false, entered_by_coach: true, queued: false, saveError: false,
    });
  });

  it("drops a clean entry on a coach cue, keeps a blank pad line, sorts", () => {
    const { c, ex } = setup([
      { line: 3, text: "", savedText: "" },
      { line: 1, text: "x", savedText: "x" },
      { line: 2, text: "", savedText: "" },
    ]);
    c.applyExerciseLines(ex, { sub_lines: [], coach_lines: [{ line: 1, text: "cue" }] });
    expect(lines(ex)).toEqual([2, 3]);
    expect(ex.coach_lines).toEqual([{ line: 1, text: "cue" }]);
  });

  it("moves a dirty entry off a coach cue to the lowest free number", () => {
    const { c, ex } = setup([
      { line: 1, text: "typing", savedText: "" },
      { line: 2, text: "", savedText: "" },
    ]);
    c.applyExerciseLines(ex, { sub_lines: [], coach_lines: [{ line: 1, text: "cue" }] });
    expect(lines(ex)).toEqual([2, 3]);
    expect(ex.sub_lines.find((l) => l.line === 3)).toMatchObject({ text: "typing", savedText: "" });
  });

  it("keeps a dirty focused entry's text when the server has other text on its number", () => {
    const { c, ex } = setup([{ line: 1, text: "mine", savedText: "mine" }]);
    ex.sub_lines[0].text = "mine, edited"; // dirty
    c.applyExerciseLines(ex, { sub_lines: [srv(1, "the coach's set", { entered_by_coach: true })], coach_lines: [] });
    expect(lines(ex)).toEqual([1, 2]);
    expect(ex.sub_lines[0]).toMatchObject({ text: "the coach's set", entered_by_coach: true });
    expect(ex.sub_lines[1]).toMatchObject({ text: "mine, edited", savedText: "" });
  });

  it("a dirty entry whose number the server also holds with the same text stays put", () => {
    const { c, ex } = setup([{ line: 1, text: "225 x 5", savedText: undefined }]);
    c.applyExerciseLines(ex, { sub_lines: [srv(1, "225 x 5")], coach_lines: [] });
    expect(lines(ex)).toEqual([1]);
  });

  it("a line with a save running is left exactly where it is (its answer reconciles it)", () => {
    const { c, ex } = setup([{ line: 1, text: "a", savedText: "a" }]);
    c._lineSavesRunning["1:1"] = 1;
    c.applyExerciseLines(ex, { sub_lines: [srv(1, "b")], coach_lines: [] });
    expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([[1, "a"]]);
  });

  it("never leaves two entries on one number or an entry on a cue", () => {
    const { c, ex } = setup([
      { line: 1, text: "a", savedText: "" },
      { line: 2, text: "b", savedText: "" },
      { line: 3, text: "c", savedText: "c" },
    ]);
    c.applyExerciseLines(ex, {
      sub_lines: [srv(3, "z")],
      coach_lines: [{ line: 1, text: "cue" }, { line: 2, text: "cue2" }],
    });
    const nums = lines(ex);
    expect(new Set(nums).size).toBe(nums.length);
    expect(nums.every((n) => n !== 1 && n !== 2)).toBe(true);
    expect(nums).toEqual([3, 4, 5]);
    expect(ex.sub_lines.map((l) => l.text)).toEqual(["z", "a", "b"]);
  });

  it("moves the outbox entry this page owns with the dirty line, flagged new", () => {
    const { c, ex } = setup([{ line: 1, text: "225 x 5", savedText: "" }]);
    c.enqueueCell({ exercise_id: 1, line: 1, text: "225 x 5" });
    ex.sub_lines[0].queued = true;
    c.applyExerciseLines(ex, { sub_lines: [], coach_lines: [{ line: 1, text: "cue" }] });
    const q = c.readQueue();
    expect(q).toHaveLength(1);
    expect(q[0].body).toEqual({ exercise_id: 1, line: 2, text: "225 x 5", new: true, token: expect.any(String) });
    expect(c._ownEntries[q[0].id]).toBe(true);
    expect(ex.sub_lines[0]).toMatchObject({ line: 2, queued: true, savedText: "" });
  });
});

describe("#709 — a queued line restored where its number is now taken", () => {
  const queued = (line, text, extra = {}) => ({
    kind: "cell", id: "q1", url: NOTE_CELL_URL, body: { exercise_id: 1, line, text, ...extra },
  });

  it("shows it on the next free number, as queued, and re-targets its outbox entry", () => {
    const c = initWith(
      { exercises: [{ id: 1, pad_lines: 2, coach_lines: [{ line: 1, text: "cue" }] }] },
      { queue: [queued(1, "100 x 5")] },
    );
    const ex = c.exercises[0];
    expect(ex.sub_lines.map((l) => l.line)).toEqual([2, 3]);
    expect(ex.sub_lines[0]).toMatchObject({ text: "100 x 5", queued: true });
    expect(c.readQueue()[0].body).toEqual({ exercise_id: 1, line: 2, text: "100 x 5", new: true, token: expect.any(String) });
  });

  it("after the flush the text shows once, on the number the server used", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          ok: true,
          cell: { id: 9, line: 3, text: "100 x 5", warn: false, warn_reason: "", entered_by_coach: false },
          relocated_from: 2,
          exercise_lines: {
            sub_lines: [
              { line: 2, text: "90 x 5", warn: false, warn_reason: "", entered_by_coach: true },
              { line: 3, text: "100 x 5", warn: false, warn_reason: "", entered_by_coach: false },
            ],
            coach_lines: [{ line: 1, text: "cue" }],
          },
        },
      }),
    );
    const c = initWith(
      { exercises: [{ id: 1, pad_lines: 2, coach_lines: [{ line: 1, text: "cue" }] }] },
      { queue: [queued(2, "100 x 5", { new: true })] },
    );
    await c.flushQueue();
    const ex = c.exercises[0];
    expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([
      [2, "90 x 5"],
      [3, "100 x 5"],
    ]);
    expect(ex.sub_lines[0].entered_by_coach).toBe(true);
    expect(c.readQueue()).toHaveLength(0);
  });
});

describe("athlete_session.html — logged by coach (#709)", () => {
  const html = readFileSync(
    resolve(process.cwd(), "app/store_project/templates/meso/athlete_session.html"),
    "utf8",
  );

  it("labels a coach-entered sub-line without making the input read-only", () => {
    const tag = html.match(/<span[^>]*data-testid="sub-line-by-coach"[^>]*>[^<]*<\/span>/)[0];
    expect(tag).toContain('x-show="l.entered_by_coach"');
    expect(tag).toContain("logged by coach");
    const input = html.match(/<input[^>]*data-testid="sub-line-input"[\s\S]*?\/>/)[0];
    expect(input).not.toContain("readonly");
    expect(input).not.toContain(":disabled");
  });
});

describe("#709 — nothing typed is lost, stale text is blanked, keys are stable", () => {
  const srv = (line, text) => ({ line, text, warn: false, warn_reason: "", entered_by_coach: false });

  it("a displaced dirty line with no free number goes to ex.unplaced, outbox entry kept", () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const full = [];
    for (let n = 2; n <= 20; n += 1) full.push({ line: n, text: "s" + n, savedText: "s" + n });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "typed", savedText: "" }, ...full] };
    c.exercises = [ex];
    c.enqueueCell({ exercise_id: 1, line: 1, text: "typed" });
    ex.sub_lines[0].queued = true;
    c.applyExerciseLines(ex, {
      sub_lines: full.map((l) => srv(l.line, l.text)),
      coach_lines: [{ line: 1, text: "cue" }],
    });
    expect(ex.unplaced).toEqual([{ text: "typed", token: "" }]);
    expect(ex.sub_lines.some((l) => l.text === "typed")).toBe(false);
    expect(c.readQueue()).toHaveLength(1);
    expect(c.readQueue()[0].body).toEqual({ exercise_id: 1, line: 1, text: "typed" });
  });

  it("a restored queued line with no free number still shows, as unplaced", () => {
    const coach = [];
    for (let n = 1; n <= 20; n += 1) coach.push({ line: n, text: "cue" + n });
    const c = initWith(
      { exercises: [{ id: 1, pad_lines: 1, coach_lines: coach }] },
      { queue: [{ kind: "cell", id: "q1", url: NOTE_CELL_URL, body: { exercise_id: 1, line: 1, text: "100 x 5" } }] },
    );
    expect(c.exercises[0].unplaced).toEqual([{ text: "100 x 5", token: "" }]);
    expect(c.readQueue()).toHaveLength(1);
  });

  it("a clean line the server no longer has is blanked to a pad", () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = {
      id: 1,
      coach_lines: [],
      sub_lines: [{ line: 1, text: "225 x 5", savedText: "225 x 5", warn: true, warn_reason: "x", pr: "9 kg", entered_by_coach: true }],
    };
    c.applyExerciseLines(ex, { sub_lines: [], coach_lines: [] });
    expect(ex.sub_lines[0]).toMatchObject({
      line: 1, text: "", savedText: "", warn: false, warn_reason: "", pr: "", entered_by_coach: false,
    });
    expect(c._lineNeedsSending(ex.sub_lines[0], "", 1, 1)).toBe(false); // a blur re-posts nothing
  });

  it("a merge that renumbers an entry keeps the same object and its key", () => {
    const c = initWith({ exercises: [{ id: 1, pad_lines: 2 }] });
    const ex = c.exercises[0];
    ex.sub_lines[0].text = "typing";
    const mine = ex.sub_lines[0];
    const key = mine._k;
    expect(key).toBeTruthy();
    expect(new Set(ex.sub_lines.map((l) => l._k)).size).toBe(2);
    c.applyExerciseLines(ex, { sub_lines: [], coach_lines: [{ line: 1, text: "cue" }] });
    expect(ex.sub_lines.find((l) => l.text === "typing")).toBe(mine);
    expect(mine._k).toBe(key);
    expect(mine.line).not.toBe(1);
    c.addLine(ex);
    expect(new Set(ex.sub_lines.map((l) => l._k)).size).toBe(ex.sub_lines.length);
  });

  it("the template keys on _k and renders unplaced text with x-text", () => {
    const html = readFileSync(
      resolve(process.cwd(), "app/store_project/templates/meso/athlete_session.html"),
      "utf8",
    );
    expect(html).toContain('x-for="l in ex.sub_lines" :key="l._k"');
    const tag = html.match(/<div[^>]*data-testid="sub-line-unplaced"[^>]*>/)[0];
    expect(tag).toContain("x-text=");
    expect(tag).not.toContain("x-html");
  });
});

describe("#709 — restore never overwrites another queued line; replay token", () => {
  const q = (id, line, text, extra = {}) => ({
    kind: "cell", id, url: NOTE_CELL_URL, body: { exercise_id: 1, line, text, new: true, ...extra },
  });
  const ok = (line, text, extra = {}) =>
    res({ body: { ok: true, cell: { id: 5, line, text, warn: false }, ...extra } });
  const sent = (i = 0) => JSON.parse(global.fetch.mock.calls[i][1].body);

  it("a cue on line 1 over queued lines 1 and 2 keeps both writes, on distinct numbers", () => {
    const c = initWith(
      { exercises: [{ id: 1, pad_lines: 2, coach_lines: [{ line: 1, text: "cue" }] }] },
      { queue: [q("a", 1, "225 x 5"), q("b", 2, "230 x 5")] },
    );
    const ex = c.exercises[0];
    const byText = Object.fromEntries(ex.sub_lines.map((l) => [l.text, l.line]));
    expect(byText["230 x 5"]).toBe(2);
    expect(byText["225 x 5"]).toBe(3);
    const queue = c.readQueue();
    expect(queue.map((i) => [i.body.line, i.body.text]).sort()).toEqual([
      [2, "230 x 5"],
      [3, "225 x 5"],
    ]);
  });

  it("_retargetOutbox refuses a number another entry targets and deletes nothing", () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    c.writeQueue([q("a", 1, "x"), q("b", 2, "y")].map((i) => ({ ...i, url: CELL_URL })));
    expect(c._retargetOutbox("a", 2)).toBe("");
    expect(c.readQueue()).toHaveLength(2);
  });

  it("a merge's displaced line avoids numbers an outbox entry targets", () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "a", savedText: "" }] };
    c.enqueueCell({ exercise_id: 1, line: 1, text: "a", new: true });
    c.enqueueCell({ exercise_id: 1, line: 2, text: "other", new: true });
    ex.sub_lines[0].queued = true;
    c.applyExerciseLines(ex, { sub_lines: [], coach_lines: [{ line: 1, text: "cue" }] });
    expect(ex.sub_lines[0].line).toBe(3);
    expect(c.readQueue().map((i) => i.body.line).sort()).toEqual([2, 3]);
  });

  it("the same token rides the write-ahead, the post and a replay", async () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const run = c.saveCell(ex, 1);
    const ahead = c.readQueue()[0].body.token;
    expect(typeof ahead).toBe("string");
    await run;
    expect(sent().token).toBe(ahead);
    expect(c.readQueue()[0].body.token).toBe(ahead);
    global.fetch = vi.fn().mockResolvedValue(ok(1, "225 x 5"));
    await c.flushQueue();
    expect(sent().token).toBe(ahead);
    expect(ex.sub_lines[0].newToken).toBeUndefined(); // dropped once the server holds it
  });

  it("no token without new", async () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "b", savedText: "a" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockResolvedValue(ok(1, "b"));
    await c.saveCell(ex, 1);
    expect("token" in sent()).toBe(false);
    expect("new" in sent()).toBe(false);
  });

  it("an idempotent replay answered with newer text shows it once, from the server", async () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockResolvedValue(
      ok(2, "230 x 5", {
        relocated_from: 1,
        exercise_lines: {
          sub_lines: [{ line: 2, text: "230 x 5", warn: false, warn_reason: "", entered_by_coach: false }],
          coach_lines: [{ line: 1, text: "cue" }],
        },
      }),
    );
    await c.saveCell(ex, 1);
    expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([[2, "230 x 5"]]);
    expect(ex.sub_lines[0].savedText).toBe("230 x 5");
  });

  it("an idempotent 200 on the same line takes the text the server holds", async () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockResolvedValue(ok(1, "230 x 5"));
    await c.saveCell(ex, 1);
    expect(ex.sub_lines[0]).toMatchObject({ text: "230 x 5", savedText: "230 x 5" });
  });
});

describe("#709 — merge keeps an edit; an unconfirmed new line stays new", () => {
  const ok = (line, text) =>
    res({ body: { ok: true, cell: { id: 5, line, text, warn: false } } });
  const sent = (i = 0) => JSON.parse(global.fetch.mock.calls[i][1].body);

  it("a pending edit of a line the server still holds unchanged keeps its number and outbox entry", () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "100x5", savedText: "100x5" }] };
    c.exercises = [ex];
    ex.sub_lines[0].text = "105x5";
    c.enqueueCell({ exercise_id: 1, line: 1, text: "105x5" });
    ex.sub_lines[0].queued = true;
    c.applyExerciseLines(ex, {
      sub_lines: [
        { line: 1, text: "100x5", warn: false, warn_reason: "", entered_by_coach: false },
        { line: 2, text: "x", warn: false, warn_reason: "", entered_by_coach: true },
      ],
      coach_lines: [],
    });
    expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([[1, "105x5"], [2, "x"]]);
    expect(c.readQueue().map((i) => [i.body.line, i.body.text, i.body.new])).toEqual([[1, "105x5", undefined]]);
  });

  it("a dirty line with unknown savedText stays in place", () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "a", savedText: undefined }] };
    c.applyExerciseLines(ex, {
      sub_lines: [{ line: 1, text: "b", warn: false, warn_reason: "", entered_by_coach: false }],
      coach_lines: [],
    });
    expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([[1, "a"]]);
  });

  it("after a network failure an edit is still queued as new with the same token", async () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.saveCell(ex, 1);
    const token = c.readQueue()[0].body.token;
    expect(ex.sub_lines[0].savedText).toBeUndefined();
    ex.sub_lines[0].text = "230 x 5";
    const run = c.saveCell(ex, 1);
    expect(c.readQueue()[0].body).toEqual({ exercise_id: 1, line: 1, text: "230 x 5", new: true, token });
    await run;
    expect(sent(1)).toEqual({ exercise_id: 1, line: 1, text: "230 x 5", new: true, token });
    expect(c.readQueue()[0].body).toEqual({ exercise_id: 1, line: 1, text: "230 x 5", new: true, token });
  });

  it("a 200 clears the token, so the next edit is a plain edit", async () => {
    const c = makeLogger({ cellUrl: CELL_URL });
    const ex = { id: 1, coach_lines: [], sub_lines: [{ line: 1, text: "225 x 5", savedText: "" }] };
    c.exercises = [ex];
    global.fetch = vi.fn().mockResolvedValue(ok(1, "225 x 5"));
    await c.saveCell(ex, 1);
    expect(ex.sub_lines[0].newToken).toBeUndefined();
    ex.sub_lines[0].text = "230 x 5";
    global.fetch = vi.fn().mockResolvedValue(ok(1, "230 x 5"));
    await c.saveCell(ex, 1);
    expect("new" in sent()).toBe(false);
    expect("token" in sent()).toBe(false);
  });

  it("restore then flush keeps new and the token", async () => {
    global.fetch = vi.fn().mockResolvedValue(ok(1, "225 x 5"));
    const c = initWith(
      { exercises: [{ id: 1, pad_lines: 1 }] },
      { queue: [{ kind: "cell", id: "q1", url: NOTE_CELL_URL, body: { exercise_id: 1, line: 1, text: "225 x 5", new: true, token: "tok-1" } }] },
    );
    expect(c.exercises[0].sub_lines[0].newToken).toBe("tok-1");
    await c.flushQueue();
    expect(sent()).toEqual({ exercise_id: 1, line: 1, text: "225 x 5", new: true, token: "tok-1" });
  });
});

// ---- live session sync (#709 PR 2) ----
const SYNC_URL = "/meso/api/me/session/42/sync/";

// A poll answer: `exercises` are page-payload-shaped entries.
function syncBody(over = {}) {
  return {
    ok: true,
    changed: true,
    sync_v: 8,
    status: "pending",
    notes: "",
    progress: { logged: 0, prescribed: 4, as_of: 5 },
    exercises: [],
    ...over,
  };
}

// Route fetch by url: the sync poll, the cell write and the log write each get
// a handler returning a res() stub.
function routeFetch({ sync, cell, log } = {}) {
  const calls = { sync: [], cell: [], log: [] };
  global.fetch = vi.fn((url, opts) => {
    if (String(url).startsWith(SYNC_URL)) {
      calls.sync.push(String(url));
      return Promise.resolve(sync ? sync(String(url)) : res({ body: { ok: true, changed: false, sync_v: 5 } }));
    }
    if (url === NOTE_CELL_URL) {
      calls.cell.push(JSON.parse(opts.body));
      return Promise.resolve(cell ? cell(JSON.parse(opts.body)) : res({ body: {} }));
    }
    calls.log.push(JSON.parse(opts.body));
    return Promise.resolve(log ? log(JSON.parse(opts.body)) : res({ body: logBody() }));
  });
  return calls;
}

// Loggers whose window/document listeners outlive their test: stopped after
// each, so they don't answer the next test's focus events.
const syncPages = [];

function syncPage(data = {}) {
  const c = initWith({
    sync_v: 5,
    sync_url: SYNC_URL,
    exercises: [
      { id: 1, name: "Squat", pad_lines: 3 },
      { id: 2, name: "Press", pad_lines: 2 },
    ],
    ...data,
  });
  syncPages.push(c);
  return c;
}

const tick = (ms = 3000) => vi.advanceTimersByTimeAsync(ms);

function setVisibility(state) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
}

describe("live session sync (#709)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    for (const c of syncPages.splice(0)) c._pollStopped = true;
    delete document.visibilityState;
    vi.useRealTimers();
  });

  describe("polling", () => {
    it("polls the sync url with the page's stamp every 3s while active", async () => {
      const calls = routeFetch();
      syncPage();
      expect(calls.sync).toHaveLength(0);
      await tick(3000);
      expect(calls.sync).toEqual([SYNC_URL + "?v=5"]);
      await tick(3000);
      expect(calls.sync).toHaveLength(2);
    });

    it("backs off 6 / 12 / 30s once idle for two minutes", async () => {
      const calls = routeFetch();
      syncPage();
      await tick(121000);
      const active = calls.sync.length;
      expect(active).toBeGreaterThan(30);
      await tick(60000);
      const idle = calls.sync.length - active;
      expect(idle).toBeGreaterThan(0);
      expect(idle).toBeLessThanOrEqual(4);
    });

    it("a hidden page does not poll; becoming visible polls at once", async () => {
      const calls = routeFetch();
      setVisibility("hidden");
      syncPage();
      await tick(60000);
      expect(calls.sync).toHaveLength(0);
      setVisibility("visible");
      document.dispatchEvent(new Event("visibilitychange"));
      await tick(0);
      expect(calls.sync).toHaveLength(1);
    });

    it("an offline page does not poll; online triggers a poll", async () => {
      const calls = routeFetch();
      const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
      syncPage();
      await tick(60000);
      expect(calls.sync).toHaveLength(0);
      online.mockReturnValue(true);
      window.dispatchEvent(new Event("online"));
      await tick(0);
      expect(calls.sync).toHaveLength(1);
    });

    it("window focus polls at once", async () => {
      const calls = routeFetch();
      syncPage();
      window.dispatchEvent(new Event("focus"));
      await tick(0);
      expect(calls.sync).toHaveLength(1);
    });

    it("never has two polls in flight", async () => {
      let release;
      const calls = routeFetch({
        sync: () => new Promise((r) => (release = () => r(res({ body: { ok: true, changed: false, sync_v: 5 } })))),
      });
      syncPage();
      window.dispatchEvent(new Event("focus"));
      window.dispatchEvent(new Event("focus"));
      await tick(20000);
      expect(calls.sync).toHaveLength(1);
      release();
      await tick(3000);
      expect(calls.sync.length).toBeGreaterThan(1);
    });

    it("a 404 backs off to 60s and stops for good only after 10 in a row", async () => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const calls = routeFetch({ sync: () => res({ ok: false, status: 404 }) });
      syncPage();
      await tick(3000);
      expect(calls.sync).toHaveLength(1);
      await tick(30000);
      expect(calls.sync).toHaveLength(1);
      await tick(30000);
      expect(calls.sync).toHaveLength(2);
      await tick(60000 * 12);
      expect(calls.sync).toHaveLength(10);
      expect(warn).toHaveBeenCalledTimes(1);
    });

    it("a poll that finds the route again resets the 404 run", async () => {
      let n = 0;
      const calls = routeFetch({
        sync: () => {
          n += 1;
          return n === 1 || n === 3
            ? res({ ok: false, status: 404 })
            : res({ body: { ok: true, changed: false, sync_v: 5 } });
        },
      });
      const c = syncPage();
      await tick(3000);
      await tick(60000);
      await tick(3000);
      expect(c._poll404s).toBe(1);
      expect(calls.sync.length).toBeGreaterThanOrEqual(3);
    });

    it("a network error backs off silently and keeps polling", async () => {
      const err = vi.spyOn(console, "error").mockImplementation(() => {});
      let n = 0;
      const calls = routeFetch({
        sync: () => {
          n += 1;
          throw new TypeError("Failed to fetch");
        },
      });
      syncPage();
      await tick(40000);
      expect(n).toBeGreaterThan(1);
      expect(calls.sync.length).toBeLessThan(8);
      expect(err).not.toHaveBeenCalled();
    });

    it("sends the newest stamp after a change", async () => {
      const calls = routeFetch({ sync: () => res({ body: syncBody({ sync_v: 9 }) }) });
      syncPage();
      await tick(3000);
      await tick(3000);
      expect(calls.sync[1]).toBe(SYNC_URL + "?v=9");
    });

    it("does not poll without a stamp (an older server)", async () => {
      const calls = routeFetch();
      initWith({ sync_url: SYNC_URL, exercises: [{ id: 1, pad_lines: 1 }] });
      await tick(10000);
      expect(calls.sync).toHaveLength(0);
      syncPage();
      await tick(3000);
      expect(calls.sync).toHaveLength(1);
    });
  });

  describe("merge", () => {
    async function pollWith(c, body) {
      routeFetch({ sync: () => res({ body }) });
      window.dispatchEvent(new Event("focus"));
      await tick(0);
    }

    it("a stale poll leaves an exercise alone once a write answer is adopted", async () => {
      routeFetch({
        cell: (b) => res({ body: { ok: true, sync_v: 7, cell: { line: b.line, text: b.text } } }),
      });
      const c = syncPage();
      const ex = c.exercises[0];
      ex.sub_lines[0].text = "225 x 5";
      await c.saveCell(ex, 1);
      expect(ex.sub_lines[0].savedText).toBe("225 x 5");
      await pollWith(
        c,
        syncBody({
          sync_v: 6,
          exercises: [
            { id: 1, name: "Squat", sub_lines: [{ line: 1, text: "old" }] },
            { id: 2, name: "Press", sub_lines: [{ line: 1, text: "from coach", entered_by_coach: true }] },
          ],
        }),
      );
      expect(ex.sub_lines[0].text).toBe("225 x 5");
      // Another exercise, no write answer: merged.
      expect(c.exercises[1].sub_lines[0].text).toBe("from coach");
    });

    it("a 422 answer's stamp guards the exercise too", async () => {
      routeFetch({ cell: () => res({ ok: false, status: 422, body: { ok: false, sync_v: 9 } }) });
      const c = syncPage();
      const ex = c.exercises[0];
      ex.coach_lines = [{ line: 1, text: "cue" }];
      ex.sub_lines[0].text = "x";
      await c.saveCell(ex, 1);
      await pollWith(
        c,
        syncBody({
          sync_v: 8,
          exercises: [
            { id: 1, name: "Squat", coach_lines: [], sub_lines: [] },
            { id: 2, name: "Press", coach_lines: [{ line: 1, text: "other" }] },
          ],
        }),
      );
      expect(ex.coach_lines).toEqual([{ line: 1, text: "cue" }]);
      expect(c.exercises[1].coach_lines).toEqual([{ line: 1, text: "other" }]);
    });

    it("a new coach line appears; a clean line takes the server text and keeps its key", async () => {
      routeFetch();
      const c = syncPage();
      const ex = c.exercises[0];
      const key = ex.sub_lines[0]._k;
      await pollWith(
        c,
        syncBody({
          exercises: [
            {
              id: 1,
              name: "Squat",
              coach_lines: [{ line: 2, text: "tempo 3-1-1" }],
              sub_lines: [{ line: 1, text: "225 x 5", entered_by_coach: true }],
            },
            { id: 2, name: "Press" },
          ],
        }),
      );
      expect(ex.coach_lines).toEqual([{ line: 2, text: "tempo 3-1-1" }]);
      expect(ex.sub_lines[0]).toMatchObject({ line: 1, text: "225 x 5", savedText: "225 x 5", entered_by_coach: true });
      expect(ex.sub_lines[0]._k).toBe(key);
      expect(lineNumbers(ex)).toEqual([1, 3]);
    });

    it("a focused clean line takes new server text only if it differs from savedText", async () => {
      routeFetch();
      const c = syncPage();
      const ex = c.exercises[0];
      ex.sub_lines[0].text = "225 x 5";
      ex.sub_lines[0].savedText = "225 x 5";
      const same = { id: 1, name: "Squat", sub_lines: [{ line: 1, text: "225 x 5" }] };
      await pollWith(c, syncBody({ sync_v: 8, exercises: [same, { id: 2, name: "Press" }] }));
      expect(ex.sub_lines[0].text).toBe("225 x 5");
      const changed = { id: 1, name: "Squat", sub_lines: [{ line: 1, text: "230 x 5" }] };
      await pollWith(c, syncBody({ sync_v: 9, exercises: [changed, { id: 2, name: "Press" }] }));
      expect(ex.sub_lines[0].text).toBe("230 x 5");
      expect(ex.sub_lines[0].savedText).toBe("230 x 5");
    });

    it("a dirty line is untouched, and a line mid-save too", async () => {
      routeFetch();
      const c = syncPage();
      const ex = c.exercises[0];
      const dirty = ex.sub_lines[0];
      dirty.text = "typed";
      const running = ex.sub_lines[1];
      running.text = "saving";
      c._lineSavesRunning["1:2"] = 1;
      await pollWith(
        c,
        syncBody({
          exercises: [
            { id: 1, name: "Squat", coach_lines: [{ line: 4, text: "cue" }], sub_lines: [] },
            { id: 2, name: "Press" },
          ],
        }),
      );
      expect(ex.coach_lines).toEqual([{ line: 4, text: "cue" }]);
      expect(ex.sub_lines.find((l) => l._k === dirty._k)).toMatchObject({ line: 1, text: "typed" });
      expect(ex.sub_lines.find((l) => l._k === running._k)).toMatchObject({ line: 2, text: "saving" });
    });

    it("a queued line is untouched", async () => {
      routeFetch();
      const c = syncPage();
      const ex = c.exercises[0];
      ex.sub_lines[0].text = "offline text";
      ex.sub_lines[0].queued = true;
      await pollWith(
        c,
        syncBody({
          exercises: [
            { id: 1, name: "Squat", coach_lines: [{ line: 5, text: "cue" }], sub_lines: [] },
            { id: 2, name: "Press" },
          ],
        }),
      );
      expect(ex.sub_lines[0]).toMatchObject({ text: "offline text", queued: true });
      expect(ex.coach_lines).toEqual([{ line: 5, text: "cue" }]);
    });

    it("logged_readonly and the exercise's own fields are taken", async () => {
      routeFetch();
      const c = syncPage();
      await pollWith(
        c,
        syncBody({
          exercises: [
            { id: 1, name: "Squat", target: "5 x 5", text: "5 x 5 @ 80%", logged_readonly: [{ text: "from before" }] },
            { id: 2, name: "Press" },
          ],
        }),
      );
      expect(c.exercises[0].logged_readonly).toEqual([{ text: "from before" }]);
      expect(c.exercises[0].target).toBe("5 x 5");
      expect(c.exercises[0].text).toBe("5 x 5 @ 80%");
    });

    it("logged_as / logged_as_mixed are taken, updated and cleared by a poll (#714)", async () => {
      routeFetch();
      const c = syncPage();
      await pollWith(
        c,
        syncBody({
          exercises: [
            { id: 1, name: "Front Squat", logged_as: ["Back Squat"], logged_as_mixed: true },
            { id: 2, name: "Press" },
          ],
        }),
      );
      expect(c.exercises[0].logged_as).toEqual(["Back Squat"]);
      expect(c.exercises[0].logged_as_mixed).toBe(true);
      await pollWith(
        c,
        syncBody({
          exercises: [
            { id: 1, name: "Front Squat", logged_as: [], logged_as_mixed: false },
            { id: 2, name: "Press" },
          ],
        }),
      );
      expect(c.exercises[0].logged_as).toEqual([]);
      expect(c.exercises[0].logged_as_mixed).toBe(false);
    });

    it("a new exercise is initialized with pads, keys and unplaced", async () => {
      routeFetch();
      const c = syncPage();
      await pollWith(
        c,
        syncBody({
          exercises: [
            { id: 1, name: "Squat" },
            { id: 2, name: "Press" },
            { id: 3, name: "Row", pad_lines: 2, one_rm: "100", one_rm_source: "logged" },
          ],
        }),
      );
      expect(c.exercises.map((e) => e.id)).toEqual([1, 2, 3]);
      const row = c.exercises[2];
      expect(lineNumbers(row)).toEqual([1, 2]);
      expect(row.sub_lines.every((l) => l._k && l.savedText === "")).toBe(true);
      expect(row.unplaced).toEqual([]);
      expect(row.one_rm).toBe("100");
      expect(row.e1rm).toBe("");
    });

    it("a removed exercise goes, unless it holds a dirty or queued line", async () => {
      routeFetch();
      const c = syncPage({
        exercises: [
          { id: 1, pad_lines: 1 },
          { id: 2, pad_lines: 1 },
          { id: 3, pad_lines: 1 },
        ],
      });
      c.exercises[1].sub_lines[0].text = "typed";
      c.enqueueCell({ exercise_id: 3, line: 1, text: "queued" });
      await pollWith(c, syncBody({ exercises: [{ id: 1 }] }));
      expect(c.exercises.map((e) => e.id)).toEqual([1, 2, 3]);
      expect(c.readQueue()).toHaveLength(1);
      c.exercises[1].sub_lines[0].text = "";
      c.dropEntry(c.readQueue()[0]);
      await pollWith(c, syncBody({ sync_v: 9, exercises: [{ id: 1 }] }));
      expect(c.exercises.map((e) => e.id)).toEqual([1]);
    });

    it("progress is applied, ordered by as_of", async () => {
      routeFetch();
      const c = syncPage();
      c.applyProgress({ logged: 3, prescribed: 4, as_of: 50 });
      await pollWith(c, syncBody({ progress: { logged: 1, prescribed: 4, as_of: 10 } }));
      expect(c.progress.logged).toBe(3);
      await pollWith(c, syncBody({ sync_v: 9, progress: { logged: 4, prescribed: 4, as_of: 90 } }));
      expect(c.progress.logged).toBe(4);
    });

    it("status is adopted, but not during finish() or with a queued log", async () => {
      routeFetch();
      const c = syncPage();
      await pollWith(c, syncBody({ status: "done" }));
      expect(c.status).toBe("done");
      c.status = "pending";
      c.saving = true;
      await pollWith(c, syncBody({ sync_v: 9, status: "done" }));
      expect(c.status).toBe("pending");
      c.saving = false;
      c.enqueue({ status: "done" });
      await pollWith(c, syncBody({ sync_v: 10, status: "done" }));
      expect(c.status).toBe("pending");
    });

    it("status older than an adopted log answer is not adopted", async () => {
      const calls = routeFetch({ log: () => res({ body: { ...logBody("done"), sync_v: 9 } }) });
      const c = syncPage();
      await c.finish();
      expect(calls.log).toHaveLength(1);
      expect(c.status).toBe("done");
      await pollWith(c, syncBody({ sync_v: 8, status: "pending" }));
      expect(c.status).toBe("done");
      await pollWith(c, syncBody({ sync_v: 10, status: "pending" }));
      expect(c.status).toBe("pending");
    });

    it("notes are adopted only when clean", async () => {
      routeFetch();
      const c = syncPage();
      await pollWith(c, syncBody({ notes: "coach saw it" }));
      expect(c.notes).toBe("coach saw it");
      expect(c._notesSavedText).toBe("coach saw it");
      c.notes = "typing";
      c.noteInput();
      await pollWith(c, syncBody({ sync_v: 9, notes: "other" }));
      expect(c.notes).toBe("typing");
    });
  });
});

describe("two queued new lines, the coach logs on line 1 meanwhile (e2e)", () => {
  // A server that holds the coach's set on line 1 and files a `new` write on a
  // taken number onto the next free one, answering like the real endpoint.
  function fakeServer() {
    const stack = new Map([[1, "5 @ 225"]]);
    const posted = [];
    global.fetch = vi.fn(async (url, opts) => {
      const body = JSON.parse(opts.body);
      posted.push(body);
      let line = body.line;
      let relocated = false;
      if (body.new && stack.has(line)) {
        relocated = true;
        const from = line;
        line = 1;
        while (stack.has(line)) line += 1;
        stack.set(line, body.text);
        return res({ body: relocatedAnswer(from, line, body.text) });
      }
      stack.set(line, body.text);
      return res({ body: relocatedAnswer(null, line, body.text) });
    });
    const relocatedAnswer = (from, line, text) => ({
      ok: true,
      sync_v: 9,
      progress: { logged: 0, prescribed: 4 },
      ...(from ? { relocated_from: from } : {}),
      cell: { line, text, warn: false, warn_reason: "", entered_by_coach: false },
      exercise_lines: {
        coach_lines: [],
        sub_lines: [...stack].map(([l, t]) => ({ line: l, text: t, entered_by_coach: l === 1 })),
      },
    });
    return posted;
  }

  it("converges to each line exactly once", async () => {
    const posted = fakeServer();
    const c = initWith({ exercises: [{ id: 1, pad_lines: 2 }] });
    const ex = c.exercises[0];
    ex.sub_lines[0].text = "225 x 5";
    ex.sub_lines[1].text = "230 x 5";
    global.fetch = vi.fn().mockRejectedValue(new TypeError("offline"));
    await c.saveCell(ex, 1);
    await c.saveCell(ex, 2);
    expect(c.readQueue()).toHaveLength(2);
    const server = fakeServer();
    await c.flushQueue();
    expect(server.length).toBeGreaterThanOrEqual(2);
    expect(ex.sub_lines.map((l) => l.text)).toEqual(["5 @ 225", "225 x 5", "230 x 5"]);
    expect(ex.sub_lines.map((l) => l.line)).toEqual([1, 2, 3]);
    expect(c.readQueue()).toEqual([]);
    expect(posted).toEqual([]);
  });
});

describe("live session sync: review round 1 (#709)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    for (const c of syncPages.splice(0)) c._pollStopped = true;
    document.body.querySelectorAll("input.focus-probe").forEach((n) => n.remove());
    delete document.visibilityState;
    vi.useRealTimers();
  });

  const focusOn = (attr, id) => {
    const input = document.createElement("input");
    input.className = "focus-probe";
    input.dataset[attr] = String(id);
    document.body.appendChild(input);
    input.focus();
    return input;
  };
  const srvLine = (line, text, token = "", extra = {}) => ({ line, text, token, ...extra });

  describe("1. a landed write is recognized by its token", () => {
    function landed(text) {
      const c = syncPage({ exercises: [{ id: 1, pad_lines: 1 }] });
      const ex = c.exercises[0];
      const entry = ex.sub_lines[0];
      entry.text = text;
      entry.savedText = undefined; // the answer was lost
      entry.newToken = "tk";
      c.enqueueCell({ exercise_id: 1, line: 1, text, new: true, token: "tk" });
      return { c, ex, entry };
    }
    const stack = {
      coach_lines: [],
      sub_lines: [
        srvLine(1, "5 @ 225", "", { entered_by_coach: true }),
        srvLine(2, "225 x 5", "tk"),
      ],
    };

    it("an edited entry keeps its text, re-keys onto the landed line and re-posts with the token", () => {
      const { c, ex, entry } = landed("230 x 5");
      c.applyExerciseLines(ex, stack);
      expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([
        [1, "5 @ 225"],
        [2, "230 x 5"],
      ]);
      expect(ex.sub_lines[1]).toBe(entry);
      expect(entry.savedText).toBe("225 x 5");
      expect(entry.newToken).toBe("tk");
      expect(c.readQueue().map((q) => q.body)).toEqual([
        { exercise_id: 1, line: 2, text: "230 x 5", new: true, token: "tk" },
      ]);
    });

    it("an unedited entry just becomes the server's line, once", () => {
      const { c, ex, entry } = landed("225 x 5");
      c.applyExerciseLines(ex, stack);
      expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([
        [1, "5 @ 225"],
        [2, "225 x 5"],
      ]);
      expect(ex.sub_lines[1]).toBe(entry);
      expect(entry.newToken).toBeUndefined();
      expect(c.readQueue()).toEqual([]);
    });

    it("with no token match it behaves as before (displaced as someone else's text)", () => {
      const { c, ex } = landed("230 x 5");
      c.applyExerciseLines(ex, {
        coach_lines: [],
        sub_lines: [srvLine(1, "5 @ 225", "", { entered_by_coach: true })],
      });
      expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([
        [1, "5 @ 225"],
        [2, "230 x 5"],
      ]);
    });
  });

  describe("2. a save running is left alone", () => {
    it("a merge neither moves it nor adds its write's server line a second time", () => {
      const c = syncPage({ exercises: [{ id: 1, pad_lines: 1 }] });
      const ex = c.exercises[0];
      ex.sub_lines[0].text = "225 x 5";
      ex.sub_lines[0].newToken = "tk";
      c._lineSavesRunning["1:1"] = 1;
      c.applyExerciseLines(ex, {
        coach_lines: [],
        sub_lines: [srvLine(1, "5 @ 225", "", { entered_by_coach: true }), srvLine(2, "225 x 5", "tk")],
      });
      expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([[1, "225 x 5"]]);
    });

    it("a poll merged while the answer body is being read leaves one copy", async () => {
      let gate;
      const held = new Promise((r) => (gate = r));
      const calls = routeFetch({
        cell: (b) => ({
          ok: true,
          status: 200,
          redirected: false,
          json: async () => {
            await held;
            return { ok: true, sync_v: 12, cell: { line: b.line, text: b.text } };
          },
        }),
      });
      const c = syncPage({ exercises: [{ id: 1, pad_lines: 1 }] });
      const ex = c.exercises[0];
      ex.sub_lines[0].text = "225 x 5";
      const save = c.saveCell(ex, 1);
      await tick(0);
      c.applySync(
        syncBody({
          sync_v: 11,
          exercises: [
            { id: 1, coach_lines: [], sub_lines: [srvLine(2, "225 x 5", calls.cell[0].token)] },
          ],
        }),
      );
      gate();
      await save;
      expect(ex.sub_lines.map((l) => [l.line, l.text])).toEqual([[1, "225 x 5"]]);
      expect(c.readQueue()).toEqual([]);
    });

    it("an early return removes a clean copy carrying the same token, never a dirty one", () => {
      const c = syncPage({ exercises: [{ id: 1, pad_lines: 1 }] });
      const ex = c.exercises[0];
      const keep = { line: 2, text: "a", savedText: "a", _k: "k1" };
      const copy = { line: 1, text: "a", savedText: "a", serverToken: "tk", _k: "k2" };
      const edited = { line: 3, text: "b", savedText: "a", serverToken: "tk", _k: "k3" };
      ex.sub_lines = [copy, keep, edited];
      c._dropTokenCopies(ex, keep, "tk");
      expect(ex.sub_lines.map((l) => l._k)).toEqual(["k1", "k3"]);
    });
  });

  describe("3. focus", () => {
    const body = (v, ex1) =>
      syncBody({
        sync_v: v,
        exercises: [
          ex1,
          { id: 2, name: "Press", coach_lines: [{ line: 1, text: "cue 2" }] },
        ],
      });

    it("a focused clean line does not take the coach's text; the exercise waits, the poll retries fast", async () => {
      const calls = routeFetch({
        sync: () =>
          res({ body: body(8, { id: 1, name: "Squat", sub_lines: [srvLine(1, "5 @ 225", "", { entered_by_coach: true })] }) }),
      });
      const c = syncPage();
      const input = focusOn("mesoEx", 1);
      await tick(3000);
      expect(c.exercises[0].sub_lines[0].text).toBe("");
      expect(c.exercises[1].coach_lines).toEqual([{ line: 1, text: "cue 2" }]);
      await tick(3000);
      expect(calls.sync).toEqual([SYNC_URL + "?v=5", SYNC_URL + "?v=5"]);
      input.blur();
      await tick(3000);
      expect(c.exercises[0].sub_lines[0].text).toBe("5 @ 225");
      await tick(3000);
      expect(calls.sync[3]).toBe(SYNC_URL + "?v=8");
    });

    it("a focused dirty line is never renumbered", async () => {
      routeFetch({
        sync: () =>
          res({
            body: body(8, {
              id: 1,
              name: "Squat",
              coach_lines: [{ line: 1, text: "cue" }],
              sub_lines: [],
            }),
          }),
      });
      const c = syncPage();
      const line = c.exercises[0].sub_lines[0];
      line.text = "half-typed";
      focusOn("mesoEx", 1);
      await tick(3000);
      expect(c.exercises[0].sub_lines[0]).toBe(line);
      expect(line.line).toBe(1);
      expect(c.exercises[0].coach_lines).toEqual([]);
    });

    it("the 1RM input's focus holds its exercise too", async () => {
      routeFetch({ sync: () => res({ body: body(8, { id: 1, name: "Squat", coach_lines: [{ line: 3, text: "cue" }] }) }) });
      const c = syncPage();
      focusOn("mesoEx", 1);
      await tick(3000);
      expect(c.exercises[0].coach_lines).toEqual([]);
    });
  });

  describe("5. a log answer with no stamp", () => {
    it("counts as current as of the stamp held", async () => {
      routeFetch({ log: () => res({ body: logBody("done") }) });
      const c = syncPage();
      await c.finish();
      expect(c._logV).toBe(5);
    });

    it("an older in-flight poll cannot revert the status", async () => {
      let release;
      routeFetch({
        sync: () => new Promise((r) => (release = () => r(res({ body: syncBody({ sync_v: 8, status: "pending" }) })))),
        log: () => res({ body: logBody("done") }),
      });
      const c = syncPage();
      c.pollNow();
      await tick(0);
      await c.finish();
      expect(c.status).toBe("done");
      release();
      await tick(0);
      expect(c.status).toBe("done");
    });
  });

  describe("1RM", () => {
    const withOneRm = (sx) => syncBody({ exercises: [{ id: 1, name: "Squat", ...sx }, { id: 2, name: "Press" }] });
    async function poll(c, body) {
      routeFetch({ sync: () => res({ body }) });
      window.dispatchEvent(new Event("focus"));
      await tick(0);
    }

    it("a manual value seeds the input, a logged one the placeholder", async () => {
      const c = syncPage();
      await poll(c, withOneRm({ one_rm: "140", one_rm_source: "manual" }));
      expect([c.exercises[0].e1rm, c.exercises[0].one_rm]).toEqual(["140", ""]);
      await poll(c, { ...withOneRm({ one_rm: "150", one_rm_source: "logged" }), sync_v: 9 });
      expect([c.exercises[0].e1rm, c.exercises[0].one_rm]).toEqual(["", "150"]);
    });

    it("is not taken while the input has a save pending or running", async () => {
      const c = syncPage();
      c.exercises[0].e1rm = "typed";
      c._oneRmTimers[1] = 1;
      await poll(c, withOneRm({ one_rm: "140", one_rm_source: "manual" }));
      expect(c.exercises[0].e1rm).toBe("typed");
      delete c._oneRmTimers[1];
      c._oneRmBusy[1] = 1;
      await poll(c, { ...withOneRm({ one_rm: "140", one_rm_source: "manual" }), sync_v: 9 });
      expect(c.exercises[0].e1rm).toBe("typed");
      c._oneRmBusy[1] = 0;
      await poll(c, { ...withOneRm({ one_rm: "140", one_rm_source: "manual" }), sync_v: 10 });
      expect(c.exercises[0].e1rm).toBe("140");
    });
  });
});

describe("live session sync: final round (#709)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    for (const c of syncPages.splice(0)) c._pollStopped = true;
    vi.useRealTimers();
  });

  it("unplaced text whose write landed is removed by a later merge", () => {
    const c = syncPage({ exercises: [{ id: 1, pad_lines: 1 }] });
    const ex = c.exercises[0];
    const entry = ex.sub_lines[0];
    entry.text = "230 x 5";
    entry.savedText = undefined;
    entry.newToken = "tk";
    c.enqueueCell({ exercise_id: 1, line: 1, text: "230 x 5", new: true, token: "tk" });
    // Another queued write already targets the landed line's number, so the
    // retarget is refused and the edit is parked.
    c.enqueueCell({ exercise_id: 1, line: 2, text: "other", new: true, token: "ot" });
    const stack = {
      coach_lines: [],
      sub_lines: [
        { line: 1, text: "5 @ 225", token: "", entered_by_coach: true },
        { line: 2, text: "225 x 5", token: "tk" },
      ],
    };
    c.applyExerciseLines(ex, stack);
    expect(ex.unplaced).toEqual([{ text: "230 x 5", token: "tk" }]);
    // The replay lands through the token; the next merge sees the line.
    c.applyExerciseLines(ex, stack);
    expect(ex.unplaced).toEqual([]);
  });

  it("an unplaced item without a matching token stays", () => {
    const c = syncPage({ exercises: [{ id: 1, pad_lines: 1 }] });
    const ex = c.exercises[0];
    ex.unplaced = [{ text: "a", token: "x" }, { text: "b", token: "tk" }];
    c.applyExerciseLines(ex, { coach_lines: [], sub_lines: [{ line: 2, text: "b", token: "tk" }] });
    expect(ex.unplaced).toEqual([{ text: "a", token: "x" }]);
  });

  it("an exercise with a pending or running 1RM save is kept though the poll omits it", async () => {
    routeFetch({ sync: () => res({ body: syncBody({ exercises: [{ id: 1, name: "Squat" }] }) }) });
    const c = syncPage();
    c._oneRmTimers[2] = 1;
    window.dispatchEvent(new Event("focus"));
    await tick(0);
    expect(c.exercises.map((e) => e.id)).toEqual([1, 2]);
    delete c._oneRmTimers[2];
    c._oneRmBusy[2] = 1;
    window.dispatchEvent(new Event("focus"));
    await tick(3000);
    expect(c.exercises.map((e) => e.id)).toEqual([1, 2]);
    c._oneRmBusy[2] = 0;
    window.dispatchEvent(new Event("focus"));
    await tick(3000);
    expect(c.exercises.map((e) => e.id)).toEqual([1]);
  });

  it("a 1RM answer's stamp guards the exercise against an older poll", async () => {
    const c = syncPage();
    c.oneRmUrl = "/meso/api/me/session/42/one-rm/";
    c.exercises[0].e1rm = "150";
    const calls = routeFetch({
      sync: () =>
        res({
          body: syncBody({
            sync_v: 10,
            exercises: [{ id: 1, name: "Squat", one_rm: "100", one_rm_source: "manual" }, { id: 2, name: "Press" }],
          }),
        }),
    });
    const base = global.fetch;
    global.fetch = vi.fn((url, opts) =>
      url === c.oneRmUrl
        ? Promise.resolve(res({ body: { ok: true, one_rm: "150", source: "manual", sync_v: 12 } }))
        : base(url, opts),
    );
    await c._postOneRm(c.exercises[0]);
    expect(c.exerciseV[1]).toBe(12);
    window.dispatchEvent(new Event("focus"));
    await tick(0);
    expect(calls.sync).toHaveLength(1);
    expect(c.exercises[0].e1rm).toBe("150");
  });
});

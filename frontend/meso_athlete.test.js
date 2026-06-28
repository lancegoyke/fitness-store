// Tests for the athlete session logger (app/store_project/static/js/meso_athlete.js).
//
// Focus: the logic that is fragile and effectively impossible to verify by hand
// — the offline write queue (stash on network failure, dedupe per session,
// replay on reconnect) and the save/flush state machine. The pure helpers
// (rowFilled / buildPayload / syncFromLog) are covered too since the queue
// payloads are built from them.

import { createLogger } from "../app/store_project/static/js/meso_athlete.js";

const LOG_URL = "/meso/api/me/session/42/log/";

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
    expect(c.rowFilled({ done: false, reps: "", load: "", rpe: "" })).toBe(false);
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
        body: { log: { status: "done", sets: [{ prescription: 1, set_number: 1 }] } },
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
      res({ body: { log: { status: "done", sets: [{ prescription: 1, set_number: 1 }] } } }),
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

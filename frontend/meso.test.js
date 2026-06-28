// Tests for the meso designer (app/store_project/static/js/meso.js).
//
// Focus: the agent poll loop (pollBatch) — a background job is kicked off, then
// the client polls a status endpoint until the batch lands, fails, or the poll
// gives up. The branching there (drafting → keep polling, failed, !ok, network
// error, timeout, missing url) is the fragile part. Also covers the response
// shaping helpers (agentErrorText, batchMessage).

import { createMeso } from "../app/store_project/static/js/meso.js";

// A meso component wired enough to run pollBatch without a live Alpine runtime:
// stub the DOM-bound nav helpers and make sleep instant so polling doesn't wait.
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

// The text of the last agent message pushed onto the thread.
function lastAgent(c) {
  return c.messages[c.messages.length - 1];
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("agentErrorText", () => {
  const c = createMeso();
  it("maps known statuses to friendly copy", () => {
    expect(c.agentErrorText(503, {})).toMatch(/isn't configured/);
    expect(c.agentErrorText(502, {})).toMatch(/trouble responding/);
    expect(c.agentErrorText(400, {})).toMatch(/shorter instruction/);
  });
  it("prefers a server-provided error for other statuses", () => {
    expect(c.agentErrorText(500, { error: "boom" })).toBe("boom");
  });
  it("falls back to a generic message", () => {
    expect(c.agentErrorText(500, {})).toMatch(/couldn't process/);
  });
});

describe("batchMessage", () => {
  it("exposes a review link only when there are changes", () => {
    const c = createMeso();
    const msg = c.batchMessage({
      summary: "Lowered Day 2 volume.",
      changes: [{ kind: "edit" }],
      review_url: "/meso/review/9/",
    });
    expect(msg.text).toBe("Lowered Day 2 volume.");
    expect(msg.changes).toHaveLength(1);
    expect(msg.reviewUrl).toBe("/meso/review/9/");
  });

  it("uses a fallback message and no review link when nothing changed", () => {
    const c = createMeso();
    const msg = c.batchMessage({ summary: "", changes: [], review_url: "/x/" });
    expect(msg.text).toMatch(/couldn't find any safe changes/);
    expect(msg.reviewUrl).toBe(null);
  });
});

describe("pollBatch", () => {
  it("reports an error when no status url is given", async () => {
    const c = makeMeso();
    await c.pollBatch(undefined);
    expect(lastAgent(c).error).toBe(true);
    expect(lastAgent(c).text).toMatch(/couldn't process/);
  });

  it("polls while drafting, then renders the resolved batch", async () => {
    const c = makeMeso();
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ body: { status: "drafting" } }))
      .mockResolvedValueOnce(res({ body: { status: "drafting" } }))
      .mockResolvedValueOnce(
        res({
          body: {
            status: "pending",
            summary: "Done.",
            changes: [{ kind: "edit" }],
            review_url: "/meso/review/3/",
          },
        }),
      );
    await c.pollBatch("/status/");
    expect(global.fetch).toHaveBeenCalledTimes(3);
    expect(lastAgent(c).text).toBe("Done.");
    expect(lastAgent(c).reviewUrl).toBe("/meso/review/3/");
    expect(lastAgent(c).error).toBeUndefined();
  });

  it("surfaces the agent's own error message on a failed batch", async () => {
    const c = makeMeso();
    global.fetch = vi
      .fn()
      .mockResolvedValue(res({ body: { status: "failed", error: "model refused" } }));
    await c.pollBatch("/status/");
    expect(lastAgent(c).error).toBe(true);
    expect(lastAgent(c).text).toBe("model refused");
  });

  it("maps an HTTP error status while polling", async () => {
    const c = makeMeso();
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 503, body: {} }));
    await c.pollBatch("/status/");
    expect(lastAgent(c).error).toBe(true);
    expect(lastAgent(c).text).toMatch(/isn't configured/);
  });

  it("reports a network error and stops polling", async () => {
    const c = makeMeso();
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.pollBatch("/status/");
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(lastAgent(c).error).toBe(true);
    expect(lastAgent(c).text).toMatch(/Something went wrong/);
  });

  it("gives up with a hint after the attempt cap", async () => {
    const c = makeMeso({ pollMaxAttempts: 3 });
    global.fetch = vi.fn().mockResolvedValue(res({ body: { status: "drafting" } }));
    await c.pollBatch("/status/");
    expect(global.fetch).toHaveBeenCalledTimes(3);
    expect(lastAgent(c).error).toBe(true);
    expect(lastAgent(c).text).toMatch(/taking longer than expected/);
  });
});

// Ported from frontend/meso.test.js's "agentErrorText" / "batchMessage" /
// "pollBatch" describe-blocks, adapted to the dependency-injected function
// signature (no `createMeso()` instance — fetch/sleep/message-sink are
// passed explicitly). Same assertions, same edge cases.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { agentErrorText, batchMessage, pollBatch, type AgentMessage } from "./agent";

function res({ ok = true, status = 200, body = {} } = {}) {
  return { ok, status, json: async () => body };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("agentErrorText", () => {
  it("maps known statuses to friendly copy", () => {
    expect(agentErrorText(503, {})).toMatch(/isn't configured/);
    expect(agentErrorText(502, {})).toMatch(/trouble responding/);
    expect(agentErrorText(400, {})).toMatch(/shorter instruction/);
  });
  it("prefers a server-provided error for other statuses", () => {
    expect(agentErrorText(500, { error: "boom" })).toBe("boom");
  });
  it("falls back to a generic message", () => {
    expect(agentErrorText(500, {})).toMatch(/couldn't process/);
  });
});

describe("batchMessage", () => {
  it("exposes a review link only when there are changes", () => {
    const msg = batchMessage({
      summary: "Lowered Day 2 volume.",
      changes: [{ kind: "edit" }],
      review_url: "/meso/review/9/",
    });
    expect(msg.text).toBe("Lowered Day 2 volume.");
    expect(msg.changes).toHaveLength(1);
    expect(msg.reviewUrl).toBe("/meso/review/9/");
  });

  it("uses a fallback message and no review link when nothing changed", () => {
    const msg = batchMessage({ summary: "", changes: [], review_url: "/x/" });
    expect(msg.text).toMatch(/couldn't find any safe changes/);
    expect(msg.reviewUrl).toBe(null);
  });
});

describe("pollBatch", () => {
  // A fake sleep that resolves instantly (mirrors makeMeso()'s `c.sleep`
  // stub) so specs don't wait out real intervals.
  const instantSleep = () => Promise.resolve();

  function collect() {
    const messages: AgentMessage[] = [];
    const onMessage = (m: AgentMessage) => messages.push(m);
    return { messages, onMessage, last: () => messages[messages.length - 1] };
  }

  it("reports an error when no status url is given", async () => {
    const { onMessage, last } = collect();
    await pollBatch(undefined, { onMessage });
    expect(last()?.error).toBe(true);
    expect(last()?.text).toMatch(/couldn't process/);
  });

  it("polls while drafting, then renders the resolved batch", async () => {
    const fetchImpl = vi
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
    const { onMessage, last } = collect();
    await pollBatch("/status/", { fetchImpl, sleep: instantSleep, onMessage });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(last()?.text).toBe("Done.");
    expect(last()?.reviewUrl).toBe("/meso/review/3/");
    expect(last()?.error).toBeUndefined();
  });

  it("surfaces the agent's own error message on a failed batch", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(res({ body: { status: "failed", error: "model refused" } }));
    const { onMessage, last } = collect();
    await pollBatch("/status/", { fetchImpl, sleep: instantSleep, onMessage });
    expect(last()?.error).toBe(true);
    expect(last()?.text).toBe("model refused");
  });

  it("maps an HTTP error status while polling", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(res({ ok: false, status: 503, body: {} }));
    const { onMessage, last } = collect();
    await pollBatch("/status/", { fetchImpl, sleep: instantSleep, onMessage });
    expect(last()?.error).toBe(true);
    expect(last()?.text).toMatch(/isn't configured/);
  });

  it("reports a network error and stops polling", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const { onMessage, last } = collect();
    await pollBatch("/status/", { fetchImpl, sleep: instantSleep, onMessage });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(last()?.error).toBe(true);
    expect(last()?.text).toMatch(/Something went wrong/);
  });

  it("gives up with a hint after the attempt cap", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(res({ body: { status: "drafting" } }));
    const { onMessage, last } = collect();
    await pollBatch("/status/", { fetchImpl, sleep: instantSleep, maxAttempts: 3, onMessage });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(last()?.error).toBe(true);
    expect(last()?.text).toMatch(/taking longer than expected/);
  });
});

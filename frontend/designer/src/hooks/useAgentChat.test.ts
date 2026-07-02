// Specs for useAgentChat (CONTRACT.md "useAgentChat") — composer send/chip,
// typing-state gating, error text mapping via lib/agent.ts's pollBatch, and
// resume-from-thread (a hydrated last message still drafting when the page
// rendered). Ported from meso.test.js's pollBatch/agentErrorText/batchMessage
// coverage plus init()/hydrateThread()/resumeDrafting()'s behavior.
import { act, renderHook } from "@testing-library/react";
import { useAgentChat } from "./useAgentChat";
import type { ChatMessage } from "./useAgentChat";

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function setup(initialMessages: ChatMessage[] = [], initialResumeUrl: string | null = null) {
  return renderHook(() =>
    useAgentChat({ planId: 7, csrf: "tok", initialMessages, initialResumeUrl }),
  );
}

function lastMessage(messages: ChatMessage[]) {
  return messages[messages.length - 1];
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

async function flush(times = 5) {
  for (let i = 0; i < times; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

describe("hydration", () => {
  it("starts with the hydrated initial messages", () => {
    const seed: ChatMessage[] = [{ id: 1, role: "agent", text: "hi" }];
    const { result } = setup(seed);
    expect(result.current.messages).toEqual(seed);
  });

  it("exposes the static chip labels", () => {
    const { result } = setup();
    expect(result.current.chips.length).toBeGreaterThan(0);
    expect(result.current.chips[0]).toHaveProperty("label");
  });
});

describe("onSend", () => {
  it("pushes a coach message, sets agentTyping, and POSTs the instruction", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ status_url: "/status/1/" }, true, 202)) as unknown as typeof fetch;
    act(() => result.current.setInputText("lighten Friday"));
    act(() => result.current.onSend());
    expect(result.current.inputText).toBe("");
    expect(result.current.messages.some((m) => m.role === "coach" && m.text === "lighten Friday")).toBe(true);
    expect(result.current.agentTyping).toBe(true);
    const [url, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/meso/api/plan/7/agent/");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ instruction: "lighten Friday" });
    await flush();
  });

  it("does nothing for a blank instruction", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    act(() => result.current.setInputText("   "));
    act(() => result.current.onSend());
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("no-ops (can't double-submit) while agentTyping", async () => {
    const { result } = setup();
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(() => new Promise((resolve) => (resolveFetch = resolve))) as unknown as typeof fetch;
    act(() => result.current.setInputText("first"));
    act(() => result.current.onSend());
    act(() => result.current.setInputText("second"));
    act(() => result.current.onSend());
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveFetch(res({ status_url: null }, true, 202));
      await Promise.resolve();
    });
  });

  it("onInputKey Enter triggers onSend", () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ status_url: null }, true, 202)) as unknown as typeof fetch;
    act(() => result.current.setInputText("go"));
    const preventDefault = vi.fn();
    act(() =>
      result.current.onInputKey({
        key: "Enter",
        preventDefault,
      } as unknown as Parameters<typeof result.current.onInputKey>[0]),
    );
    expect(preventDefault).toHaveBeenCalled();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("onChip", () => {
  it("sends the chip's label verbatim as the instruction", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ status_url: null }, true, 202)) as unknown as typeof fetch;
    act(() => result.current.onChip("Add a deload week"));
    expect(result.current.messages.some((m) => m.role === "coach" && m.text === "Add a deload week")).toBe(true);
    const [, opts] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ instruction: "Add a deload week" });
    await flush();
  });

  it("no-ops while agentTyping", async () => {
    const { result } = setup();
    let resolveFetch: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn(() => new Promise((resolve) => (resolveFetch = resolve))) as unknown as typeof fetch;
    act(() => result.current.onChip("A"));
    act(() => result.current.onChip("B"));
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveFetch(res({ status_url: null }, true, 202));
      await Promise.resolve();
    });
  });
});

describe("agent reply rendering (immediate resolution, no status_url)", () => {
  it("pushes the couldn't-process message when the POST reply carries no status_url", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, true, 202)) as unknown as typeof fetch;
    act(() => result.current.onChip("Lower Day 2 volume"));
    await flush();
    const last = lastMessage(result.current.messages);
    expect(last!.role).toBe("agent");
    expect(last!.error).toBe(true);
    expect(last!.text).toMatch(/couldn't process/);
    expect(result.current.agentTyping).toBe(false);
  });

  it("resolves a batch reply with changes + review link", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ status_url: "/status/1/" }, true, 202))
      .mockResolvedValueOnce(
        res({
          status: "pending",
          summary: "Lowered Day 2 volume.",
          changes: [{ id: 1, title: "Squat -10%" }],
          review_url: "/meso/review/9/",
        }),
      ) as unknown as typeof fetch;
    act(() => result.current.onChip("Lower Day 2 volume"));
    await flush();
    const last = lastMessage(result.current.messages);
    expect(last!.text).toBe("Lowered Day 2 volume.");
    expect(last!.changes).toEqual([{ id: 1, title: "Squat -10%" }]);
    expect(last!.reviewUrl).toBe("/meso/review/9/");
    expect(result.current.agentTyping).toBe(false);
  });

  it("polls through a drafting status before resolving", async () => {
    const { result } = setup();
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ status_url: "/status/1/" }, true, 202))
      .mockResolvedValueOnce(res({ status: "drafting" }))
      .mockResolvedValueOnce(res({ status: "pending", summary: "Done.", changes: [] })) as unknown as typeof fetch;
    act(() => result.current.onChip("go"));
    await flush();
    expect(result.current.agentTyping).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    await flush();
    expect(result.current.agentTyping).toBe(false);
    expect(lastMessage(result.current.messages)!.text).toMatch(/couldn't find any safe changes|Done\./);
  });
});

describe("error paths", () => {
  it("maps a non-2xx POST reply via agentErrorText (503)", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 503)) as unknown as typeof fetch;
    act(() => result.current.onChip("go"));
    await flush();
    const last = lastMessage(result.current.messages);
    expect(last!.error).toBe(true);
    expect(last!.text).toMatch(/isn't configured/);
  });

  it("maps a 402 (allowance exhausted) POST reply via the server error field", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({ error: "Allowance exhausted" }, false, 402)) as unknown as typeof fetch;
    act(() => result.current.onChip("go"));
    await flush();
    expect(lastMessage(result.current.messages)!.text).toBe("Allowance exhausted");
  });

  it("maps a 400 POST reply", async () => {
    const { result } = setup();
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 400)) as unknown as typeof fetch;
    act(() => result.current.onChip("go"));
    await flush();
    expect(lastMessage(result.current.messages)!.text).toMatch(/shorter instruction/);
  });

  it("pushes the generic network-error message on a thrown fetch", async () => {
    const { result } = setup();
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) as unknown as typeof fetch;
    act(() => result.current.onChip("go"));
    await flush();
    const last = lastMessage(result.current.messages);
    expect(last!.error).toBe(true);
    expect(last!.text).toMatch(/Something went wrong reaching the agent/);
    expect(result.current.agentTyping).toBe(false);
  });
});

describe("resume-from-thread", () => {
  it("resumes polling the hydrated thread's pending pollUrl on mount", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce(res({ status: "pending", summary: "Resumed.", changes: [] })) as unknown as typeof fetch;
    const { result } = setup(
      [{ id: 1, role: "agent", text: "Tell me..." }],
      "/status/resume/",
    );
    await flush();
    expect(globalThis.fetch).toHaveBeenCalledWith("/status/resume/");
    expect(lastMessage(result.current.messages)!.text).toMatch(/Resumed\.|couldn't find any safe changes/);
  });

  it("does not poll when there is no resume url", async () => {
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
    setup([{ id: 1, role: "agent", text: "hi" }], null);
    await flush();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

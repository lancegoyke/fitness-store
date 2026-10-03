// Specs for usePlanSync (#709 PR 2): the designer's live-sync poll. Cadence
// (3s active, 3 -> 6 -> 12 -> 30s idle backoff), visibility/focus, no
// overlap, a quiet stop on 404/403, silent backoff on network errors.
import { act, renderHook } from "@testing-library/react";
import { usePlanSync } from "./usePlanSync";
import type { MesoGrid } from "../lib/api";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

function res(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

const unchanged = (v = 5) => res({ ok: true, changed: false, sync_v: v });

function setup(opts: { version?: number | undefined; onGrid?: (g: MesoGrid) => boolean } = {}) {
  const onGrid = opts.onGrid ?? vi.fn(() => false);
  const getVersion = vi.fn(() => ("version" in opts ? opts.version : 5));
  const hook = renderHook(() => usePlanSync({ planId: 7, mesocycleId: 3, getVersion, onGrid }));
  return { ...hook, onGrid, getVersion };
}

function calls() {
  return (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.useFakeTimers();
  setVisibility("visible");
  globalThis.fetch = vi.fn().mockResolvedValue(unchanged()) as unknown as typeof fetch;
});

afterEach(() => {
  vi.useRealTimers();
  setVisibility("visible");
});

describe("usePlanSync", () => {
  it("polls the sync endpoint with the known version and block", async () => {
    setup();
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1);
    expect(calls()[0]![0]).toBe("/meso/api/plan/7/sync/?v=5&mesocycle=3");
  });

  it("backs off 3 -> 6 -> 12 -> 30s while idle, and stays at 30s", async () => {
    setup();
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(5900);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(100);
    expect(calls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(12000);
    expect(calls()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(30000);
    expect(calls()).toHaveLength(4);
    await vi.advanceTimersByTimeAsync(29900);
    expect(calls()).toHaveLength(4);
    await vi.advanceTimersByTimeAsync(100);
    expect(calls()).toHaveLength(5);
  });

  it("stays at 3s while active (a remote change) and marks active on a changed merge", async () => {
    const onGrid = vi.fn(() => true);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(res({ ok: true, changed: true, sync_v: 6, grid: { sync_v: 6 } })) as unknown as typeof fetch;
    setup({ onGrid });
    await vi.advanceTimersByTimeAsync(3000);
    expect(onGrid).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(3);
  });

  it("a merge that changed nothing does not mark active", async () => {
    const onGrid = vi.fn(() => false);
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(res({ ok: true, changed: true, sync_v: 6, grid: { sync_v: 6 } })) as unknown as typeof fetch;
    setup({ onGrid });
    await vi.advanceTimersByTimeAsync(3000);
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1); // next is at 6s
  });

  it("markActive snaps a long idle wait back to 3s", async () => {
    const { result } = setup();
    await vi.advanceTimersByTimeAsync(3000 + 6000 + 12000);
    expect(calls()).toHaveLength(3);
    act(() => result.current.markActive());
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(4);
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(5);
  });

  it("stops polling while hidden, and polls at once on visible", async () => {
    setup();
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1);
    act(() => setVisibility("hidden"));
    await vi.advanceTimersByTimeAsync(120000);
    expect(calls()).toHaveLength(1);
    await act(async () => {
      setVisibility("visible");
    });
    expect(calls()).toHaveLength(2);
  });

  it("polls at once on window focus", async () => {
    setup();
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(calls()).toHaveLength(1);
  });

  it("never runs two polls at once", async () => {
    let release!: (v: unknown) => void;
    globalThis.fetch = vi.fn(
      () => new Promise((r) => (release = r)),
    ) as unknown as typeof fetch;
    setup();
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      setVisibility("visible");
    });
    await vi.advanceTimersByTimeAsync(60000);
    expect(calls()).toHaveLength(1);
    await act(async () => release(unchanged()));
    await vi.advanceTimersByTimeAsync(6000);
    expect(calls()).toHaveLength(2);
  });

  it("a 404 backs off to 60s and keeps retrying (a rolling deploy's old replica)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockResolvedValue(res({}, false, 404)) as unknown as typeof fetch;
    setup();
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(59900);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(100);
    expect(calls()).toHaveLength(2);
    expect(warn).not.toHaveBeenCalled();
  });

  it("a 200 after a 404 resets the count, so only 10 in a row stop it", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi.fn().mockResolvedValue(res({}, false, 404));
    for (let i = 0; i < 9; i++) f.mockResolvedValueOnce(res({}, false, 404));
    f.mockResolvedValueOnce(unchanged());
    globalThis.fetch = f as unknown as typeof fetch;
    setup();
    await vi.advanceTimersByTimeAsync(3000 + 9 * 60000);
    expect(calls()).toHaveLength(10); // 9x404 then the 200
    await vi.advanceTimersByTimeAsync(6000);
    expect(calls()).toHaveLength(11); // 404 again, count restarted at 1
    await vi.advanceTimersByTimeAsync(60000);
    expect(calls()).toHaveLength(12);
  });

  it("stops for good (one warn, no error) after 10 consecutive 404/403s", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    let n = 0;
    globalThis.fetch = vi.fn(async () => res({}, false, n++ % 2 ? 403 : 404)) as unknown as typeof fetch;
    setup();
    await vi.advanceTimersByTimeAsync(3000 + 9 * 60000);
    expect(calls()).toHaveLength(10);
    expect(warn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(600000);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(calls()).toHaveLength(10);
    expect(error).not.toHaveBeenCalled();
  });

  it("a network error retries with backoff, silently", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("offline")) as unknown as typeof fetch;
    setup();
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(6000);
    expect(calls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(12000);
    expect(calls()).toHaveLength(3);
    expect(warn).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });

  it("does not poll when no version is known (old server / no stamp)", async () => {
    setup({ version: undefined });
    await vi.advanceTimersByTimeAsync(60000);
    expect(calls()).toHaveLength(0);
  });

  it("stops on unmount", async () => {
    const { unmount } = setup();
    unmount();
    await vi.advanceTimersByTimeAsync(60000);
    window.dispatchEvent(new Event("focus"));
    expect(calls()).toHaveLength(0);
  });
});

// apiPost's contract was previously exercised only indirectly (every
// meso.test.js/meso_delete.test.js/meso_undo.test.js case that hits `fetch`
// goes through it). Ported out as its own direct spec now that it's a
// standalone function instead of a `this`-bound method.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiPost } from "./api";

function res({ ok = true, status = 200, body = {} }: { ok?: boolean; status?: number; body?: unknown } = {}) {
  return { ok, status, json: async () => body };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("apiPost", () => {
  it("POSTs JSON with the CSRF header and returns the parsed body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ body: { ok: true, id: 9 } }));
    vi.stubGlobal("fetch", fetchMock);
    const data = await apiPost<{ ok: boolean; id: number }>(
      "/meso/api/plan/7/prescription/9/",
      { name: "Squat" },
      "tok",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0];
    if (!call) throw new Error("fetch was not called");
    const [url, opts] = call as [string, RequestInit & { headers: Record<string, string> }];
    expect(url).toBe("/meso/api/plan/7/prescription/9/");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRFToken"]).toBe("tok");
    expect(opts.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(opts.body as string)).toEqual({ name: "Squat" });
    expect(data).toEqual({ ok: true, id: 9 });
  });

  it("sends a null body verbatim (a bare POST) instead of the string 'null'", async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ body: {} }));
    vi.stubGlobal("fetch", fetchMock);
    await apiPost("/meso/api/plan/7/prescription/9/delete/", null, "tok");
    const call = fetchMock.mock.calls[0];
    if (!call) throw new Error("fetch was not called");
    const opts = call[1] as RequestInit;
    expect(opts.body).toBe(null);
  });

  it("throws on a non-ok response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res({ ok: false, status: 500 })));
    await expect(apiPost("/x/", null, "tok")).rejects.toThrow("Request failed: 500");
  });
});

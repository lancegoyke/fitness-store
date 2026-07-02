// Tests for the deliver screen (app/store_project/static/js/meso_deliver.js).
//
// Focus: deliver() — the one POST this screen makes. It stamps the target week
// (or none, for "current") on the plan's deliver endpoint and flips delivered /
// sending / error. No offline queue or retry logic here (unlike the athlete
// logger) — a failed send just re-arms the button.

import { createMesoDeliver } from "../app/store_project/static/js/meso_deliver.js";

const PLAN_ID = 7;
const CSRF = "tok";

// Build a fetch Response stub. Deliver only ever checks `.ok` / `.status`.
function res({ ok = true, status = 200 } = {}) {
  return { ok, status };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("createMesoDeliver: initial state", () => {
  it("hydrates planId/csrf/weekId from the constructor args", () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, 42);
    expect(c.planId).toBe(PLAN_ID);
    expect(c.csrf).toBe(CSRF);
    expect(c.weekId).toBe(42);
  });

  it("starts not delivered, not sending, no error", () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    expect(c.delivered).toBe(false);
    expect(c.sending).toBe(false);
    expect(c.error).toBe(false);
  });
});

describe("deliver", () => {
  it("POSTs to the plan's deliver endpoint with CSRF + JSON headers", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    global.fetch = vi.fn().mockResolvedValue(res());
    await c.deliver();
    expect(global.fetch).toHaveBeenCalledWith(
      `/meso/api/plan/${PLAN_ID}/deliver/`,
      expect.objectContaining({
        method: "POST",
        headers: {
          "X-CSRFToken": CSRF,
          "Content-Type": "application/json",
        },
      }),
    );
  });

  it("sends the targeted week id in the body when weekId is set", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, 42);
    global.fetch = vi.fn().mockResolvedValue(res());
    await c.deliver();
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body).toEqual({ week_id: 42 });
  });

  it("sends an empty body when weekId is null (deliver the current week)", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    global.fetch = vi.fn().mockResolvedValue(res());
    await c.deliver();
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body).toEqual({});
  });

  it("sets sending true while the request is in flight, false after", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    let sendingDuringFetch;
    global.fetch = vi.fn().mockImplementation(async () => {
      sendingDuringFetch = c.sending;
      return res();
    });
    await c.deliver();
    expect(sendingDuringFetch).toBe(true);
    expect(c.sending).toBe(false);
  });

  it("marks delivered on success", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    global.fetch = vi.fn().mockResolvedValue(res());
    await c.deliver();
    expect(c.delivered).toBe(true);
    expect(c.error).toBe(false);
  });

  it("clears a previous error at the start of a fresh attempt", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    c.error = true;
    global.fetch = vi.fn().mockResolvedValue(res());
    await c.deliver();
    expect(c.error).toBe(false);
  });

  it("surfaces an HTTP error and does not mark delivered", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    await c.deliver();
    expect(c.error).toBe(true);
    expect(c.delivered).toBe(false);
    expect(c.sending).toBe(false);
    expect(console.error).toHaveBeenCalledWith(
      "Deliver failed",
      expect.any(Error),
    );
  });

  it("surfaces a network failure the same way as an HTTP error", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await c.deliver();
    expect(c.error).toBe(true);
    expect(c.delivered).toBe(false);
    expect(c.sending).toBe(false);
  });

  it("leaves an earlier successful delivery marked delivered if a redelivery fails", async () => {
    const c = createMesoDeliver(PLAN_ID, CSRF, null);
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue(res());
    await c.deliver();
    expect(c.delivered).toBe(true);

    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    await c.deliver();
    expect(c.error).toBe(true);
    expect(c.delivered).toBe(true); // failure doesn't unset a prior success
  });
});

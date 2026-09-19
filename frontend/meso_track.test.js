// Tests for the client beacon (app/store_project/static/js/meso_track.js).
//
// Focus: window.mesoTrack() is the one way anything in the browser reports a
// closed-set client event (push_permission, pwa_installed, ...) to the
// analytics beacon (`meso:track_beacon`, analytics/views.py::track_beacon).
// It reads its config from the same #meso-pwa-config span meso_push.js
// reads, and — since it's a courtesy to analytics, not something a caller
// should have to guard or block on — must never reject or throw: a rejected
// fetch, a non-OK response, and a missing/misconfigured span are all silent
// no-ops that still resolve.

import { mesoTrack } from "../app/store_project/static/js/meso_track.js";

function setConfig({ url = "/meso/api/track/", csrf = "tok" } = {}) {
  document.body.innerHTML =
    '<span id="meso-pwa-config" hidden ' +
    (url ? `data-track-url="${url}" ` : "") +
    `data-csrf="${csrf}"></span>`;
}

beforeEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("mesoTrack: the POST", () => {
  it("sends the configured URL, JSON content-type, CSRF header, and {name, props} body", async () => {
    setConfig({ url: "/meso/api/track/", csrf: "tok123" });
    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 204 });

    await mesoTrack("push_permission", { result: "granted" });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/meso/api/track/");
    expect(opts.method).toBe("POST");
    expect(opts.headers).toEqual({
      "Content-Type": "application/json",
      "X-CSRFToken": "tok123",
    });
    expect(JSON.parse(opts.body)).toEqual({
      name: "push_permission",
      props: { result: "granted" },
    });
  });

  it("defaults props to {} when called with none", async () => {
    setConfig();
    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 204 });

    await mesoTrack("push_clicked");

    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body).toEqual({ name: "push_clicked", props: {} });
  });

  it("falls back to an empty CSRF token when data-csrf is absent", async () => {
    document.body.innerHTML =
      '<span id="meso-pwa-config" hidden data-track-url="/meso/api/track/"></span>';
    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 204 });

    await mesoTrack("pwa_installed", { via: "standalone" });

    expect(global.fetch.mock.calls[0][1].headers["X-CSRFToken"]).toBe("");
  });

  it("returns a promise a caller can await for the request to settle", async () => {
    setConfig();
    let resolveFetch;
    global.fetch = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );
    let settled = false;
    const p = mesoTrack("pwa_installed", { via: "standalone" }).then(() => {
      settled = true;
    });
    expect(settled).toBe(false);
    resolveFetch({ ok: true, status: 204 });
    await p;
    expect(settled).toBe(true);
  });
});

describe("mesoTrack: never throws or rejects", () => {
  it("resolves rather than rejecting when fetch itself rejects (offline)", async () => {
    setConfig();
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(
      mesoTrack("pwa_installed", { via: "appinstalled" }),
    ).resolves.toBeUndefined();
  });

  it("resolves when the response is a non-OK status (400/429/...) without inspecting it", async () => {
    setConfig();
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 429 });

    // Nothing on the page reads a beacon's answer — the call isn't even
    // inspected for .ok, so a 429 doesn't need special handling to resolve
    // (as opposed to rejecting). If mesoTrack rejected here, this await
    // would throw and fail the test.
    await mesoTrack("push_permission", { result: "denied" });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("mesoTrack: inert no-op without a usable config", () => {
  it("does not call fetch when the #meso-pwa-config span is absent", async () => {
    document.body.innerHTML = "";
    global.fetch = vi.fn();

    const result = await mesoTrack("pwa_installed", { via: "standalone" });

    expect(global.fetch).not.toHaveBeenCalled();
    expect(result).toBeUndefined();
  });

  it("does not call fetch when data-track-url is missing from the span", async () => {
    document.body.innerHTML =
      '<span id="meso-pwa-config" hidden data-csrf="tok"></span>';
    global.fetch = vi.fn();

    await mesoTrack("pwa_installed", { via: "standalone" });

    expect(global.fetch).not.toHaveBeenCalled();
  });
});

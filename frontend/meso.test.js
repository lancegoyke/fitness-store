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

// A meso component in Group mode with two members and one shared row, wired
// enough to drive the in-grid per-athlete override editor without Alpine/DOM.
function makeGroupMeso(overrides = {}) {
  const c = makeMeso();
  c.mode = "group";
  c.group = {
    id: 3,
    name: "Squad",
    members: [
      { id: "a1", name: "Maya Okonkwo", initials: "MO" },
      { id: "a2", name: "Aaron Adams", initials: "AA" },
    ],
  };
  return Object.assign(c, overrides);
}

// A shared-program row Maya already adjusts (load 90% of a 100kg base).
function groupRow(overrides = {}) {
  return Object.assign(
    {
      id: 11,
      name: "Back Squat",
      sets: "3",
      reps: "10",
      load: "100",
      adj: "MO -10%",
      adjusts: [
        {
          id: "a1",
          name: "Maya Okonkwo",
          initials: "MO",
          label: "-10%",
          swap: "",
          load_pct: 90,
          sets: "",
          reps: "",
          note: "",
        },
      ],
    },
    overrides,
  );
}

// The body sent on the nth fetch call, parsed back from JSON.
function sentBody(n = 0) {
  return JSON.parse(global.fetch.mock.calls[n][1].body);
}

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

describe("override editor", () => {
  it("opens on a shared row, selecting the first member with their stored diff", () => {
    const c = makeGroupMeso();
    const ex = groupRow();
    c.openOverride(ex);
    expect(c.override).not.toBe(null);
    expect(c.override.ex).toBe(ex);
    expect(c.override.memberId).toBe("a1");
    // Maya's stored 90% pre-fills the draft (as a string for the text input).
    expect(c.override.draft.load_pct).toBe("90");
    expect(c.override.draft.swap).toBe("");
    expect(c.overrideHasExisting).toBe(true);
  });

  it("blanks the draft when switching to a member with no adjust", () => {
    const c = makeGroupMeso();
    c.openOverride(groupRow());
    c.selectOverrideMember("a2");
    expect(c.override.memberId).toBe("a2");
    expect(c.override.draft.load_pct).toBe("");
    expect(c.override.draft.swap).toBe("");
    expect(c.overrideHasExisting).toBe(false);
  });

  it("is a no-op outside group mode or with no members", () => {
    const indiv = makeMeso();
    indiv.openOverride(groupRow());
    expect(indiv.override == null).toBe(true);

    const empty = makeGroupMeso({ group: { id: 3, name: "Squad", members: [] } });
    empty.openOverride(groupRow());
    expect(empty.override == null).toBe(true);
  });

  it("saves a member's adjust, posts the full diff, and repaints the row", async () => {
    const c = makeGroupMeso();
    const ex = groupRow();
    global.fetch = vi.fn().mockResolvedValue(
      res({
        body: {
          adj: "2 adjusts",
          adjusts: [{ id: "a1" }, { id: "a2" }],
        },
      }),
    );
    c.openOverride(ex);
    c.selectOverrideMember("a2");
    c.override.draft.swap = "Box Squat";
    c.override.draft.load_pct = "85";
    await c.saveOverride();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch.mock.calls[0][0]).toBe(
      "/meso/api/plan/7/prescription/11/override/",
    );
    expect(sentBody()).toEqual({
      athlete: "a2",
      swap: "Box Squat",
      load_pct: 85,
      sets: "",
      reps: "",
      note: "",
    });
    // The reply repaints the badge and closes the editor.
    expect(ex.adj).toBe("2 adjusts");
    expect(ex.adjusts).toHaveLength(2);
    expect(c.override).toBe(null);
  });

  it("sends load_pct null when the field is left blank", async () => {
    const c = makeGroupMeso();
    global.fetch = vi.fn().mockResolvedValue(res({ body: { adj: null, adjusts: [] } }));
    c.openOverride(groupRow());
    c.selectOverrideMember("a2");
    c.override.draft.note = "tempo";
    await c.saveOverride();
    expect(sentBody().load_pct).toBe(null);
    expect(sentBody().note).toBe("tempo");
  });

  it("rejects a non-numeric or out-of-band load% without posting", async () => {
    const c = makeGroupMeso();
    global.fetch = vi.fn();
    c.openOverride(groupRow());
    c.override.draft.load_pct = "abc";
    await c.saveOverride();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(c.override.error).toMatch(/Load %/);

    c.override.draft.load_pct = "500";
    await c.saveOverride();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("clears a member's adjust and repaints the row", async () => {
    const c = makeGroupMeso();
    const ex = groupRow();
    global.fetch = vi.fn().mockResolvedValue(res({ body: { adj: null, adjusts: [] } }));
    c.openOverride(ex);
    await c.clearOverride();
    expect(sentBody()).toEqual({ athlete: "a1", clear: true });
    expect(ex.adj).toBe(null);
    expect(ex.adjusts).toEqual([]);
    expect(c.override).toBe(null);
  });

  it("ignores a dismiss while a save is in flight", () => {
    // Escape / backdrop both route through closeOverride; if it nulled the
    // editor mid-save, a later save failure would throw setting override.error.
    const c = makeGroupMeso();
    c.openOverride(groupRow());
    c.override.saving = true;
    c.closeOverride();
    expect(c.override).not.toBe(null);
  });

  it("keeps the editor open and surfaces an error when the save fails", async () => {
    const c = makeGroupMeso();
    const ex = groupRow();
    vi.spyOn(console, "error").mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue(res({ ok: false, status: 500 }));
    c.openOverride(ex);
    c.override.draft.load_pct = "80";
    await c.saveOverride();
    expect(c.override).not.toBe(null);
    expect(c.override.error).toMatch(/Couldn't save/);
    expect(ex.adj).toBe("MO -10%"); // unchanged
  });
});

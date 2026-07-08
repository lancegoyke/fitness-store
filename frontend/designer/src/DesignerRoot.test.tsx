// Specs for DesignerRoot (CONTRACT.md "DesignerRoot") — this PR (Phase 2 PR
// B) replaces the PR-A placeholder smoke test with real hydration coverage:
// reads #meso-plan-data / #meso-chat-thread / #meso-csrf / #meso-designer-
// flags json_script elements the same way designer.html's `init()` did, then
// composes every hook and renders the tree. Absent/malformed payload is the
// no-op-without-a-plan guard ported from init()'s early return.
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DesignerRoot } from "./DesignerRoot";

function jsonScript(id: string, data: unknown) {
  const el = document.createElement("script");
  el.type = "application/json";
  el.id = id;
  el.textContent = JSON.stringify(data);
  document.body.appendChild(el);
}

function csrfSpan(token = "tok") {
  const el = document.createElement("span");
  el.id = "meso-csrf";
  el.dataset.token = token;
  el.hidden = true;
  document.body.appendChild(el);
}

function planPayload(overrides: Record<string, unknown> = {}) {
  return {
    plan: { id: 7, title: "Maya's plan", status: "active", unit: "kg" },
    group: null,
    athlete: { name: "Maya Okonkwo", initials: "MO", goal: "Strength", contraindications: [] },
    program: [
      {
        id: 1,
        n: 1,
        name: "Lower",
        bias: "Quad bias",
        exercises: [{ id: 9, name: "Squat", sets: "3", reps: "5", load: "100", load_type: "abs" }],
      },
    ],
    weeks: [{ id: 1, index: 1, label: "Wk 1", current: true }],
    phases: [{ name: "Hypertrophy", weeks: "4 wk", state: "current" }],
    viewing: 1,
    history: { can_undo: false, can_redo: false, undo_label: null, redo_label: null },
    ...overrides,
  };
}

function flagsPayload(overrides: Record<string, unknown> = {}) {
  return {
    is_sandbox: false,
    can_use_agent: true,
    agent_allowance: { metered: false, allowance: 0, remaining: null, can_use: true, tier: "unlimited" },
    signup_url: "/meso/sandbox/signup/",
    price_summary: "$19/mo — unlimited athletes",
    ...overrides,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

describe("hydration: full payload", () => {
  it("renders the grid from #meso-plan-data / #meso-chat-thread / #meso-csrf / #meso-designer-flags", () => {
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan("tok123");
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByTestId("exercise-name-9")).toHaveValue("Squat");
    // The athlete's name legitimately renders twice — the top-bar identity
    // chip AND the left rail — exactly as the Alpine template it mirrors did.
    expect(screen.getAllByText("Maya Okonkwo").length).toBeGreaterThan(0);
    expect(screen.getByTestId("week-chip-1")).toBeInTheDocument();
  });

  it("hydrates the chat thread from #meso-chat-thread when present", () => {
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", [{ id: 1, role: "agent", text: "Persisted greeting" }]);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByText("Persisted greeting")).toBeInTheDocument();
  });

  it("falls back to the default greeting when the thread is empty/absent", () => {
    jsonScript("meso-plan-data", planPayload());
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByText(/Tell me how you'd like to adjust this plan/)).toBeInTheDocument();
  });

  it("uses a group-aware greeting when the plan has a group and no persisted thread", () => {
    jsonScript(
      "meso-plan-data",
      planPayload({
        group: { id: 3, name: "Squad", member_count: 1, members: [{ id: "a1", name: "Maya", initials: "MO" }], flags: [] },
        athlete: null,
      }),
    );
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByText(/group's shared program/)).toBeInTheDocument();
  });
});

// === P1 (multi-week table): #meso-grid-data hydration + the new default
// view. `view` gains a "table" member (frontend/designer/CONTRACT.md predates
// this — see the P1 spec instead); the default `view` becomes "table" when
// grid data is hydrated, falling back to today's "week" default otherwise —
// every OTHER describe block in this file never hydrates #meso-grid-data, so
// none of them needed touching.
function gridPayload(overrides: Record<string, unknown> = {}) {
  return {
    mesocycle: { id: 1, plan_id: 7, name: "Block 1", week_count: 1 },
    weeks: [{ id: 1, index: 0, label: "Wk 1", phase: "Accum", deload: false, current: true, delivered_at: null }],
    days: [
      {
        session_slot_id: 1,
        session_id: 11,
        day_number: 1,
        name: "Lower",
        bias: "Quad bias",
        order: 0,
        rows: [
          {
            exercise_slot_id: 9,
            name: "Squat",
            exercise_id: 55,
            order: 0,
            tags: [],
            cells: {
              "1": {
                prescription_id: 100,
                sets: "3",
                reps: "5",
                load: "100",
                load_type: "abs",
                rpe: "8",
                rest: "90",
                note: "",
                skipped: false,
                swap_name: "",
                swap_exercise_id: null,
                swap_display: "",
              },
            },
          },
        ],
      },
    ],
    history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" },
    ...overrides,
  };
}

describe("hydration: #meso-grid-data / P1 table view default", () => {
  it("defaults to the table view when #meso-grid-data is present", () => {
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    jsonScript("meso-grid-data", gridPayload());

    render(<DesignerRoot />);

    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
    expect(screen.getByTestId("meso-day-table-1")).toBeInTheDocument();
  });

  it("falls back to the week view when #meso-grid-data is absent", () => {
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.queryByTestId("meso-table-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("exercise-name-9")).toBeInTheDocument();
  });

  it("can switch from the table view to the week view and back via the segmented control", async () => {
    const user = userEvent.setup();
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    jsonScript("meso-grid-data", gridPayload());

    render(<DesignerRoot />);
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();

    await user.click(screen.getByText("This week"));
    expect(screen.queryByTestId("meso-table-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("exercise-name-9")).toBeInTheDocument();

    await user.click(screen.getByText("Table"));
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
  });

  it("switching the main view refetches the newly-activated view's data source, so neither view shows stale state", async () => {
    // The table (gridState) and "This week" (planData) are two sibling data
    // owners — an edit made in one must not leave the other showing stale
    // server state after the coach switches back to it. The grid's CURRENT
    // week (id 2) is deliberately different from planData's own viewedWeekId
    // (id 1, from planPayload's `viewing: 1`) — reactivating "This week" must
    // target the GRID's current week (the table may have removed the week
    // planData last viewed), not planData's possibly-stale/deleted one.
    const user = userEvent.setup();
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    jsonScript(
      "meso-grid-data",
      gridPayload({
        weeks: [
          { id: 1, index: 0, label: "Wk 1", phase: "Accum", deload: false, current: false, delivered_at: null },
          { id: 2, index: 1, label: "Wk 2", phase: "Accum", deload: false, current: true, delivered_at: null },
        ],
      }),
    );

    const weekReply = {
      ok: true,
      program: [
        {
          id: 1,
          n: 1,
          name: "Lower",
          exercises: [{ id: 9, name: "Squat", sets: "3", reps: "5", load: "100", load_type: "abs" }],
        },
      ],
      weeks: [{ id: 2, index: 2, label: "Wk 2", current: true }],
      phases: [{ name: "Hypertrophy", weeks: "4 wk", state: "current" }],
      viewing: 2,
    };
    const gridReply = {
      ok: true,
      ...gridPayload({
        weeks: [
          { id: 1, index: 0, label: "Wk 1", phase: "Accum", deload: false, current: false, delivered_at: null },
          { id: 2, index: 1, label: "Wk 2", phase: "Accum", deload: false, current: true, delivered_at: null },
        ],
      }),
    };
    const fetchMock = vi.fn((url: string) => {
      const body = url.includes("/grid/") ? gridReply : weekReply;
      return Promise.resolve({ ok: true, status: 200, json: async () => body });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<DesignerRoot />);
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled(); // no refetch on initial mount

    await user.click(screen.getByText("This week"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/week/2/"));
    expect(fetchMock).not.toHaveBeenCalledWith("/meso/api/plan/7/week/1/");

    await user.click(screen.getByText("Table"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/grid/"));
  });

  it("targets the deliver link at the grid's CURRENT week while the table view is active, not planData's viewedWeekId", () => {
    jsonScript("meso-plan-data", planPayload()); // viewing: 1 (planData's viewed week)
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    jsonScript(
      "meso-grid-data",
      gridPayload({
        weeks: [
          { id: 1, index: 0, label: "Wk 1", phase: "Accum", deload: false, current: false, delivered_at: null },
          { id: 2, index: 1, label: "Wk 2", phase: "Accum", deload: false, current: true, delivered_at: null },
        ],
      }),
    );

    render(<DesignerRoot />);

    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
    expect(screen.getByTestId("deliver-link")).toHaveAttribute("href", "/meso/deliver/7/?week=2");
  });

  // Code-review nit: the global Ctrl/Cmd+Z window listener lives inside
  // useUndoRedo and always called ITS OWN planData undo/redo — even while
  // the table (gridState) is the active view, leaving the visible table
  // stale after an undo. DesignerRoot now overrides useUndoRedo's keyboard
  // handlers to the grid's own undo/redo while view === "table". Both
  // planData.undo and gridState.undo POST to the same `/undo/` endpoint, so
  // the distinguishing signal that the GRID path ran is the follow-up GET
  // to `/grid/` (gridState.undo refetches the grid; planData's undo does
  // not).
  it("routes a Ctrl/Cmd+Z keyboard shortcut to the grid's own undo while the table view is active", async () => {
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    jsonScript(
      "meso-grid-data",
      gridPayload({
        history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: null },
      }),
    );

    const gridReply = {
      ok: true,
      ...gridPayload({
        history: { can_undo: false, can_redo: true, undo_label: null, redo_label: "Edited Squat" },
      }),
    };
    const fetchMock = vi.fn(() => {
      return Promise.resolve({ ok: true, status: 200, json: async () => gridReply });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<DesignerRoot />);
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/undo/", expect.anything()),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/grid/"));
  });

  it("console.errors and does not crash on malformed grid JSON, still rendering the week view", () => {
    jsonScript("meso-plan-data", planPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    const el = document.createElement("script");
    el.type = "application/json";
    el.id = "meso-grid-data";
    el.textContent = "{not valid json";
    document.body.appendChild(el);
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(<DesignerRoot />);

    expect(spy).toHaveBeenCalled();
    expect(screen.getByTestId("exercise-name-9")).toBeInTheDocument();
  });
});

describe("hydration: missing or malformed payload", () => {
  it("renders nothing when #meso-plan-data is absent", () => {
    const { container } = render(<DesignerRoot />);
    expect(container).toBeEmptyDOMElement();
  });

  it("console.errors and does not crash on malformed plan JSON", () => {
    const el = document.createElement("script");
    el.type = "application/json";
    el.id = "meso-plan-data";
    el.textContent = "{not valid json";
    document.body.appendChild(el);
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { container } = render(<DesignerRoot />);

    expect(spy).toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });
});

// === Phase 3: grid keyboard navigation — RED (../hooks/useGridNav does not
// exist; WeekGrid does not call it yet). DesignerRoot.tsx itself needs NO
// code changes for this PR (useGridNav is instantiated inside WeekGrid,
// which already receives `program` from usePlanData) — these specs mount
// the full app because focus-restoration-across-applyPlanData needs a real
// undo() round trip (usePlanData + useUndoRedo), and the undo-key
// regression needs the real window keydown listener (useUndoRedo).
//
// Restoration tiers tested here are ONLY the three FALLBACKS (spec: "else
// focus the first cell of the same day ... else the grid's first cell ...
// else nothing") — tier 1 ("re-focus the same prescriptionId+column")
// is deliberately NOT pinned at this level: React's keyed reconciliation
// already keeps the same <input> DOM node (and thus its native focus)
// across a same-shape program swap with no restoration code involved, so
// an integration spec for it can't fail today for the right reason (it's
// covered, and IS discriminating, at the hook level in
// hooks/useGridNav.test.tsx). Tiers 2/3 genuinely unmount the previously-
// focused input (its row/day disappears from the array), which drops
// focus to <body> today with nothing to restore it — that's what makes
// them real red specs here.
function twoDayGridProgram() {
  return [
    {
      id: 1,
      n: 1,
      name: "Lower",
      exercises: [
        { id: 9, name: "Box Squat", sets: "3", reps: "5", load: "100", load_type: "abs" },
        { id: 10, name: "RDL", sets: "3", reps: "8", load: "80", load_type: "abs" },
      ],
    },
    {
      id: 2,
      n: 2,
      name: "Upper",
      exercises: [{ id: 11, name: "Bench", sets: "3", reps: "5", load: "70", load_type: "abs" }],
    },
  ];
}

function mountGridPlan() {
  jsonScript(
    "meso-plan-data",
    planPayload({
      program: twoDayGridProgram(),
      history: { can_undo: true, can_redo: false, undo_label: "Edited Box Squat", redo_label: null },
    }),
  );
  jsonScript("meso-chat-thread", []);
  csrfSpan("tok123");
  jsonScript("meso-designer-flags", flagsPayload());
  render(<DesignerRoot />);
}

function mockUndoReply(program: unknown[]) {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      ok: true,
      program,
      weeks: [{ id: 1, index: 1, label: "Wk 1", current: true }],
      phases: [{ name: "Hypertrophy", weeks: "4 wk", state: "current" }],
      viewing: 1,
      // can_undo stays true: the multi-undo restoration specs click the undo
      // button repeatedly, and a can_undo:false reply would disable it after
      // the first click (userEvent.click on a disabled button never fires).
      history: { can_undo: true, can_redo: true, undo_label: "Edited RDL", redo_label: "Edited Box Squat" },
    }),
  }) as unknown as typeof fetch;
}

describe("Phase 3: grid focus restoration across an applyPlanData swap (undo)", () => {
  it("tier: falls back to the day's first cell when the focused prescription is gone but its day survives", async () => {
    const user = userEvent.setup();
    mountGridPlan();
    await user.click(screen.getByTestId("exercise-sets-9"));

    // Exercise 9 is gone from day 1 (undone); exercise 10 remains.
    mockUndoReply([
      { id: 1, n: 1, name: "Lower", exercises: [{ id: 10, name: "RDL", sets: "3", reps: "8", load: "80", load_type: "abs" }] },
      { id: 2, n: 2, name: "Upper", exercises: [{ id: 11, name: "Bench", sets: "3", reps: "5", load: "70", load_type: "abs" }] },
    ]);
    await user.click(screen.getByTestId("undo-button"));

    await waitFor(() => expect(screen.getByTestId("exercise-name-10")).toHaveFocus());
  });

  it("tier: falls back to the grid's first cell when the focused day itself is gone; a further swap that empties the grid does not crash", async () => {
    const user = userEvent.setup();
    mountGridPlan();
    await user.click(screen.getByTestId("exercise-sets-11")); // day 2's only exercise

    // Day 2 is gone entirely (undone); only day 1 remains.
    mockUndoReply([
      {
        id: 1,
        n: 1,
        name: "Lower",
        exercises: [
          { id: 9, name: "Box Squat", sets: "3", reps: "5", load: "100", load_type: "abs" },
          { id: 10, name: "RDL", sets: "3", reps: "8", load: "80", load_type: "abs" },
        ],
      },
    ]);
    await user.click(screen.getByTestId("undo-button"));

    await waitFor(() => expect(screen.getByTestId("exercise-name-9")).toHaveFocus());

    // A further swap that empties the whole grid: "else nothing" — must not
    // throw (an uncaught error here would fail this test), and must not
    // leave a stale reference to a removed element.
    mockUndoReply([]);
    await user.click(screen.getByTestId("undo-button"));
    await waitFor(() => expect(screen.queryByTestId("exercise-name-9")).not.toBeInTheDocument());
  });
});

describe("Phase 3 regression: grid inputs still suppress global undo (handleUndoKey)", () => {
  it("once a cell has its own keydown wiring, Ctrl+Z there still leaves native field undo alone (no undo POST)", async () => {
    const user = userEvent.setup();
    mountGridPlan();
    await user.click(screen.getByTestId("exercise-sets-9"));
    // Sanity: this proves the cell truly has Phase 3's own keydown handling
    // active (not just "nothing is wired so nothing could intercept
    // anything") before trusting the negative assertion below.
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("exercise-sets-10")).toHaveFocus();

    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as unknown as typeof fetch;
    fireEvent.keyDown(screen.getByTestId("exercise-sets-10"), { key: "z", ctrlKey: true });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

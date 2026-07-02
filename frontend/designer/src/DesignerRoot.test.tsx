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
      history: { can_undo: false, can_redo: true, undo_label: null, redo_label: "Edited Box Squat" },
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

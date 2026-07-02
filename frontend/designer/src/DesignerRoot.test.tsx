// Specs for DesignerRoot (CONTRACT.md "DesignerRoot") — this PR (Phase 2 PR
// B) replaces the PR-A placeholder smoke test with real hydration coverage:
// reads #meso-plan-data / #meso-chat-thread / #meso-csrf / #meso-designer-
// flags json_script elements the same way designer.html's `init()` did, then
// composes every hook and renders the tree. Absent/malformed payload is the
// no-op-without-a-plan guard ported from init()'s early return.
import { render, screen, cleanup } from "@testing-library/react";
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
    expect(screen.getByText("Maya Okonkwo")).toBeInTheDocument();
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

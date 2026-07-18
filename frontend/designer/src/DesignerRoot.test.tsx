// Specs for DesignerRoot (CONTRACT.md "DesignerRoot") — issue #455 phase A5:
// the one-week designer (WeekStrip/WeekGrid/DayCard/ExerciseRow +
// usePlanData/useAutosave/useDeletes/useUndoRedo/useReorder/useOneRmEditor)
// is retired. useGrid (the P1 multi-week table) is now the SOLE data owner.
//
// Hydrates once from #meso-grid-data / #meso-chat-thread / #meso-csrf /
// #meso-designer-flags json_script elements. #meso-grid-data is now the
// hydration GATE (absent/unparseable -> the whole island renders nothing) —
// a plan with no mesocycle block at all is a documented "shouldn't happen
// post-scaffold" edge case (views.py's MesoDesignerView.get_context_data),
// not a graceful-degrade case anymore: there's no more one-week fallback to
// degrade to (issue #455 phase A5 product-behavior change, flagged in the
// PR description).
//
// Focus-restoration-across-a-grid-swap coverage (tiers 1/2a/2b/3/4) lives at
// the hook level now (hooks/useTableNav.test.tsx) — the old "Phase 3: grid
// focus restoration"/"Phase 3 regression" describe blocks here existed only
// to exercise useGridNav through the retired WeekGrid and are dropped, not
// ported (verified: useTableNav.test.tsx's "focus restoration across a grid
// swap" describe block already covers every tier those blocks pinned).
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

// The #meso-grid-data payload (serialize_mesocycle_grid) — the ONLY
// hydration source now. Carries plan/athlete/phases (issue #455 phase
// A5 additive serializer fields) alongside the pre-existing
// mesocycle/weeks/days/history shape.
function gridPayload(overrides: Record<string, unknown> = {}) {
  return {
    plan: { id: 7, title: "Maya's plan", goal: "Strength", status: "active", unit: "kg" },
    athlete: { name: "Maya Okonkwo", initials: "MO", goal: "Strength", contraindications: [] },
    phases: [{ name: "Hypertrophy", weeks: "4 wk", state: "current" }],
    mesocycle: { id: 1, plan_id: 7, name: "Block 1", week_count: 1 },
    weeks: [
      { id: 1, index: 0, label: "Wk 1", phase: "Accum", deload: false, delivered_at: null, vol: 70, inten: 65 },
    ],
    days: [
      {
        session_slot_id: 1,
        session_id: 11,
        session_ids: { "1": 11 },
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
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "1": {
                prescription_id: 100,
                text: "3 x 5, RPE 8, 100",
                skipped: false,
                lines: [],
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

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

describe("hydration: full payload", () => {
  it("renders the table from #meso-grid-data / #meso-chat-thread / #meso-csrf / #meso-designer-flags", () => {
    jsonScript("meso-grid-data", gridPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan("tok123");
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByTestId("row-name-9")).toHaveValue("Squat");
    // designer-simplify: the athlete's name now renders exactly once (the
    // sidebar's AthleteMeta) — the duplicate top-bar identity chip is gone.
    expect(screen.getAllByText("Maya Okonkwo")).toHaveLength(1);
    expect(screen.getByTestId("week-col-1")).toBeInTheDocument();
  });

  it("hydrates the chat thread from #meso-chat-thread when present", () => {
    jsonScript("meso-grid-data", gridPayload());
    jsonScript("meso-chat-thread", [{ id: 1, role: "agent", text: "Persisted greeting" }]);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByText("Persisted greeting")).toBeInTheDocument();
  });

  it("falls back to the default greeting when the thread is empty/absent", () => {
    jsonScript("meso-grid-data", gridPayload());
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByText(/Tell me how you'd like to adjust this plan/)).toBeInTheDocument();
  });
});

describe("table view (issue #455 phase A5: the only view left besides periodization/athlete)", () => {
  it("renders the table view by default", () => {
    jsonScript("meso-grid-data", gridPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
    expect(screen.getByTestId("meso-day-table-1")).toBeInTheDocument();
  });

  it("can switch to Periodization and Athlete view and back via the segmented control", async () => {
    const user = userEvent.setup();
    jsonScript("meso-grid-data", gridPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();

    await user.click(screen.getByText("Periodization"));
    expect(screen.queryByTestId("meso-table-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("period-style-timeline-button")).toBeInTheDocument();

    await user.click(screen.getByText("Athlete view"));
    expect(screen.queryByTestId("period-style-timeline-button")).not.toBeInTheDocument();
    // AthletePreview's phone mock renders the resolved day-0 exercise.
    expect(screen.getByText("Squat")).toBeInTheDocument();

    await user.click(screen.getByText("Table"));
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
  });

  it("clicking a Periodization timeline week switches back to the table view (issue #455 A5 product-behavior change)", async () => {
    // There's no more one-week "landing" view to preview a week into — the
    // table already shows every week as columns at once.
    const user = userEvent.setup();
    jsonScript("meso-grid-data", gridPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);
    await user.click(screen.getByText("Periodization"));
    expect(screen.getByTestId("block-week-1")).toBeInTheDocument();

    await user.click(screen.getByTestId("block-week-1"));

    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();
  });

  it("targets the deliver link at the grid's FIRST week (programs are date-less — no current-week pointer)", () => {
    jsonScript("meso-grid-data", gridPayload({
      weeks: [
        { id: 1, index: 0, label: "Wk 1", phase: "Accum", deload: false, delivered_at: null },
        { id: 2, index: 1, label: "Wk 2", phase: "Accum", deload: false, delivered_at: null },
      ],
    }));
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    expect(screen.getByTestId("deliver-link")).toHaveAttribute("href", "/meso/deliver/7/?week=1");
  });

  // §4b: the default grid opens on the plan's FIRST block by order, which
  // can have zero live weeks while a later block has some (week_delete only
  // guards the plan's LAST live week, not the block's). Nothing to deliver
  // from an empty block, so the control must be inert rather than a link
  // that falls back to DeliverView's current_week(plan) — a later block.
  it("renders Deliver inert when the viewed block has zero live weeks", () => {
    jsonScript("meso-grid-data", gridPayload({ weeks: [], days: [] }));
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    render(<DesignerRoot />);

    const deliver = screen.getByTestId("deliver-link");
    expect(deliver).not.toHaveAttribute("href");
    expect(deliver).toHaveAttribute("aria-disabled", "true");
  });

  it("routes a Ctrl/Cmd+Z keyboard shortcut to the grid's own undo (the only undo/redo owner left)", async () => {
    jsonScript(
      "meso-grid-data",
      gridPayload({
        history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: null },
      }),
    );
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());

    const gridReply = {
      ok: true,
      ...gridPayload({
        history: { can_undo: false, can_redo: true, undo_label: null, redo_label: "Edited Squat" },
      }),
    };
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 200, json: async () => gridReply }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<DesignerRoot />);
    expect(screen.getByTestId("meso-table-view")).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/undo/", expect.anything()));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/grid/"));
  });
});

// Phase 2a text-first wiring: the ghost sub-line input routes through
// gridState.writeCellLine (POST row/<slot>/cell/) and a row-column edit
// through gridState.patchRowColumns (POST row/<slot>/) — pinned here because
// MesoTable only reaches those verbs through DesignerRoot's wiring.
describe("Phase 2a: sub-line + row-column wiring", () => {
  function mountIsland() {
    jsonScript("meso-grid-data", gridPayload());
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: true }) });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<DesignerRoot />);
    return fetchMock;
  }

  it("committing the ghost sub-line input POSTs {week_id, line, text} to row/<slot>/cell/", async () => {
    const user = userEvent.setup();
    const fetchMock = mountIsland();

    await user.type(screen.getByTestId("cell-line-new-100"), "RPE 8");
    await user.tab();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/row/9/cell/", expect.anything()));
    const call = fetchMock.mock.calls.find((c) => c[0] === "/meso/api/plan/7/row/9/cell/")!;
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({ week_id: 1, line: 1, text: "RPE 8" });
    // The optimistic repaint promotes the ghost's text to a real sub-line input.
    expect(screen.getByTestId("cell-line-100-1")).toHaveValue("RPE 8");
  });

  it("committing a Tempo edit POSTs the partial patch to row/<slot>/", async () => {
    const user = userEvent.setup();
    const fetchMock = mountIsland();

    await user.type(screen.getByTestId("row-tempo-9"), "31X1");
    await user.tab();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/row/9/", expect.anything()));
    const call = fetchMock.mock.calls.find((c) => c[0] === "/meso/api/plan/7/row/9/")!;
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({ tempo: "31X1" });
  });
});

// §4b (docs/meso/remove-current-week-plan.md): the coach's viewed block
// (`gridData.mesocycle.id`) rides every agent proposal POST — grounding/
// validation run in a background job and apply on a later request, so
// neither can re-read a "current week" pointer that no longer exists.
describe("§4b: agent proposal carries the viewed block's mesocycle_id", () => {
  it("sends {instruction, mesocycle_id} sourced from the hydrated grid's mesocycle", async () => {
    const user = userEvent.setup();
    jsonScript("meso-grid-data", gridPayload({ mesocycle: { id: 42, plan_id: 7, name: "Block 1", week_count: 1 } }));
    jsonScript("meso-chat-thread", []);
    csrfSpan();
    jsonScript("meso-designer-flags", flagsPayload());
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 202, json: async () => ({ status_url: null }) });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<DesignerRoot />);
    await user.click(screen.getByTestId("agent-chip-0"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/meso/api/plan/7/agent/", expect.anything()));
    const call = fetchMock.mock.calls.find((c) => c[0] === "/meso/api/plan/7/agent/")!;
    const body = JSON.parse((call[1] as RequestInit).body as string);
    expect(body.mesocycle_id).toBe(42);
  });
});

describe("hydration: missing or malformed payload", () => {
  it("renders nothing when #meso-grid-data is absent", () => {
    const { container } = render(<DesignerRoot />);
    expect(container).toBeEmptyDOMElement();
  });

  it("console.errors and renders nothing on malformed grid JSON (issue #455 A5: #meso-grid-data is now the hydration gate — there's no more one-week payload to fall back to)", () => {
    const el = document.createElement("script");
    el.type = "application/json";
    el.id = "meso-grid-data";
    el.textContent = "{not valid json";
    document.body.appendChild(el);
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { container } = render(<DesignerRoot />);

    expect(spy).toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });
});

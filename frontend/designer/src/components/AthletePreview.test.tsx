// Specs for AthletePreview (CONTRACT.md "AthletePreview") — the phone mock's
// first-day/first-three-lifts view. Ported from meso.js's athleteDay/aTotal/
// aDone getters (now computed inside this component per the contract, since
// they're view-shaping with no existing lib coverage). Phase 2a (text-first
// cells): the prescription is one freeform string plus optional sub-lines —
// no sets count to fan set rows out from, so the mock shows ONE loggable row
// per lift (key "a0-<xi>-0") with the verbatim text as its target.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AthletePreview } from "./AthletePreview";
import type { Day } from "../lib/api";

function program(): Day[] {
  return [
    {
      id: 1,
      n: 1,
      name: "Lower · Quad bias",
      exercises: [
        { id: 1, name: "Back Squat", text: "3 x 5, 100" },
        { id: 2, name: "Leg Press", text: "2 x 10, 75%", lines: [{ line: 1, text: "RPE 8" }] },
      ],
    },
  ];
}

function baseProps(overrides: Partial<Parameters<typeof AthletePreview>[0]> = {}) {
  return {
    program: program(),
    unit: "kg",
    checks: {} as Record<string, boolean>,
    onToggleCheck: vi.fn(),
    ...overrides,
  };
}

describe("AthletePreview", () => {
  it("renders the first day's lifts with one loggable row each, keyed a0-<xi>-0", () => {
    render(<AthletePreview {...baseProps()} />);
    expect(screen.getByText("Back Squat")).toBeInTheDocument();
    expect(screen.getByText("Leg Press")).toBeInTheDocument();
    expect(screen.getByTestId("athlete-check-a0-0-0")).toBeInTheDocument();
    expect(screen.getByTestId("athlete-check-a0-1-0")).toBeInTheDocument();
  });

  it("renders the prescription text verbatim as the target", () => {
    render(<AthletePreview {...baseProps()} />);
    expect(screen.getByText("target 3 x 5, 100")).toBeInTheDocument();
    expect(screen.getByText("target 2 x 10, 75%")).toBeInTheDocument();
  });

  it("renders a lift's sub-lines under its head, skipping blank ones", () => {
    const p = program();
    p[0]!.exercises[1]!.lines = [
      { line: 1, text: "RPE 8" },
      { line: 2, text: "   " }, // blank sub-line: cleared in place, not shown
      { line: 3, text: "sub: Cable Crunch" },
    ];
    render(<AthletePreview {...baseProps({ program: p })} />);
    expect(screen.getByTestId("athlete-line-2-0")).toHaveTextContent("RPE 8");
    expect(screen.getByTestId("athlete-line-2-1")).toHaveTextContent("sub: Cable Crunch");
    expect(screen.queryByTestId("athlete-line-2-2")).not.toBeInTheDocument();
  });

  it("renders nothing (empty) when the current week has no sessions yet", () => {
    render(<AthletePreview {...baseProps({ program: [] })} />);
    expect(screen.queryByTestId(/athlete-check-/)).not.toBeInTheDocument();
  });

  it("toggleCheck fires onToggleCheck with the row's key and reflects `checks`", async () => {
    const user = userEvent.setup();
    const onToggleCheck = vi.fn();
    render(<AthletePreview {...baseProps({ onToggleCheck })} />);
    await user.click(screen.getByTestId("athlete-check-a0-0-0"));
    expect(onToggleCheck).toHaveBeenCalledWith("a0-0-0");
  });

  it("shows a done set as checked when `checks` marks it true", () => {
    render(<AthletePreview {...baseProps({ checks: { "a0-0-0": true } })} />);
    const done = screen.getByTestId("athlete-check-a0-0-0");
    // The done indicator renders a check glyph; assert it differs from an
    // undone row's rendering rather than assuming a specific DOM shape.
    const undone = screen.getByTestId("athlete-check-a0-1-0");
    expect(done.innerHTML).not.toBe(undone.innerHTML);
  });

  it("does not render a skipped exercise, but keeps a non-skipped one", () => {
    const p = program();
    p[0]!.exercises.push({
      id: 3,
      name: "Leg Curl",
      text: "3 x 12, 40",
      skipped: true,
    });
    render(<AthletePreview {...baseProps({ program: p })} />);
    expect(screen.getByText("Back Squat")).toBeInTheDocument();
    expect(screen.getByText("Leg Press")).toBeInTheDocument();
    expect(screen.queryByText("Leg Curl")).not.toBeInTheDocument();
  });

  it("only considers the first three exercises of the first day", () => {
    const p = program();
    p[0]!.exercises.push(
      { id: 3, name: "Leg Curl", text: "3 x 12, 40" },
      { id: 4, name: "Calf Raise", text: "3 x 15, 20" },
    );
    render(<AthletePreview {...baseProps({ program: p })} />);
    expect(screen.getByText("Back Squat")).toBeInTheDocument();
    expect(screen.getByText("Leg Press")).toBeInTheDocument();
    expect(screen.getByText("Leg Curl")).toBeInTheDocument();
    expect(screen.queryByText("Calf Raise")).not.toBeInTheDocument();
  });
});

describe("AthletePreview phone coachmark", () => {
  // Parity with the Alpine template's dismissible first-run note ("Preview as
  // your athlete") — same coachmark plumbing as WeekGrid's "grid" key.
  function coachmarkProps(visible: boolean) {
    return baseProps({
      coachmarkVisible: vi.fn((key: string) => visible && key === "phone"),
      dismissCoachmark: vi.fn(),
    });
  }

  it("shows the phone coachmark until dismissed", () => {
    render(<AthletePreview {...coachmarkProps(true)} />);
    expect(screen.getByText("Preview as your athlete")).toBeInTheDocument();
  });

  it("hides the coachmark when dismissed", () => {
    render(<AthletePreview {...coachmarkProps(false)} />);
    expect(screen.queryByText("Preview as your athlete")).not.toBeInTheDocument();
  });

  it("the dismiss button reports the phone key", async () => {
    const user = userEvent.setup();
    const props = coachmarkProps(true);
    render(<AthletePreview {...props} />);
    await user.click(screen.getByLabelText("Dismiss tip"));
    expect(props.dismissCoachmark).toHaveBeenCalledWith("phone");
  });
});

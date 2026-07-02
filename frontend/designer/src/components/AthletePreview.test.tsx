// Specs for AthletePreview (CONTRACT.md "AthletePreview") — the phone mock's
// first-day/first-three-lifts view. Ported from meso.js's athleteDay/aTotal/
// aDone getters (now computed inside this component per the contract, since
// they're view-shaping with no existing lib coverage).
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
        { id: 1, name: "Back Squat", sets: "3", reps: "5", load: "100", load_type: "abs" },
        { id: 2, name: "Leg Press", sets: "2", reps: "10", load: "75", load_type: "pct" },
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
  it("renders the first day's first three lifts with per-set rows", () => {
    render(<AthletePreview {...baseProps()} />);
    expect(screen.getByText("Back Squat")).toBeInTheDocument();
    expect(screen.getByText("Leg Press")).toBeInTheDocument();
    // Squat: 3 sets → keys a0-0-0, a0-0-1, a0-0-2.
    expect(screen.getByTestId("athlete-check-a0-0-0")).toBeInTheDocument();
    expect(screen.getByTestId("athlete-check-a0-0-2")).toBeInTheDocument();
    // Leg Press: 2 sets → keys a0-1-0, a0-1-1.
    expect(screen.getByTestId("athlete-check-a0-1-0")).toBeInTheDocument();
    expect(screen.getByTestId("athlete-check-a0-1-1")).toBeInTheDocument();
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
    const undone = screen.getByTestId("athlete-check-a0-0-1");
    expect(done.innerHTML).not.toBe(undone.innerHTML);
  });

  it("only considers the first three exercises of the first day", () => {
    const p = program();
    p[0]!.exercises.push(
      { id: 3, name: "Leg Curl", sets: "3", reps: "12", load: "40", load_type: "abs" },
      { id: 4, name: "Calf Raise", sets: "3", reps: "15", load: "20", load_type: "abs" },
    );
    render(<AthletePreview {...baseProps({ program: p })} />);
    expect(screen.getByText("Back Squat")).toBeInTheDocument();
    expect(screen.getByText("Leg Press")).toBeInTheDocument();
    expect(screen.getByText("Leg Curl")).toBeInTheDocument();
    expect(screen.queryByText("Calf Raise")).not.toBeInTheDocument();
  });
});

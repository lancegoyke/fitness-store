// Specs for LeftRail (CONTRACT.md "LeftRail") — athlete identity block
// + macrocycle phase list; "Open plan →" calls onOpenBlockView.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LeftRail } from "./LeftRail";
import type { AthleteIdentity, Phase } from "../lib/api";

const athlete: AthleteIdentity = {
  name: "Maya Okonkwo",
  initials: "MO",
  goal: "Strength",
  contraindications: [{ text: "Knee-sensitive" }],
};
const phases: Phase[] = [
  { name: "Accumulation", weeks: "4 wk", state: "done" },
  { name: "Hypertrophy", weeks: "4 wk", state: "current" },
  { name: "Peak", weeks: "2 wk", state: "next" },
];

function baseProps(overrides: Partial<Parameters<typeof LeftRail>[0]> = {}) {
  return {
    athlete,
    phases,
    onOpenBlockView: vi.fn(),
    ...overrides,
  };
}

describe("LeftRail", () => {
  it("renders the athlete's identity, goal, and contraindications", () => {
    render(<LeftRail {...baseProps()} />);
    expect(screen.getByText("Maya Okonkwo")).toBeInTheDocument();
    expect(screen.getByText("Knee-sensitive")).toBeInTheDocument();
  });

  it("renders every phase with its state", () => {
    render(<LeftRail {...baseProps()} />);
    expect(screen.getByText("Accumulation")).toBeInTheDocument();
    expect(screen.getByText("Hypertrophy")).toBeInTheDocument();
    expect(screen.getByText("Peak")).toBeInTheDocument();
  });

  it("renders a 'No blocks yet.' fallback with no phases", () => {
    render(<LeftRail {...baseProps({ phases: [] })} />);
    expect(screen.getByText("No blocks yet.")).toBeInTheDocument();
  });

  it("calls onOpenBlockView from the 'Open plan →' button", async () => {
    const user = userEvent.setup();
    const onOpenBlockView = vi.fn();
    render(<LeftRail {...baseProps({ onOpenBlockView })} />);
    await user.click(screen.getByTestId("open-block-view-button"));
    expect(onOpenBlockView).toHaveBeenCalledTimes(1);
  });

  it("shows 'None noted.' when the athlete has no contraindications", () => {
    render(<LeftRail {...baseProps({ athlete: { ...athlete, contraindications: [] } })} />);
    expect(screen.getByText("None noted.")).toBeInTheDocument();
  });
});

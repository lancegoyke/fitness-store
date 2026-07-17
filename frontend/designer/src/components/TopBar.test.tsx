// Specs for TopBar (CONTRACT.md "TopBar") — identity chip, cycle label,
// preview/review/deliver actions. (The individual/group mode segmented
// control and the group "Deliver to all · soon" chip went with the group
// subsystem — the designer is single-mode now.)
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TopBar } from "./TopBar";
import type { AthleteIdentity } from "../lib/api";

const athlete: AthleteIdentity = { name: "Maya Okonkwo", initials: "MO", goal: "Strength", contraindications: [] };

function baseProps(overrides: Partial<Parameters<typeof TopBar>[0]> = {}) {
  return {
    athlete,
    cycleLabel: "Hypertrophy · Wk 2 / 4",
    onPreviewAsAthlete: vi.fn(),
    deliverHref: "/meso/deliver/7/?week=2",
    ...overrides,
  };
}

describe("TopBar", () => {
  it("renders the athlete identity chip", () => {
    render(<TopBar {...baseProps()} />);
    expect(screen.getByText("Maya Okonkwo")).toBeInTheDocument();
  });

  it("renders no identity chip when athlete is null", () => {
    render(<TopBar {...baseProps({ athlete: null })} />);
    expect(screen.queryByText("Maya Okonkwo")).not.toBeInTheDocument();
  });

  it("shows Review changes + Deliver", () => {
    render(<TopBar {...baseProps()} />);
    expect(screen.getByTestId("review-link")).toBeInTheDocument();
    expect(screen.getByTestId("deliver-link")).toHaveAttribute("href", "/meso/deliver/7/?week=2");
  });

  it("calls onPreviewAsAthlete when the preview button is clicked", async () => {
    const user = userEvent.setup();
    const onPreviewAsAthlete = vi.fn();
    render(<TopBar {...baseProps({ onPreviewAsAthlete })} />);
    await user.click(screen.getByTestId("preview-athlete-button"));
    expect(onPreviewAsAthlete).toHaveBeenCalledTimes(1);
  });

  it("renders the cycle label chip only when non-empty", () => {
    const { rerender } = render(<TopBar {...baseProps()} />);
    expect(screen.getByText("Hypertrophy · Wk 2 / 4")).toBeInTheDocument();
    rerender(<TopBar {...baseProps({ cycleLabel: "" })} />);
    expect(screen.queryByText("Hypertrophy · Wk 2 / 4")).not.toBeInTheDocument();
  });
});

// Specs for TopBar (CONTRACT.md "TopBar") — mode segmented control, identity
// chip, cycle label, preview/review/deliver actions; individual-only
// review+deliver vs. the group "Deliver to all · soon" chip (ported 1:1 from
// designer.html's x-show conditionals, lines ~92-97).
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TopBar } from "./TopBar";
import type { AthleteIdentity, GroupIdentity } from "../lib/api";

const athlete: AthleteIdentity = { name: "Maya Okonkwo", initials: "MO", goal: "Strength", contraindications: [] };
const group: GroupIdentity = { id: 3, name: "Squad", member_count: 2, members: [], flags: [] };

function baseProps(overrides: Partial<Parameters<typeof TopBar>[0]> = {}) {
  return {
    mode: "individual" as const,
    onSetMode: vi.fn(),
    isIndividual: true,
    isGroup: false,
    athlete,
    group: null,
    cycleLabel: "Hypertrophy · Wk 2 / 4",
    onPreviewAsAthlete: vi.fn(),
    deliverHref: "/meso/deliver/7/?week=2",
    ...overrides,
  };
}

describe("TopBar", () => {
  it("renders the individual identity and mode buttons", () => {
    render(<TopBar {...baseProps()} />);
    expect(screen.getByText("Maya Okonkwo")).toBeInTheDocument();
    expect(screen.getByTestId("mode-individual-button")).toBeInTheDocument();
    expect(screen.getByTestId("mode-group-button")).toBeInTheDocument();
  });

  it("calls onSetMode with the clicked segment", async () => {
    const user = userEvent.setup();
    const onSetMode = vi.fn();
    render(<TopBar {...baseProps({ onSetMode })} />);
    await user.click(screen.getByTestId("mode-group-button"));
    expect(onSetMode).toHaveBeenCalledWith("group");
  });

  it("shows Review changes + Deliver for an individual plan, not the group chip", () => {
    render(<TopBar {...baseProps()} />);
    expect(screen.getByTestId("review-link")).toBeInTheDocument();
    expect(screen.getByTestId("deliver-link")).toHaveAttribute("href", "/meso/deliver/7/?week=2");
    expect(screen.queryByText(/Deliver to all/)).not.toBeInTheDocument();
  });

  it("shows the 'Deliver to all · soon' chip for a group plan, hiding review+deliver", () => {
    render(
      <TopBar
        {...baseProps({ mode: "group", isIndividual: false, isGroup: true, athlete: null, group })}
      />,
    );
    expect(screen.getByText(/Deliver to all/)).toBeInTheDocument();
    expect(screen.queryByTestId("review-link")).not.toBeInTheDocument();
    expect(screen.queryByTestId("deliver-link")).not.toBeInTheDocument();
    expect(screen.getByText("Squad")).toBeInTheDocument();
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

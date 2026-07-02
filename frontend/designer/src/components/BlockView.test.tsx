// Specs for BlockView (CONTRACT.md "BlockView") — macro strip, the three
// periodStyle renders (timeline/ladder/calendar), calendar cells via
// lib/grid.ts's cellStyle/cellOn.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BlockView } from "./BlockView";
import type { Phase, Week } from "../lib/api";

const phases: Phase[] = [
  { name: "Accumulation", weeks: "4 wk", state: "done" },
  { name: "Hypertrophy", weeks: "4 wk", state: "current" },
];
const weeks: Week[] = [
  { id: 1, index: 1, label: "Wk 1", current: true, phase: "Hypertrophy", vol: 80, inten: 60 },
  { id: 2, index: 2, label: "Wk 2", current: false, phase: "Hypertrophy", vol: 90, inten: 65, deload: true },
];

function baseProps(overrides: Partial<Parameters<typeof BlockView>[0]> = {}) {
  return {
    phases,
    weeks,
    periodStyle: "timeline" as const,
    onSetPeriodStyle: vi.fn(),
    onSwitchWeek: vi.fn(),
    ...overrides,
  };
}

describe("BlockView", () => {
  it("renders the macro strip with every phase", () => {
    render(<BlockView {...baseProps()} />);
    expect(screen.getByText("Accumulation")).toBeInTheDocument();
    expect(screen.getByText("Hypertrophy")).toBeInTheDocument();
  });

  it("renders the period-style segmented control and switches styles", async () => {
    const user = userEvent.setup();
    const onSetPeriodStyle = vi.fn();
    render(<BlockView {...baseProps({ onSetPeriodStyle })} />);
    await user.click(screen.getByTestId("period-style-ladder-button"));
    expect(onSetPeriodStyle).toHaveBeenCalledWith("ladder");
    await user.click(screen.getByTestId("period-style-calendar-button"));
    expect(onSetPeriodStyle).toHaveBeenCalledWith("calendar");
    await user.click(screen.getByTestId("period-style-timeline-button"));
    expect(onSetPeriodStyle).toHaveBeenCalledWith("timeline");
  });

  it("renders a clickable timeline bar per week that calls onSwitchWeek", async () => {
    const user = userEvent.setup();
    const onSwitchWeek = vi.fn();
    render(<BlockView {...baseProps({ periodStyle: "timeline", onSwitchWeek })} />);
    await user.click(screen.getByTestId("block-week-1"));
    expect(onSwitchWeek).toHaveBeenCalledWith(1);
  });

  it("renders the ladder view's phases when periodStyle is 'ladder'", () => {
    render(<BlockView {...baseProps({ periodStyle: "ladder" })} />);
    // Ladder renders one block per phase (same names as the macro strip).
    expect(screen.getAllByText("Hypertrophy").length).toBeGreaterThanOrEqual(2);
  });

  it("renders the calendar view's week labels when periodStyle is 'calendar'", () => {
    render(<BlockView {...baseProps({ periodStyle: "calendar" })} />);
    expect(screen.getAllByText("Wk 1").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Wk 2").length).toBeGreaterThanOrEqual(1);
  });
});

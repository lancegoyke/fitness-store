// Specs for BlockView (CONTRACT.md "BlockView") — macro strip, the three
// periodStyle renders (timeline/ladder/calendar), calendar cells via
// lib/grid.ts's cellStyle/cellOn.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BlockView } from "./BlockView";
import type { GridWeek, Phase } from "../lib/api";

const phases: Phase[] = [
  { name: "Accumulation", weeks: "4 wk", state: "done" },
  { name: "Hypertrophy", weeks: "4 wk", state: "current" },
];
// Issue #455 phase A5: BlockView now takes GridWeek[] (sourced off the
// grid), not the retired one-week Week[]. Programs are date-less and carry
// no "current week" pointer (docs/meso/remove-current-week-plan.md), so
// there is no `current` field to fixture here anymore.
const weeks: GridWeek[] = [
  {
    id: 1,
    index: 1,
    label: "Wk 1",
    phase: "Hypertrophy",
    deload: false,
    delivered_at: null,
    vol: 80,
    inten: 60,
  },
  {
    id: 2,
    index: 2,
    label: "Wk 2",
    phase: "Hypertrophy",
    deload: true,
    delivered_at: null,
    vol: 90,
    inten: 65,
  },
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

  // Programs are date-less and carry no "current week" pointer
  // (docs/meso/remove-current-week-plan.md) — the timeline/calendar views no
  // longer highlight any one week as "current" (only a deload week still
  // gets its own marker). Phase state's "current" (a macrocycle concept,
  // still real) is unaffected — covered by the macro-strip test above.
  it("never applies an is-current class to a timeline bar/label or a calendar week label/dot", () => {
    const { container: timelineContainer } = render(<BlockView {...baseProps({ periodStyle: "timeline" })} />);
    expect(timelineContainer.querySelectorAll(".is-current")).toHaveLength(0);

    const { container: calendarContainer } = render(<BlockView {...baseProps({ periodStyle: "calendar" })} />);
    expect(calendarContainer.querySelectorAll(".is-current")).toHaveLength(0);
  });
});

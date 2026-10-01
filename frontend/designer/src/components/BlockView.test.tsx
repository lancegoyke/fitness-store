// Specs for BlockView (CONTRACT.md "BlockView") — macro strip, the three
// periodStyle renders (timeline/ladder/calendar), calendar cells via
// lib/grid.ts's cellStyle/cellOn.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BlockView } from "./BlockView";
import type { GridDay, GridWeek, Phase } from "../lib/api";

const phases: Phase[] = [
  { id: 10, name: "Accumulation", weeks: "4 wk", state: "done" },
  { id: 20, name: "Hypertrophy", weeks: "4 wk", state: "current" },
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

function calendarDays(count: number): GridDay[] {
  return Array.from({ length: count }, (_, index) => ({
    session_slot_id: index + 1,
    session_id: 100 + index,
    session_ids: { "1": 100 + index, "2": 200 + index },
    day_number: index + 1,
    name: `Day ${index + 1}`,
    bias: "",
    order: index,
    rows: [],
  }));
}

function baseProps(overrides: Partial<Parameters<typeof BlockView>[0]> = {}) {
  return {
    phases,
    weeks,
    days: calendarDays(3),
    periodStyle: "timeline" as const,
    onSetPeriodStyle: vi.fn(),
    onRenameMesocycle: vi.fn(),
    onSwitchWeek: vi.fn(),
    ...overrides,
  };
}

describe("BlockView", () => {
  it("renders the macro strip with every phase", () => {
    render(<BlockView {...baseProps()} />);
    expect(screen.getByText("Accumulation")).toBeInTheDocument();
    expect(screen.getByText("Hypertrophy")).toBeInTheDocument();
    expect(screen.getByText("This mesocycle · Hypertrophy")).toBeInTheDocument();
  });

  it("renames a phase inline with a trimmed Enter commit", async () => {
    const user = userEvent.setup();
    const onRenameMesocycle = vi.fn();
    render(<BlockView {...baseProps({ onRenameMesocycle })} />);

    await user.click(screen.getByRole("button", { name: "Rename block: Hypertrophy" }));
    const input = screen.getByRole("textbox", { name: "Block name" });
    await user.clear(input);
    await user.type(input, "  Strength  {Enter}");

    expect(onRenameMesocycle).toHaveBeenCalledWith(20, "Strength");
  });

  it("commits on blur, while Escape and an empty draft revert", async () => {
    const user = userEvent.setup();
    const onRenameMesocycle = vi.fn();
    render(<BlockView {...baseProps({ onRenameMesocycle })} />);

    await user.click(screen.getByRole("button", { name: "Rename block: Accumulation" }));
    await user.clear(screen.getByRole("textbox", { name: "Block name" }));
    await user.type(screen.getByRole("textbox", { name: "Block name" }), "Base");
    await user.tab();
    expect(onRenameMesocycle).toHaveBeenCalledWith(10, "Base");

    await user.click(screen.getByRole("button", { name: "Rename block: Hypertrophy" }));
    await user.clear(screen.getByRole("textbox", { name: "Block name" }));
    await user.type(screen.getByRole("textbox", { name: "Block name" }), "Discard{Escape}");
    expect(onRenameMesocycle).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Rename block: Hypertrophy" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Rename block: Hypertrophy" }));
    await user.clear(screen.getByRole("textbox", { name: "Block name" }));
    await user.tab();
    expect(onRenameMesocycle).toHaveBeenCalledTimes(1);
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

  it.each([2, 3, 4])("renders one calendar dot per live week for each of %i program days", (dayCount) => {
    const { container } = render(
      <BlockView {...baseProps({ periodStyle: "calendar", days: calendarDays(dayCount) })} />,
    );

    expect(container.querySelectorAll(".meso-cal-day-label")).toHaveLength(dayCount);
    expect(container.querySelectorAll(".meso-cal-dot")).toHaveLength(dayCount * weeks.length);
  });

  it("uses program-day columns, omits missing sessions, and keeps deload sessions", () => {
    const days = calendarDays(3);
    days[1] = { ...days[1]!, session_ids: { "1": 101 } };
    const { container } = render(<BlockView {...baseProps({ periodStyle: "calendar", days })} />);

    expect(Array.from(container.querySelectorAll(".meso-cal-day-label"), (node) => node.textContent)).toEqual([
      "Day 1",
      "Day 2",
      "Day 3",
    ]);
    const rows = container.querySelectorAll(".meso-cal-row");
    expect(rows[0]?.querySelectorAll(".meso-cal-dot")).toHaveLength(3);
    expect(rows[1]?.querySelectorAll(".meso-cal-dot")).toHaveLength(2);
    expect(container.querySelector(".meso-cal-header-row")).not.toHaveTextContent(/^MWF$/);
    expect(screen.queryByText("M")).not.toBeInTheDocument();
    expect(screen.queryByText("W")).not.toBeInTheDocument();
    expect(screen.queryByText("F")).not.toBeInTheDocument();
  });

  it("labels calendar columns by the program day's own number, as the athlete sees it", () => {
    const days = calendarDays(2);
    days[1] = { ...days[1]!, day_number: 3, name: "" };
    const { container } = render(<BlockView {...baseProps({ periodStyle: "calendar", days })} />);

    expect(Array.from(container.querySelectorAll(".meso-cal-day-label"), (node) => node.textContent)).toEqual([
      "Day 1",
      "Day 3",
    ]);
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

  describe("unparsed weeks (#638)", () => {
    const mixed: GridWeek[] = [
      { ...weeks[0]!, vol: null, inten: null },
      weeks[1]!,
    ];

    it("draws no bars and a caption for a week with nothing parsed", () => {
      const { container } = render(<BlockView {...baseProps({ weeks: mixed })} />);
      const w1 = screen.getByTestId("block-week-1");
      expect(w1.querySelectorAll(".meso-bar")).toHaveLength(0);
      expect(screen.getByTestId("block-week-nodata-1")).toHaveTextContent(
        "Nothing to chart — no sets × reps or loads parsed",
      );
      expect(screen.getByTestId("block-week-2").querySelectorAll(".meso-bar")).toHaveLength(2);
      expect(screen.queryByTestId("block-week-nodata-2")).not.toBeInTheDocument();
      expect(screen.getByTestId("block-chart-caption")).toBeInTheDocument();
      expect(container).toBeTruthy();
    });

    it("draws only the non-null bar when one is null, with no nodata caption", () => {
      render(<BlockView {...baseProps({ weeks: [{ ...weeks[0]!, vol: 50, inten: null }] })} />);
      const w1 = screen.getByTestId("block-week-1");
      expect(w1.querySelectorAll(".meso-bar--vol")).toHaveLength(1);
      expect(w1.querySelectorAll(".meso-bar--inten")).toHaveLength(0);
      expect(screen.queryByTestId("block-week-nodata-1")).not.toBeInTheDocument();
      expect(screen.getByTestId("block-chart-caption")).toBeInTheDocument();
    });

    it("shows neither caption when every bar is present", () => {
      render(<BlockView {...baseProps()} />);
      expect(screen.queryByTestId("block-chart-caption")).not.toBeInTheDocument();
      expect(screen.queryByTestId("block-week-nodata-1")).not.toBeInTheDocument();
    });

    it("labels calendar columns by day name, falling back to 'Day N' when blank", () => {
      const days = calendarDays(3);
      days[0]!.name = "Upper A";
      days[1]!.name = "   ";
      days[2]!.name = "";
      const { container } = render(<BlockView {...baseProps({ periodStyle: "calendar", days })} />);
      const labels = Array.from(container.querySelectorAll(".meso-cal-day-label")).map((el) => el.textContent);
      expect(labels).toEqual(["Upper A", "Day 2", "Day 3"]);
    });
  });
});

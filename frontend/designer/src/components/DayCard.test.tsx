// Specs for DayCard (CONTRACT.md "DayCard") — day header (name/bias/count +
// remove-day arm/confirm/cancel), column headers, one ExerciseRow per
// exercise, "+ Add exercise".
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DayCard } from "./DayCard";
import type { Day } from "../lib/api";
import type { PendingDelete } from "../hooks/usePlanData";

function day(overrides: Partial<Day> = {}): Day {
  return {
    id: 1,
    n: 1,
    name: "Lower",
    bias: "Quad bias",
    exercises: [
      { id: 9, name: "Squat", sets: "3", reps: "5", load: "100", load_type: "abs" },
      { id: 10, name: "Bench", sets: "3", reps: "5", load: "80", load_type: "abs" },
    ],
    ...overrides,
  };
}

function baseProps(overrides: Partial<Parameters<typeof DayCard>[0]> = {}) {
  return {
    day: day(),
    dayIndex: 0,
    isGroup: false,
    unit: "kg",
    pendingDelete: null as PendingDelete | null,
    deleting: false,
    onRequestRemoveDay: vi.fn(),
    onConfirmPendingDelete: vi.fn(),
    onCancelPendingDelete: vi.fn(),
    onAddExercise: vi.fn(),
    onFieldChange: vi.fn(),
    onCommit: vi.fn(),
    onRemoveExercise: vi.fn(),
    onToggleLoadType: vi.fn(),
    onOpenOverride: vi.fn(),
    onOpenOneRm: vi.fn(),
    onOneRmChange: vi.fn(),
    onOneRmSave: vi.fn(),
    onOneRmCancel: vi.fn(),
    oneRmOpenForRow: vi.fn(() => false),
    oneRmEditorState: null,
    ...overrides,
  };
}

describe("DayCard", () => {
  it("renders the day header (name, bias, exercise count)", () => {
    render(<DayCard {...baseProps()} />);
    expect(screen.getByText("Lower")).toBeInTheDocument();
    expect(screen.getByText("Quad bias")).toBeInTheDocument();
    expect(screen.getByText("2 exercises")).toBeInTheDocument();
  });

  it("renders one ExerciseRow per exercise via its testids", () => {
    render(<DayCard {...baseProps()} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveValue("Squat");
    expect(screen.getByTestId("exercise-name-10")).toHaveValue("Bench");
  });

  it("arms remove-day and renders confirm/cancel when armed for this day", async () => {
    const user = userEvent.setup();
    const onRequestRemoveDay = vi.fn();
    const { rerender } = render(<DayCard {...baseProps({ onRequestRemoveDay })} />);
    await user.click(screen.getByTestId("remove-day-1"));
    expect(onRequestRemoveDay).toHaveBeenCalledWith(0);

    const onConfirmPendingDelete = vi.fn();
    const onCancelPendingDelete = vi.fn();
    rerender(
      <DayCard
        {...baseProps({
          pendingDelete: { type: "day", di: 0 },
          onConfirmPendingDelete,
          onCancelPendingDelete,
        })}
      />,
    );
    expect(screen.queryByTestId("remove-day-1")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("confirm-remove-day-1"));
    expect(onConfirmPendingDelete).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("cancel-remove-day-1"));
    expect(onCancelPendingDelete).toHaveBeenCalledTimes(1);
  });

  it("does not show this day's confirm/cancel when a different day is armed", () => {
    render(<DayCard {...baseProps({ pendingDelete: { type: "day", di: 1 } })} />);
    expect(screen.getByTestId("remove-day-1")).toBeInTheDocument();
    expect(screen.queryByTestId("confirm-remove-day-1")).not.toBeInTheDocument();
  });

  it("calls onAddExercise with the day index from '+ Add exercise'", async () => {
    const user = userEvent.setup();
    const onAddExercise = vi.fn();
    render(<DayCard {...baseProps({ onAddExercise })} />);
    await user.click(screen.getByTestId("add-exercise-1"));
    expect(onAddExercise).toHaveBeenCalledWith(0);
  });
});

// === Phase 4 (dnd-kit reordering) — RED. DayCard does not render a day-strip
// drag handle yet; these specs are appended (existing specs above are
// untouched). Per scratchpad/phase4-spec.md: "each DayCard header gets one
// [handle] (`data-testid="day-drag-${id}"`, aria-label "Reorder <day
// name>")". The spec's own example phrase is "<day name or 'Day N'>" — this
// block resolves the fallback as "Day <day.n>" (the same badge number the
// header already renders in `.meso-day-badge`), not the array index, so the
// label stays correct after a day reorder (`n` is the day's own identity;
// `dayIndex` is presentation position and changes on every drag).
describe("drag handle (Phase 4, dnd-kit reordering)", () => {
  it("renders a day-strip drag-handle button with the reorder testid/aria-label", () => {
    render(<DayCard {...baseProps()} />);
    const handle = screen.getByTestId("day-drag-1");
    expect(handle.tagName).toBe("BUTTON");
    expect(handle).toHaveAttribute("type", "button");
    expect(handle).toHaveAttribute("aria-label", "Reorder Lower");
  });

  it("falls back to 'Reorder Day <n>' when the day has no name", () => {
    render(<DayCard {...baseProps({ day: day({ name: "" }) })} />);
    expect(screen.getByTestId("day-drag-1")).toHaveAttribute("aria-label", "Reorder Day 1");
  });

  it("is NOT a grid-nav restoration target", () => {
    render(<DayCard {...baseProps()} />);
    expect(screen.getByTestId("day-drag-1")).not.toHaveAttribute("data-grid-restore");
  });
});

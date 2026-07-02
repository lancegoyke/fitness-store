// Specs for WeekGrid (CONTRACT.md "WeekGrid / DayCard / ExerciseRow") — mounts
// WeekStrip at the top when view === "week" (tested at the DesignerRoot/view-
// switching level), the grid coachmark, the group-mode banner, one DayCard per
// program day, and "+ Add day". Deep per-row editing/delete coverage lives in
// DayCard.test.tsx and ExerciseRow.test.tsx; this file covers what only
// WeekGrid itself owns.
//
// NOTE (contract gap): CONTRACT.md's WeekGrid prop list
// (`{ program, isGroup, pendingDelete, deleting, onRequestRemoveDay,
// onConfirmPendingDelete, onCancelPendingDelete, onAddDay, ...WeekStrip
// props }`) omits the DayCard/ExerciseRow passthrough (unit, onFieldChange,
// onCommit, onRemoveExercise, onToggleLoadType, onOpenOverride, onOpenOneRm,
// onOneRm{Change,Save,Cancel}, oneRmOpenForRow, oneRmEditorState) and the
// coachmark hook slice (coachmarkVisible, dismissCoachmark) its own prose
// says are wired in. Resolved by including them — WeekGrid must have them to
// render DayCard/ExerciseRow and the coachmark at all.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WeekGrid } from "./WeekGrid";
import type { Day, Week, HistoryState } from "../lib/api";
import type { PendingDelete } from "../hooks/usePlanData";

function day(overrides: Partial<Day> = {}): Day {
  return { id: 1, n: 1, name: "Lower", bias: "Quad bias", exercises: [], ...overrides };
}

const weeks: Week[] = [{ id: 1, index: 1, label: "Wk 1", current: true }];
const HISTORY_NONE: HistoryState = { can_undo: false, can_redo: false, undo_label: null, redo_label: null };

function baseProps(overrides: Partial<Parameters<typeof WeekGrid>[0]> = {}) {
  return {
    program: [day()],
    isGroup: false,
    unit: "kg",
    pendingDelete: null as PendingDelete | null,
    deleting: false,
    onRequestRemoveDay: vi.fn(),
    onConfirmPendingDelete: vi.fn(),
    onCancelPendingDelete: vi.fn(),
    onAddDay: vi.fn(),
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
    coachmarkVisible: vi.fn(() => true),
    dismissCoachmark: vi.fn(),
    weeks,
    viewedWeekId: 1 as number | string | null,
    viewedIsCurrent: true,
    history: HISTORY_NONE,
    undoing: false,
    onSwitchWeek: vi.fn(),
    onAddWeek: vi.fn(),
    onMakeCurrent: vi.fn(),
    onRequestRemoveWeek: vi.fn(),
    ...overrides,
  };
}

describe("WeekGrid", () => {
  it("renders WeekStrip's week chips at the top", () => {
    render(<WeekGrid {...baseProps()} />);
    expect(screen.getByTestId("week-chip-1")).toBeInTheDocument();
  });

  it("renders one DayCard per program day", () => {
    render(
      <WeekGrid
        {...baseProps({ program: [day({ id: 1, name: "Lower" }), day({ id: 2, name: "Upper", n: 2 })] })}
      />,
    );
    expect(screen.getByText("Lower")).toBeInTheDocument();
    expect(screen.getByText("Upper")).toBeInTheDocument();
  });

  it("shows the grid coachmark when coachmarkVisible('grid') is true, dismiss wired", async () => {
    const user = userEvent.setup();
    const dismissCoachmark = vi.fn();
    render(<WeekGrid {...baseProps({ dismissCoachmark })} />);
    expect(screen.getByText("The week grid")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(dismissCoachmark).toHaveBeenCalledWith("grid");
  });

  it("hides the grid coachmark when dismissed", () => {
    render(<WeekGrid {...baseProps({ coachmarkVisible: vi.fn(() => false) })} />);
    expect(screen.queryByText("The week grid")).not.toBeInTheDocument();
  });

  it("shows the group-mode banner only in group mode", () => {
    const { rerender } = render(<WeekGrid {...baseProps({ isGroup: false })} />);
    expect(screen.queryByText(/Shared program/)).not.toBeInTheDocument();
    rerender(<WeekGrid {...baseProps({ isGroup: true })} />);
    expect(screen.getByText(/Shared program/)).toBeInTheDocument();
  });

  it("calls onAddDay from '+ Add day'", async () => {
    const user = userEvent.setup();
    const onAddDay = vi.fn();
    render(<WeekGrid {...baseProps({ onAddDay })} />);
    await user.click(screen.getByTestId("add-day-button"));
    expect(onAddDay).toHaveBeenCalledTimes(1);
  });
});

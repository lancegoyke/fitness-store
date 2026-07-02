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
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WeekGrid } from "./WeekGrid";
import type { Day, Week, HistoryState, Exercise } from "../lib/api";
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

// === Phase 3: grid keyboard navigation — RED. WeekGrid is where useGridNav
// (../hooks/useGridNav, does not exist yet) gets instantiated internally —
// it's called ONCE inside WeekGrid (which already receives `program`), so
// WeekGrid's own prop signature does NOT change; no new required prop shows
// up in baseProps() above, and none of the existing specs needed touching.
// These specs render real multi-day/multi-exercise programs and drive real
// keyboard events through RTL — no mock gridNav is needed or possible here
// (it isn't an externally-injectable prop).
//
// A generic negative ("key X does nothing") is NOT useful as a red spec:
// with nothing wired up today, "nothing moves" is already trivially true.
// Every spec below is built so at least one assertion is false against
// today's (unmodified) WeekGrid — see inline notes where that isn't obvious.
function exercise(id: number, overrides: Partial<Exercise> = {}): Exercise {
  return { id, name: `Ex ${id}`, sets: "3", reps: "5", load: "100", load_type: "abs", rpe: "8", note: "", ...overrides };
}

// day 1 (id 1): ex 9 "Box Squat", ex 10 "RDL" — day 2 (id 2): ex 11 "Bench".
const NAV_PROGRAM: Day[] = [
  day({ id: 1, n: 1, name: "Lower", exercises: [exercise(9, { name: "Box Squat" }), exercise(10, { name: "RDL" })] }),
  day({ id: 2, n: 2, name: "Upper", exercises: [exercise(11, { name: "Bench" })] }),
];

describe("Phase 3: ArrowDown / ArrowUp navigate rows across day-card boundaries", () => {
  it("ArrowDown moves focus to the same column on the next row within a day", async () => {
    const user = userEvent.setup();
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    await user.click(screen.getByTestId("exercise-sets-9"));
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("exercise-sets-10")).toHaveFocus();
  });

  it("ArrowDown crosses a day-card boundary (last row of day 1 -> first row of day 2)", async () => {
    const user = userEvent.setup();
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    await user.click(screen.getByTestId("exercise-load-10"));
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("exercise-load-11")).toHaveFocus();
  });

  it("ArrowUp mirrors ArrowDown, crossing day boundaries upward", async () => {
    const user = userEvent.setup();
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    await user.click(screen.getByTestId("exercise-rpe-11"));
    await user.keyboard("{ArrowUp}");
    expect(screen.getByTestId("exercise-rpe-10")).toHaveFocus();
  });
});

describe("Phase 3: ArrowRight / ArrowLeft move columns only at caret extremes", () => {
  it("ArrowRight at the end of the text moves to the next column", () => {
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    const setsInput = screen.getByTestId("exercise-sets-9") as HTMLInputElement;
    setsInput.focus();
    setsInput.setSelectionRange(setsInput.value.length, setsInput.value.length);
    fireEvent.keyDown(setsInput, { key: "ArrowRight" });
    expect(screen.getByTestId("exercise-reps-9")).toHaveFocus();
  });

  it("ArrowLeft at the start of the text moves to the previous column", () => {
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    const repsInput = screen.getByTestId("exercise-reps-9") as HTMLInputElement;
    repsInput.focus();
    repsInput.setSelectionRange(0, 0);
    fireEvent.keyDown(repsInput, { key: "ArrowLeft" });
    expect(screen.getByTestId("exercise-sets-9")).toHaveFocus();
  });
});

describe("Phase 3: Enter commits / Escape reverts (integrated)", () => {
  it("Enter commits only when the cell is dirty, and keeps focus in place", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM, onCommit })} />);
    const loadInput = screen.getByTestId("exercise-load-9");
    await user.click(loadInput);
    await user.keyboard("{Enter}"); // clean cell: no-op (existing dirty gate)
    expect(onCommit).not.toHaveBeenCalled();
    await user.keyboard("5"); // dirties the cell
    await user.keyboard("{Enter}");
    expect(onCommit).toHaveBeenCalledWith(0, 0);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(loadInput).toHaveFocus();
  });

  it("Escape reverts to the focus-time value via onFieldChange and suppresses the next blur commit", async () => {
    const user = userEvent.setup();
    const onFieldChange = vi.fn();
    const onCommit = vi.fn();
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM, onFieldChange, onCommit })} />);
    const loadInput = screen.getByTestId("exercise-load-9");
    await user.click(loadInput);
    await user.keyboard("999"); // undoes nothing yet — just a draft
    await user.keyboard("{Escape}");
    // The focus-time value was "100" (exercise(9)'s default load) — never
    // re-typed, so this exact call can only come from the revert path.
    expect(onFieldChange).toHaveBeenCalledWith(0, 0, "load", "100");
    await user.tab();
    expect(onCommit).not.toHaveBeenCalled();
  });
});

describe("Phase 3: roving tabIndex across multiple rendered rows/days", () => {
  it("exactly one cell (the grid's first) is tabbable on initial render", () => {
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("exercise-name-10")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("exercise-name-11")).toHaveAttribute("tabindex", "-1");
  });

  it("focusing another cell moves the roving tabIndex to it", async () => {
    const user = userEvent.setup();
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    await user.click(screen.getByTestId("exercise-rpe-10"));
    expect(screen.getByTestId("exercise-rpe-10")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("exercise-name-9")).toHaveAttribute("tabindex", "-1");
  });
});

describe("Phase 3: aria-labels flow through to rendered cells", () => {
  it("labels a rendered cell with its exercise name and column", () => {
    render(<WeekGrid {...baseProps({ program: NAV_PROGRAM })} />);
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("aria-label", "Box Squat — sets");
  });
});

// Specs for WeekStrip (CONTRACT.md "WeekStrip") — week chips, add week,
// make-current, remove-week arm/confirm/cancel, undo/redo. Rendered only
// when weeks.length > 0 (the source's `x-show="live && weeks.length"`).
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WeekStrip } from "./WeekStrip";
import type { Week, HistoryState } from "../lib/api";
import type { PendingDelete } from "../hooks/usePlanData";

const weeks: Week[] = [
  { id: 1, index: 1, label: "Wk 1", current: true },
  { id: 2, index: 2, label: "Wk 2", current: false },
];

const HISTORY_BOTH: HistoryState = {
  can_undo: true,
  can_redo: true,
  undo_label: "Edited Box Squat",
  redo_label: "Deleted Day 2",
};
const HISTORY_NONE: HistoryState = { can_undo: false, can_redo: false, undo_label: null, redo_label: null };

function baseProps(overrides: Partial<Parameters<typeof WeekStrip>[0]> = {}) {
  return {
    weeks,
    viewedWeekId: 2 as number | string | null,
    viewedIsCurrent: false,
    pendingDelete: null as PendingDelete | null,
    deleting: false,
    history: HISTORY_BOTH,
    undoing: false,
    onSwitchWeek: vi.fn(),
    onAddWeek: vi.fn(),
    onMakeCurrent: vi.fn(),
    onRequestRemoveWeek: vi.fn(),
    onCancelPendingDelete: vi.fn(),
    onConfirmPendingDelete: vi.fn(),
    onUndo: vi.fn(),
    onRedo: vi.fn(),
    ...overrides,
  };
}

describe("WeekStrip", () => {
  it("renders nothing when weeks is empty", () => {
    const { container } = render(<WeekStrip {...baseProps({ weeks: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a chip per week and switches on click", async () => {
    const user = userEvent.setup();
    const onSwitchWeek = vi.fn();
    render(<WeekStrip {...baseProps({ onSwitchWeek })} />);
    expect(screen.getByTestId("week-chip-1")).toHaveTextContent("Wk 1");
    expect(screen.getByTestId("week-chip-2")).toHaveTextContent("Wk 2");
    await user.click(screen.getByTestId("week-chip-1"));
    expect(onSwitchWeek).toHaveBeenCalledWith(1);
  });

  it("adds a week via the add-week button", async () => {
    const user = userEvent.setup();
    const onAddWeek = vi.fn();
    render(<WeekStrip {...baseProps({ onAddWeek })} />);
    await user.click(screen.getByTestId("add-week-button"));
    expect(onAddWeek).toHaveBeenCalledTimes(1);
  });

  it("shows 'Make current' only when the viewed week isn't current", async () => {
    const user = userEvent.setup();
    const onMakeCurrent = vi.fn();
    const { rerender } = render(<WeekStrip {...baseProps({ viewedIsCurrent: false, onMakeCurrent })} />);
    await user.click(screen.getByTestId("make-current-button"));
    expect(onMakeCurrent).toHaveBeenCalledWith(2);
    rerender(<WeekStrip {...baseProps({ viewedIsCurrent: true })} />);
    expect(screen.queryByTestId("make-current-button")).not.toBeInTheDocument();
  });

  it("hides remove-week for the current week", () => {
    render(<WeekStrip {...baseProps({ viewedIsCurrent: true })} />);
    expect(screen.queryByTestId("remove-week-button")).not.toBeInTheDocument();
  });

  it("arms remove-week, then confirm/cancel replace the button", async () => {
    const user = userEvent.setup();
    const onRequestRemoveWeek = vi.fn();
    render(<WeekStrip {...baseProps({ onRequestRemoveWeek })} />);
    await user.click(screen.getByTestId("remove-week-button"));
    expect(onRequestRemoveWeek).toHaveBeenCalledWith(2);
  });

  it("renders confirm/cancel when this week's delete is armed", async () => {
    const user = userEvent.setup();
    const onConfirmPendingDelete = vi.fn();
    const onCancelPendingDelete = vi.fn();
    render(
      <WeekStrip
        {...baseProps({
          pendingDelete: { type: "week", weekId: 2 },
          onConfirmPendingDelete,
          onCancelPendingDelete,
        })}
      />,
    );
    expect(screen.queryByTestId("remove-week-button")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("confirm-remove-week-button"));
    expect(onConfirmPendingDelete).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("cancel-remove-week-button"));
    expect(onCancelPendingDelete).toHaveBeenCalledTimes(1);
  });

  it("disables confirm/cancel/remove while deleting", () => {
    render(<WeekStrip {...baseProps({ deleting: true })} />);
    expect(screen.getByTestId("remove-week-button")).toBeDisabled();
  });

  it("undo/redo disabled off history + undoing, titles from labels", () => {
    render(<WeekStrip {...baseProps({ history: HISTORY_BOTH })} />);
    expect(screen.getByTestId("undo-button")).not.toBeDisabled();
    expect(screen.getByTestId("undo-button")).toHaveAttribute("title", expect.stringContaining("Edited Box Squat"));
    expect(screen.getByTestId("redo-button")).not.toBeDisabled();
    expect(screen.getByTestId("redo-button")).toHaveAttribute("title", expect.stringContaining("Deleted Day 2"));
  });

  it("undo/redo disabled when history says nothing to do, or while undoing", () => {
    const { rerender } = render(<WeekStrip {...baseProps({ history: HISTORY_NONE })} />);
    expect(screen.getByTestId("undo-button")).toBeDisabled();
    expect(screen.getByTestId("redo-button")).toBeDisabled();
    rerender(<WeekStrip {...baseProps({ history: HISTORY_BOTH, undoing: true })} />);
    expect(screen.getByTestId("undo-button")).toBeDisabled();
    expect(screen.getByTestId("redo-button")).toBeDisabled();
  });

  it("calls onUndo/onRedo when clicked", async () => {
    const user = userEvent.setup();
    const onUndo = vi.fn();
    const onRedo = vi.fn();
    render(<WeekStrip {...baseProps({ onUndo, onRedo })} />);
    await user.click(screen.getByTestId("undo-button"));
    expect(onUndo).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("redo-button"));
    expect(onRedo).toHaveBeenCalledTimes(1);
  });
});

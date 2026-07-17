// Specs for TopBar (CONTRACT.md "TopBar") — brand, sidebar toggle, the view
// segmented control, cycle label, and review/deliver actions. designer-simplify:
// the athlete identity chip moved to AthleteMeta and "Preview as athlete" was
// dropped (the segmented "Athlete view" is the one canonical switch).
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TopBar } from "./TopBar";
import type { ViewMode } from "../DesignerRoot";

function baseProps(overrides: Partial<Parameters<typeof TopBar>[0]> = {}) {
  return {
    view: "table" as ViewMode,
    onSelectView: vi.fn(),
    cycleLabel: "Hypertrophy · Wk 2 / 4",
    deliverHref: "/meso/deliver/7/?week=2",
    sidebarOpen: true,
    onToggleSidebar: vi.fn(),
    canUndo: true,
    canRedo: false,
    undoLabel: "Edited Squat" as string | null,
    redoLabel: null as string | null,
    onUndo: vi.fn(),
    onRedo: vi.fn(),
    ...overrides,
  };
}

describe("TopBar", () => {
  it("shows Review changes + Deliver", () => {
    render(<TopBar {...baseProps()} />);
    expect(screen.getByTestId("review-link")).toBeInTheDocument();
    expect(screen.getByTestId("deliver-link")).toHaveAttribute("href", "/meso/deliver/7/?week=2");
  });

  it("renders the view segmented control and marks the active tab", () => {
    render(<TopBar {...baseProps({ view: "block" })} />);
    expect(screen.getByTestId("view-tab-table")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("view-tab-block")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("view-tab-athlete")).toBeInTheDocument();
  });

  it("calls onSelectView with the tab id when a tab is clicked", async () => {
    const user = userEvent.setup();
    const onSelectView = vi.fn();
    render(<TopBar {...baseProps({ onSelectView })} />);
    await user.click(screen.getByTestId("view-tab-athlete"));
    expect(onSelectView).toHaveBeenCalledWith("athlete");
  });

  it("toggles the sidebar and reflects open/closed state", async () => {
    const user = userEvent.setup();
    const onToggleSidebar = vi.fn();
    const { rerender } = render(<TopBar {...baseProps({ sidebarOpen: true, onToggleSidebar })} />);
    const toggle = screen.getByTestId("sidebar-toggle");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    await user.click(toggle);
    expect(onToggleSidebar).toHaveBeenCalledTimes(1);
    rerender(<TopBar {...baseProps({ sidebarOpen: false, onToggleSidebar })} />);
    expect(screen.getByTestId("sidebar-toggle")).toHaveAttribute("aria-pressed", "false");
  });

  it("no longer renders the duplicate athlete identity chip / Preview button", () => {
    render(<TopBar {...baseProps()} />);
    expect(screen.queryByTestId("preview-athlete-button")).not.toBeInTheDocument();
  });

  it("renders undo/redo, reflecting can-undo/can-redo, and fires the handlers", async () => {
    const user = userEvent.setup();
    const onUndo = vi.fn();
    const onRedo = vi.fn();
    render(<TopBar {...baseProps({ canUndo: true, canRedo: false, onUndo, onRedo })} />);
    const undo = screen.getByTestId("grid-undo");
    const redo = screen.getByTestId("grid-redo");
    expect(undo).toBeEnabled();
    expect(redo).toBeDisabled();
    // data-grid-restore is preserved so focus returns to the grid after undo.
    expect(undo).toHaveAttribute("data-grid-restore");
    await user.click(undo);
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it("renders the cycle label chip only when non-empty", () => {
    const { rerender } = render(<TopBar {...baseProps()} />);
    expect(screen.getByText("Hypertrophy · Wk 2 / 4")).toBeInTheDocument();
    rerender(<TopBar {...baseProps({ cycleLabel: "" })} />);
    expect(screen.queryByText("Hypertrophy · Wk 2 / 4")).not.toBeInTheDocument();
  });
});

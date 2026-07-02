// Specs for OverrideModal (CONTRACT.md "OverrideModal") — rendered only when
// override !== null; backdrop click + Escape both call onClose (which
// internally guards on saving, per useOverrideEditor — so this component just
// has to route both interactions to the same handler unconditionally).
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OverrideModal } from "./OverrideModal";
import type { Exercise, GroupMember } from "../lib/api";
import type { OverrideDraft } from "../lib/override";

const members: GroupMember[] = [
  { id: "a1", name: "Maya Okonkwo", initials: "MO" },
  { id: "a2", name: "Aaron Adams", initials: "AA" },
];

function ex(overrides: Partial<Exercise> = {}): Exercise {
  return { id: 11, name: "Back Squat", sets: "3", reps: "10", load: "100", load_type: "abs", ...overrides };
}

function draft(overrides: Partial<OverrideDraft> = {}): OverrideDraft {
  return { swap: "", load_pct: "90", sets: "", reps: "", note: "", ...overrides };
}

function baseProps(overrides: Partial<Parameters<typeof OverrideModal>[0]> = {}) {
  return {
    override: {
      ex: ex(),
      members,
      memberId: "a1",
      draft: draft(),
      saving: false,
      error: "",
    },
    overrideHasExisting: true,
    unit: "kg",
    onSelectMember: vi.fn(),
    onUpdateDraft: vi.fn(),
    onClose: vi.fn(),
    onSave: vi.fn(),
    onClear: vi.fn(),
    ...overrides,
  };
}

describe("OverrideModal", () => {
  it("renders nothing when override is null", () => {
    const { container } = render(<OverrideModal {...baseProps({ override: null })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the exercise name and member buttons", () => {
    render(<OverrideModal {...baseProps()} />);
    expect(screen.getByText("Back Squat")).toBeInTheDocument();
    expect(screen.getByTestId("override-member-a1")).toBeInTheDocument();
    expect(screen.getByTestId("override-member-a2")).toBeInTheDocument();
  });

  it("selects a member via onSelectMember", async () => {
    const user = userEvent.setup();
    const onSelectMember = vi.fn();
    render(<OverrideModal {...baseProps({ onSelectMember })} />);
    await user.click(screen.getByTestId("override-member-a2"));
    expect(onSelectMember).toHaveBeenCalledWith("a2");
  });

  it("renders the draft fields and calls onUpdateDraft on edits", async () => {
    const user = userEvent.setup();
    const onUpdateDraft = vi.fn();
    render(<OverrideModal {...baseProps({ onUpdateDraft })} />);
    expect(screen.getByTestId("override-load-pct-input")).toHaveValue("90");
    await user.type(screen.getByTestId("override-swap-input"), "B");
    expect(onUpdateDraft).toHaveBeenCalledWith({ swap: "B" });
  });

  it("shows the error message when override.error is set", () => {
    render(<OverrideModal {...baseProps({ override: { ...baseProps().override!, error: "Load % must be a whole number from 1 to 200." } })} />);
    expect(screen.getByTestId("override-error")).toHaveTextContent(/Load %/);
  });

  it("shows Clear only when overrideHasExisting, calling onClear", async () => {
    const user = userEvent.setup();
    const onClear = vi.fn();
    const { rerender } = render(<OverrideModal {...baseProps({ onClear })} />);
    await user.click(screen.getByTestId("override-clear-button"));
    expect(onClear).toHaveBeenCalledTimes(1);
    rerender(<OverrideModal {...baseProps({ overrideHasExisting: false })} />);
    expect(screen.queryByTestId("override-clear-button")).not.toBeInTheDocument();
  });

  it("calls onSave from the save button", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<OverrideModal {...baseProps({ onSave })} />);
    await user.click(screen.getByTestId("override-save-button"));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("routes Escape and backdrop click through onClose unconditionally", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<OverrideModal {...baseProps({ onClose })} />);
    await user.click(screen.getByTestId("override-cancel-button"));
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("disables save/clear/cancel and shows a disabled cursor while saving", () => {
    render(<OverrideModal {...baseProps({ override: { ...baseProps().override!, saving: true } })} />);
    expect(screen.getByTestId("override-save-button")).toBeDisabled();
    expect(screen.getByTestId("override-clear-button")).toBeDisabled();
    expect(screen.getByTestId("override-cancel-button")).toBeDisabled();
  });
});

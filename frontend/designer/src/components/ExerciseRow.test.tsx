// Specs for ExerciseRow (CONTRACT.md "ExerciseRow") — controlled cells
// (onChange -> onFieldChange, onBlur -> onCommit), load-type toggle, group
// adjust badge, individual %1RM badge/inline editor, remove ×.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ExerciseRow } from "./ExerciseRow";
import type { Exercise } from "../lib/api";

function ex(overrides: Partial<Exercise> = {}): Exercise {
  return { id: 9, name: "Squat", sets: "3", reps: "5", load: "100", load_type: "abs", rpe: "8", note: "", ...overrides };
}

function baseProps(overrides: Partial<Parameters<typeof ExerciseRow>[0]> = {}) {
  return {
    ex: ex(),
    dayIndex: 0,
    exIndex: 0,
    isGroup: false,
    unit: "kg",
    oneRmOpenForRow: false,
    oneRmEditorState: null,
    // NOTE (contract gap): CONTRACT.md's ExerciseRow prop list omits `deleting`,
    // but its own prose says the remove × is `disabled={deleting}` — and the
    // source (designer.html line ~440) binds `:disabled="deleting"` on that
    // button. Resolved toward the source: ExerciseRow takes a `deleting` prop.
    deleting: false,
    onFieldChange: vi.fn(),
    onCommit: vi.fn(),
    onRemove: vi.fn(),
    onToggleLoadType: vi.fn(),
    onOpenOverride: vi.fn(),
    onOpenOneRm: vi.fn(),
    onOneRmChange: vi.fn(),
    onOneRmSave: vi.fn(),
    onOneRmCancel: vi.fn(),
    ...overrides,
  };
}

describe("controlled cells", () => {
  it("renders every field's value in its testid'd input", () => {
    render(<ExerciseRow {...baseProps()} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveValue("Squat");
    expect(screen.getByTestId("exercise-sets-9")).toHaveValue("3");
    expect(screen.getByTestId("exercise-reps-9")).toHaveValue("5");
    expect(screen.getByTestId("exercise-load-9")).toHaveValue("100");
    expect(screen.getByTestId("exercise-rpe-9")).toHaveValue("8");
    expect(screen.getByTestId("exercise-note-9")).toHaveValue("");
  });

  it("fires onFieldChange on change and onCommit on blur, per field", async () => {
    const user = userEvent.setup();
    const onFieldChange = vi.fn();
    const onCommit = vi.fn();
    render(<ExerciseRow {...baseProps({ onFieldChange, onCommit })} />);
    const nameInput = screen.getByTestId("exercise-name-9");
    await user.type(nameInput, "!");
    expect(onFieldChange).toHaveBeenCalledWith("name", "Squat!");
    await user.tab();
    expect(onCommit).toHaveBeenCalledTimes(1);
  });
});

describe("load type toggle", () => {
  it("calls onToggleLoadType and shows the unit suffix for an absolute load", async () => {
    const user = userEvent.setup();
    const onToggleLoadType = vi.fn();
    render(<ExerciseRow {...baseProps({ onToggleLoadType })} />);
    expect(screen.getByTestId("exercise-load-type-9")).toHaveTextContent("kg");
    await user.click(screen.getByTestId("exercise-load-type-9"));
    expect(onToggleLoadType).toHaveBeenCalledTimes(1);
  });

  it("shows % for a pct row", () => {
    render(<ExerciseRow {...baseProps({ ex: ex({ load_type: "pct", load: "75" }) })} />);
    expect(screen.getByTestId("exercise-load-type-9")).toHaveTextContent("%");
  });
});

describe("remove", () => {
  it("calls onRemove and disables while deleting", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    const { rerender } = render(<ExerciseRow {...baseProps({ onRemove })} />);
    await user.click(screen.getByTestId("exercise-remove-9"));
    expect(onRemove).toHaveBeenCalledTimes(1);
    rerender(<ExerciseRow {...baseProps({ onRemove, deleting: true })} />);
    expect(screen.getByTestId("exercise-remove-9")).toBeDisabled();
  });
});

describe("group adjust badge", () => {
  it("shows '+ adjust' when unset, and the badge text when the row has an adjust", () => {
    const { rerender } = render(<ExerciseRow {...baseProps({ isGroup: true })} />);
    expect(screen.getByTestId("override-badge-9")).toHaveTextContent("+ adjust");
    rerender(<ExerciseRow {...baseProps({ isGroup: true, ex: ex({ adj: "MO -10%" }) })} />);
    expect(screen.getByTestId("override-badge-9")).toHaveTextContent("MO -10%");
  });

  it("calls onOpenOverride when clicked, and is absent in individual mode", async () => {
    const user = userEvent.setup();
    const onOpenOverride = vi.fn();
    render(<ExerciseRow {...baseProps({ isGroup: true, onOpenOverride })} />);
    await user.click(screen.getByTestId("override-badge-9"));
    expect(onOpenOverride).toHaveBeenCalledTimes(1);
  });

  it("is absent in individual mode", () => {
    render(<ExerciseRow {...baseProps({ isGroup: false })} />);
    expect(screen.queryByTestId("override-badge-9")).not.toBeInTheDocument();
  });
});

describe("individual %1RM badge + inline editor", () => {
  it("shows the badge only for !isGroup && load_type === 'pct'", () => {
    const { rerender } = render(
      <ExerciseRow {...baseProps({ isGroup: false, ex: ex({ load_type: "pct", one_rm: "140", one_rm_source: "logged" }) })} />,
    );
    expect(screen.getByTestId("one-rm-badge-9")).toHaveTextContent("140");
    rerender(<ExerciseRow {...baseProps({ isGroup: false, ex: ex({ load_type: "abs" }) })} />);
    expect(screen.queryByTestId("one-rm-badge-9")).not.toBeInTheDocument();
    rerender(<ExerciseRow {...baseProps({ isGroup: true, ex: ex({ load_type: "pct" }) })} />);
    expect(screen.queryByTestId("one-rm-badge-9")).not.toBeInTheDocument();
  });

  it("prompts to set a 1RM when unset", () => {
    render(<ExerciseRow {...baseProps({ ex: ex({ load_type: "pct", one_rm: "" }) })} />);
    expect(screen.getByTestId("one-rm-badge-9")).toHaveTextContent("+ set 1RM");
  });

  it("opens the editor via onOpenOneRm and renders the inline input when oneRmOpenForRow", async () => {
    const user = userEvent.setup();
    const onOpenOneRm = vi.fn();
    const { rerender } = render(
      <ExerciseRow {...baseProps({ ex: ex({ load_type: "pct", one_rm: "140" }), onOpenOneRm })} />,
    );
    await user.click(screen.getByTestId("one-rm-badge-9"));
    expect(onOpenOneRm).toHaveBeenCalledTimes(1);

    rerender(
      <ExerciseRow
        {...baseProps({
          ex: ex({ load_type: "pct", one_rm: "140" }),
          oneRmOpenForRow: true,
          oneRmEditorState: { ex: ex({ load_type: "pct" }), value: "150", saving: false, error: "" },
        })}
      />,
    );
    expect(screen.getByTestId("one-rm-input-9")).toHaveValue("150");
    expect(screen.getByTestId("one-rm-save-9")).toBeInTheDocument();
    expect(screen.getByTestId("one-rm-cancel-9")).toBeInTheDocument();
  });

  it("wires onOneRmChange/onOneRmSave/onOneRmCancel and shows the error testid", async () => {
    const user = userEvent.setup();
    const onOneRmChange = vi.fn();
    const onOneRmSave = vi.fn();
    const onOneRmCancel = vi.fn();
    render(
      <ExerciseRow
        {...baseProps({
          ex: ex({ load_type: "pct" }),
          oneRmOpenForRow: true,
          oneRmEditorState: { ex: ex({ load_type: "pct" }), value: "150", saving: false, error: "Enter a positive number, or leave blank to clear." },
          onOneRmChange,
          onOneRmSave,
          onOneRmCancel,
        })}
      />,
    );
    await user.type(screen.getByTestId("one-rm-input-9"), "0");
    expect(onOneRmChange).toHaveBeenCalled();
    await user.click(screen.getByTestId("one-rm-save-9"));
    expect(onOneRmSave).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("one-rm-cancel-9"));
    expect(onOneRmCancel).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("one-rm-error-9")).toHaveTextContent(/positive number/);
  });

  it("disables save/cancel while saving", () => {
    render(
      <ExerciseRow
        {...baseProps({
          ex: ex({ load_type: "pct" }),
          oneRmOpenForRow: true,
          oneRmEditorState: { ex: ex({ load_type: "pct" }), value: "150", saving: true, error: "" },
        })}
      />,
    );
    expect(screen.getByTestId("one-rm-save-9")).toBeDisabled();
    expect(screen.getByTestId("one-rm-cancel-9")).toBeDisabled();
  });
});

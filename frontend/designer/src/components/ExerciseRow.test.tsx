// Specs for ExerciseRow (CONTRACT.md "ExerciseRow") — controlled cells
// (onChange -> onFieldChange, onBlur -> onCommit), load-type toggle, group
// adjust badge, individual %1RM badge/inline editor, remove ×.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ExerciseRow } from "./ExerciseRow";
import type { Exercise } from "../lib/api";
// Phase 3 (grid keyboard navigation) — type-only import, safely erased at
// build time (verbatimModuleSyntax) even before ../hooks/useGridNav exists,
// so it can't break this file's EXISTING specs while useGridNav is red.
import type { GridColumn, UseGridNavResult, GridCellCallbacks } from "../hooks/useGridNav";

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

describe("no-op blur does not autosave", () => {
  // Parity with the Alpine template's native @change semantics: focusing a
  // cell and tabbing away without typing must not fire a commit — an
  // unconditional onBlur commit would record a no-op undo action server-side
  // for every cell the coach merely browses through.
  it("focus + blur without typing does not commit", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<ExerciseRow {...baseProps({ onCommit })} />);
    await user.click(screen.getByTestId("exercise-name-9"));
    await user.tab();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("a later focus + blur without further typing does not re-commit", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<ExerciseRow {...baseProps({ onCommit })} />);
    const input = screen.getByTestId("exercise-sets-9");
    await user.type(input, "4");
    await user.tab();
    expect(onCommit).toHaveBeenCalledTimes(1);
    await user.click(input);
    await user.tab();
    expect(onCommit).toHaveBeenCalledTimes(1);
  });
});

describe("1RM editor keyboard", () => {
  // Parity with the Alpine editor's @keydown.enter.prevent / @keydown.escape.prevent.
  function editorProps(overrides = {}) {
    const ex = { ...baseProps().ex, load_type: "pct" as const, one_rm: "84" };
    return baseProps({
      isGroup: false,
      ex,
      oneRmOpenForRow: true,
      oneRmEditorState: { ex, value: "84", saving: false, error: "" },
      ...overrides,
    });
  }

  it("Enter saves", async () => {
    const user = userEvent.setup();
    const onOneRmSave = vi.fn();
    render(<ExerciseRow {...editorProps({ onOneRmSave })} />);
    await user.click(screen.getByTestId("one-rm-input-9"));
    await user.keyboard("{Enter}");
    expect(onOneRmSave).toHaveBeenCalledTimes(1);
  });

  it("Escape cancels", async () => {
    const user = userEvent.setup();
    const onOneRmCancel = vi.fn();
    render(<ExerciseRow {...editorProps({ onOneRmCancel })} />);
    await user.click(screen.getByTestId("one-rm-input-9"));
    await user.keyboard("{Escape}");
    expect(onOneRmCancel).toHaveBeenCalledTimes(1);
  });
});

// === Phase 3: grid keyboard navigation — RED (frontend/designer/CONTRACT.md
// has no useGridNav section yet; ../hooks/useGridNav does not exist). See
// useGridNav.test.tsx's header for the full API contract. Additions below,
// existing specs above are untouched.
//
// Design decisions pinned here (ExerciseRow's side of the contract):
// - `ExerciseRowProps` gains an OPTIONAL `gridNav?: UseGridNavResult` prop
//   (threaded WeekGrid -> DayCard -> ExerciseRow, both hops optional) so
//   DayCard.test.tsx's existing fixtures — out of scope for this PR — never
//   need to change: DayCard forwards `gridNav` straight through, and
//   ExerciseRow falls back to a harmless no-op (tabIndex -1, inert
//   onFocus/onKeyDown) when the prop is absent, exactly as these specs
//   exercise via `baseProps()` (which never sets it).
// - `aria-label` and `data-grid-cell` are unconditional — pure functions of
//   (ex.name, ex.id, column) — so they're correct even in that no-gridNav
//   fallback path (a11y must never depend on the nav feature being wired).
describe("Phase 3: cell aria-labels (a11y, unconditional)", () => {
  it("labels every one of the six cells '<exercise name> — <column label>'", () => {
    render(<ExerciseRow {...baseProps({ ex: ex({ name: "Box Squat" }) })} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveAttribute("aria-label", "Box Squat — exercise name");
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("aria-label", "Box Squat — sets");
    expect(screen.getByTestId("exercise-reps-9")).toHaveAttribute("aria-label", "Box Squat — reps");
    expect(screen.getByTestId("exercise-load-9")).toHaveAttribute("aria-label", "Box Squat — load");
    expect(screen.getByTestId("exercise-rpe-9")).toHaveAttribute("aria-label", "Box Squat — RPE");
    expect(screen.getByTestId("exercise-note-9")).toHaveAttribute("aria-label", "Box Squat — note");
  });

  it("falls back to 'exercise' in the label when the row has no name yet", () => {
    render(<ExerciseRow {...baseProps({ ex: ex({ name: "" }) })} />);
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("aria-label", "exercise — sets");
  });
});

describe("Phase 3: grid-cell DOM identity (unconditional)", () => {
  it("tags every cell input data-grid-cell='<prescriptionId>:<column>'", () => {
    render(<ExerciseRow {...baseProps()} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveAttribute("data-grid-cell", "9:name");
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("data-grid-cell", "9:sets");
    expect(screen.getByTestId("exercise-reps-9")).toHaveAttribute("data-grid-cell", "9:reps");
    expect(screen.getByTestId("exercise-load-9")).toHaveAttribute("data-grid-cell", "9:load");
    expect(screen.getByTestId("exercise-rpe-9")).toHaveAttribute("data-grid-cell", "9:rpe");
    expect(screen.getByTestId("exercise-note-9")).toHaveAttribute("data-grid-cell", "9:note");
  });
});

describe("Phase 3: roving tabIndex wiring (gridNav prop)", () => {
  function gridNavAnchoredOn(column: GridColumn): UseGridNavResult {
    return {
      anchor: { prescriptionId: 9, column },
      cellProps: vi.fn((_id: number | string, c: GridColumn) => ({
        tabIndex: c === column ? (0 as const) : (-1 as const),
        onFocus: vi.fn(),
        onKeyDown: vi.fn(),
      })),
    };
  }

  it("sets tabIndex=0 only on the gridNav-anchored column, -1 on the rest", () => {
    render(<ExerciseRow {...baseProps({ gridNav: gridNavAnchoredOn("load") })} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("exercise-load-9")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("exercise-note-9")).toHaveAttribute("tabindex", "-1");
  });

  it("without a gridNav prop, cells fall back to a non-tabbable -1 rather than crashing", () => {
    render(<ExerciseRow {...baseProps()} />);
    expect(screen.getByTestId("exercise-name-9")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("exercise-sets-9")).toHaveAttribute("tabindex", "-1");
  });

  it("calls gridNav.cellProps with this row's prescription id and each cell's column", () => {
    const gridNav = gridNavAnchoredOn("name");
    render(<ExerciseRow {...baseProps({ gridNav })} />);
    expect(gridNav.cellProps).toHaveBeenCalledWith(9, "name", expect.anything());
    expect(gridNav.cellProps).toHaveBeenCalledWith(9, "sets", expect.anything());
    expect(gridNav.cellProps).toHaveBeenCalledWith(9, "reps", expect.anything());
    expect(gridNav.cellProps).toHaveBeenCalledWith(9, "load", expect.anything());
    expect(gridNav.cellProps).toHaveBeenCalledWith(9, "rpe", expect.anything());
    expect(gridNav.cellProps).toHaveBeenCalledWith(9, "note", expect.anything());
  });
});

describe("Phase 3: cellProps callback wiring (Enter-commit / Escape-revert contract)", () => {
  // ExerciseRow must hand useGridNav's cellProps() a callbacks object whose
  // onCommit IS the row's existing dirty-gated commit (spec: "the existing
  // dirty-gated commit — no-op when clean") and whose onRevert writes
  // through the RAW onFieldChange prop (bypassing the dirtying `changed()`
  // wrapper) and clears the dirty flag — this is what lets Escape suppress
  // a subsequent blur-commit. We capture the real callbacks ExerciseRow
  // passes and invoke them directly, rather than re-deriving useGridNav's
  // own key-decision logic here (that's useGridNav.test.tsx's job).
  function captureCallbacks(column: GridColumn) {
    let captured: GridCellCallbacks | undefined;
    const gridNav: UseGridNavResult = {
      anchor: { prescriptionId: 9, column },
      cellProps: vi.fn((_id: number | string, c: GridColumn, callbacks: GridCellCallbacks) => {
        if (c === column) captured = callbacks;
        return { tabIndex: c === column ? (0 as const) : (-1 as const), onFocus: vi.fn(), onKeyDown: vi.fn() };
      }),
    };
    return { gridNav, getCallbacks: () => captured };
  }

  it("onCommit is a no-op on a clean cell (existing dirty gate)", () => {
    const onCommit = vi.fn();
    const { gridNav, getCallbacks } = captureCallbacks("load");
    render(<ExerciseRow {...baseProps({ gridNav, onCommit })} />);
    expect(gridNav.cellProps).toHaveBeenCalled();
    getCallbacks()!.onCommit();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("onCommit fires the row's onCommit prop once the cell has actually been changed", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    const { gridNav, getCallbacks } = captureCallbacks("load");
    render(<ExerciseRow {...baseProps({ gridNav, onCommit })} />);
    await user.type(screen.getByTestId("exercise-load-9"), "5");
    expect(gridNav.cellProps).toHaveBeenCalled();
    getCallbacks()!.onCommit();
    expect(onCommit).toHaveBeenCalledTimes(1);
  });

  it("onRevert calls the raw onFieldChange with the given value and suppresses the next blur commit", async () => {
    const user = userEvent.setup();
    const onFieldChange = vi.fn();
    const onCommit = vi.fn();
    const { gridNav, getCallbacks } = captureCallbacks("load");
    render(<ExerciseRow {...baseProps({ gridNav, onFieldChange, onCommit })} />);
    await user.type(screen.getByTestId("exercise-load-9"), "5"); // dirties the row
    expect(gridNav.cellProps).toHaveBeenCalled();
    getCallbacks()!.onRevert("100");
    expect(onFieldChange).toHaveBeenCalledWith("load", "100");
    await user.click(screen.getByTestId("exercise-load-9"));
    await user.tab();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("onChange marks the row dirty and forwards to onFieldChange, same as typing", () => {
    const onFieldChange = vi.fn();
    const { gridNav, getCallbacks } = captureCallbacks("note");
    render(<ExerciseRow {...baseProps({ gridNav, onFieldChange })} />);
    expect(gridNav.cellProps).toHaveBeenCalled();
    getCallbacks()!.onChange("left knee sore");
    expect(onFieldChange).toHaveBeenCalledWith("note", "left knee sore");
  });
});

// Specs for MesoTable (P1 multi-week table) — one <table> per training day,
// exercise rows down the side, WEEK COLUMNS across the top. Per-cell editing
// (sets/reps/load+load_type/rpe/rest/note) commits on blur/Enter, carrying
// forward ExerciseRow's dirtySinceFocus semantics (only actually-typed
// fields persist). Deload marker/skipped em-dash/swap badge are display-only
// in P1 — no write UX for swap/skip.
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MesoTable } from "./MesoTable";
import { tableCellDomKey, tableCellAriaLabel } from "../hooks/useTableNav";
import type { GridCell, GridDay, GridRow, GridWeek, GroupIdentity, MesoGrid } from "../lib/api";

function week(overrides: Partial<GridWeek> = {}): GridWeek {
  return {
    id: 1,
    index: 0,
    label: "Wk 1",
    phase: "Accum",
    deload: false,
    current: true,
    delivered_at: null,
    ...overrides,
  };
}

function cell(overrides: Partial<GridCell> = {}): GridCell {
  return {
    prescription_id: 100,
    sets: "3",
    reps: "5",
    load: "100",
    load_type: "abs",
    rpe: "8",
    rest: "90",
    note: "",
    skipped: false,
    swap_name: "",
    swap_exercise_id: null,
    swap_display: "",
    ...overrides,
  };
}

function row(overrides: Partial<GridRow> = {}): GridRow {
  return {
    exercise_slot_id: 9,
    name: "Squat",
    exercise_id: 55,
    order: 0,
    tags: [],
    cells: { "1": cell() },
    ...overrides,
  };
}

function day(overrides: Partial<GridDay> = {}): GridDay {
  return {
    session_slot_id: 1,
    session_id: 11,
    session_ids: { "1": 11 },
    day_number: 1,
    name: "Lower",
    bias: "Quad bias",
    order: 0,
    rows: [row()],
    ...overrides,
  };
}

function grid(overrides: Partial<MesoGrid> = {}): MesoGrid {
  return {
    mesocycle: { id: 1, plan_id: 7, name: "Block 1", week_count: 1 },
    weeks: [week()],
    days: [day()],
    history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" },
    ...overrides,
  };
}

function group(overrides: Partial<GroupIdentity> = {}): GroupIdentity {
  return {
    id: 3,
    name: "Squad",
    member_count: 2,
    members: [
      { id: "a1", name: "Maya Okonkwo", initials: "MO" },
      { id: "a2", name: "Aaron Adams", initials: "AA" },
    ],
    flags: [],
    ...overrides,
  };
}

const HISTORY_NONE = { can_undo: false, can_redo: false, undo_label: "", redo_label: "" };

function baseProps(overrides: Partial<Parameters<typeof MesoTable>[0]> = {}) {
  return {
    grid: grid(),
    history: HISTORY_NONE,
    busy: false,
    unit: "kg",
    group: null,
    onOpenOverride: vi.fn(),
    onPatchCell: vi.fn(),
    onRenameExercise: vi.fn(),
    onAddExercise: vi.fn(),
    onRemoveExercise: vi.fn(),
    onAddDay: vi.fn(),
    onRemoveDay: vi.fn(),
    onAddWeek: vi.fn(),
    onRemoveWeek: vi.fn(),
    onSetCurrentWeek: vi.fn(),
    onUndo: vi.fn(),
    onRedo: vi.fn(),
    onSkipCell: vi.fn(),
    onSwapCell: vi.fn(),
    onFillAcrossWeeks: vi.fn(),
    onAddExerciseThisWeek: vi.fn(),
    onSetOneRm: vi.fn().mockResolvedValue({ one_rm: "", one_rm_source: "" }),
    coachmarkVisible: vi.fn(() => true),
    dismissCoachmark: vi.fn(),
    ...overrides,
  };
}

describe("layout", () => {
  it("renders one table per day", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ session_slot_id: 1, name: "Lower" }), day({ session_slot_id: 2, name: "Upper" })] }),
        })}
      />,
    );
    expect(screen.getByTestId("meso-day-table-1")).toBeInTheDocument();
    expect(screen.getByTestId("meso-day-table-2")).toBeInTheDocument();
  });

  it("renders nothing when grid is null", () => {
    const { container } = render(<MesoTable {...baseProps({ grid: null })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

// Issue #455 phase A4 — the table coachmark. Mirrors WeekGrid.test.tsx's
// "shows the grid coachmark.../hides the grid coachmark..." pair structurally,
// against the new "table" key (lib/coachmarks.ts's COACHMARK_KEYS).
describe("coachmark (issue #455 phase A4)", () => {
  it("shows the table coachmark when coachmarkVisible('table') is true, dismiss wired", async () => {
    const user = userEvent.setup();
    const dismissCoachmark = vi.fn();
    render(<MesoTable {...baseProps({ dismissCoachmark })} />);
    expect(screen.getByText("The block table")).toBeInTheDocument();
    expect(screen.getByText(/every change autosaves/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(dismissCoachmark).toHaveBeenCalledWith("table");
  });

  it("hides the table coachmark when dismissed", () => {
    render(<MesoTable {...baseProps({ coachmarkVisible: vi.fn(() => false) })} />);
    expect(screen.queryByText("The block table")).not.toBeInTheDocument();
  });

  it("renders nothing (including the coachmark) when grid is null", () => {
    const { container } = render(<MesoTable {...baseProps({ grid: null, coachmarkVisible: vi.fn(() => true) })} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("week columns", () => {
  it("renders label, a deload marker, and marks the current week", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [
              week({ id: 1, label: "Wk 1", current: true }),
              week({ id: 2, label: "Wk 2", deload: true, current: false }),
            ],
          }),
        })}
      />,
    );
    const col1 = screen.getByTestId("week-col-1");
    expect(col1).toHaveTextContent("Wk 1");
    expect(col1).toHaveAttribute("aria-current", "true");

    const col2 = screen.getByTestId("week-col-2");
    expect(col2).toHaveTextContent("Wk 2");
    expect(col2).toHaveTextContent("▽");
    expect(col2).not.toHaveAttribute("aria-current");
  });
});

describe("cells", () => {
  it("renders every editable field's value, including rest", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByTestId("cell-sets-100")).toHaveValue("3");
    expect(screen.getByTestId("cell-reps-100")).toHaveValue("5");
    expect(screen.getByTestId("cell-load-100")).toHaveValue("100");
    expect(screen.getByTestId("cell-rpe-100")).toHaveValue("8");
    expect(screen.getByTestId("cell-rest-100")).toHaveValue("90");
    expect(screen.getByTestId("cell-note-100")).toHaveValue("");
  });

  it("commits only the dirtied field on blur", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    const setsInput = screen.getByTestId("cell-sets-100");
    await user.clear(setsInput);
    await user.type(setsInput, "4");
    await user.tab();
    expect(onPatchCell).toHaveBeenCalledWith(100, { sets: "4" });
  });

  it("commits on Enter the same as blur", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    const noteInput = screen.getByTestId("cell-note-100");
    await user.type(noteInput, "left knee sore");
    await user.keyboard("{Enter}");
    expect(onPatchCell).toHaveBeenCalledWith(100, { note: "left knee sore" });
  });

  it("does not call onPatchCell on a no-op focus+blur (dirtySinceFocus gate)", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    await user.click(screen.getByTestId("cell-sets-100"));
    await user.tab();
    expect(onPatchCell).not.toHaveBeenCalled();
  });

  it("toggles load_type immediately on click (not gated by blur)", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    expect(screen.getByTestId("cell-loadtype-100")).toHaveTextContent("kg");
    await user.click(screen.getByTestId("cell-loadtype-100"));
    expect(onPatchCell).toHaveBeenCalledWith(100, { load_type: "pct" });
  });

  it("renders a skipped cell as a read-only em-dash with no inputs", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": cell({ skipped: true }) } })] })] }),
        })}
      />,
    );
    expect(screen.getByTestId("cell-skipped-100")).toHaveTextContent("—");
    expect(screen.queryByTestId("cell-sets-100")).not.toBeInTheDocument();
  });

  it("renders a swap badge alongside editable numbers for a swapped cell", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            days: [
              day({
                rows: [
                  row({
                    cells: {
                      "1": cell({ swap_name: "Leg Press", swap_exercise_id: 77, swap_display: "Leg Press" }),
                    },
                  }),
                ],
              }),
            ],
          }),
        })}
      />,
    );
    expect(screen.getByTestId("cell-swap-100")).toHaveTextContent("Leg Press");
    expect(screen.getByTestId("cell-sets-100")).toBeInTheDocument();
  });

  it("renders the swap badge for a catalog-only swap (swap_name blank, swap_display resolved)", () => {
    // A catalog swap (swap_exercise_id set) with no free-text swap_name is a
    // real gap otherwise: the old `cell.swap_name` gate would render NO badge
    // even though the backend's delivery/logging use the substitute exercise.
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            days: [
              day({
                rows: [
                  row({
                    cells: {
                      "1": cell({ swap_name: "", swap_exercise_id: 77, swap_display: "Hack Squat" }),
                    },
                  }),
                ],
              }),
            ],
          }),
        })}
      />,
    );
    expect(screen.getByTestId("cell-swap-100")).toHaveTextContent("Hack Squat");
    expect(screen.getByTestId("cell-sets-100")).toBeInTheDocument();
  });

  it("renders no swap badge for a plain cell (no swap_display)", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.queryByTestId("cell-swap-100")).not.toBeInTheDocument();
  });
});

describe("row rename", () => {
  it("calls onRenameExercise with the exercise_slot_id and new name on blur", async () => {
    const user = userEvent.setup();
    const onRenameExercise = vi.fn();
    render(<MesoTable {...baseProps({ onRenameExercise })} />);
    const nameInput = screen.getByTestId("row-name-9");
    await user.type(nameInput, "!");
    await user.tab();
    expect(onRenameExercise).toHaveBeenCalledWith(9, "Squat!");
  });
});

// --- Issue #455 phase A3: per-ROW %1RM badge/editor (name column, 2nd line) ---
// A %1RM is a property of the athlete + lift IDENTITY (AthleteOneRm has no
// week dimension), so — unlike the per-cell adjust badge (P5) — this control
// is per-ROW, reading/writing the row's IDENTITY cell (rowIdentityCellId,
// shared with row rename). Mirrors ExerciseRow.tsx's one-week %1RM badge
// (label text, "1RM: "/"1RM ≈ " prefixes, "+ set 1RM").
describe("row 1RM editor (issue #455 phase A3)", () => {
  function pctCell(overrides: Partial<GridCell> = {}) {
    return cell({ load_type: "pct", ...overrides });
  }

  it('renders "1RM: <val> <unit>" for a manual estimate on an individual pct row', () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell({ one_rm: "140", one_rm_source: "manual" }) } })] })] }),
        })}
      />,
    );
    expect(screen.getByTestId("row-one-rm-badge-9")).toHaveTextContent("1RM: 140 kg");
  });

  it('renders "1RM ≈ <val> <unit>" for a logged (derived) estimate', () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell({ one_rm: "140", one_rm_source: "logged" }) } })] })] }),
        })}
      />,
    );
    expect(screen.getByTestId("row-one-rm-badge-9")).toHaveTextContent("1RM ≈ 140 kg");
  });

  it('renders "+ set 1RM" when the athlete has no stored estimate', () => {
    render(<MesoTable {...baseProps({ grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] }) })} />);
    expect(screen.getByTestId("row-one-rm-badge-9")).toHaveTextContent("+ set 1RM");
  });

  it("renders NO badge for an absolute-load row", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": cell({ load_type: "abs", one_rm: "140" }) } })] })] }),
        })}
      />,
    );
    expect(screen.queryByTestId("row-one-rm-badge-9")).not.toBeInTheDocument();
  });

  it("renders NO badge on a group plan, even for a pct row (KEY gating regression: !group, not showAdjust's member-count check)", () => {
    render(
      <MesoTable
        {...baseProps({
          group: group(),
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell({ one_rm: "140" } ) } })] })] }),
        })}
      />,
    );
    expect(screen.queryByTestId("row-one-rm-badge-9")).not.toBeInTheDocument();
  });

  it("renders NO badge when the row has no live cell at all", () => {
    render(<MesoTable {...baseProps({ grid: grid({ days: [day({ rows: [row({ cells: {} })] })] }) })} />);
    expect(screen.queryByTestId("row-one-rm-badge-9")).not.toBeInTheDocument();
  });

  it("renders the badge for a MIXED-load row (identity cell abs, a later week pct)", () => {
    // Codex #455 A3 review: load_type is edited per cell — gating on the
    // identity cell's own load_type alone hid the row's only 1RM control
    // when only a later week carries the % load. The save target stays the
    // identity cell (same lift identity either way).
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })],
            days: [
              day({
                rows: [
                  row({
                    cells: {
                      "1": cell({ prescription_id: 100, load_type: "abs" }),
                      "2": pctCell({ prescription_id: 101 }),
                    },
                  }),
                ],
              }),
            ],
          }),
        })}
      />,
    );
    expect(screen.getByTestId("row-one-rm-badge-9")).toBeInTheDocument();
  });

  it("renders NO badge when the row's only pct cell is a one-week SWAP (different lift identity)", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })],
            days: [
              day({
                rows: [
                  row({
                    cells: {
                      "1": cell({ prescription_id: 100, load_type: "abs" }),
                      "2": pctCell({ prescription_id: 101, swap_name: "Leg Press" }),
                    },
                  }),
                ],
              }),
            ],
          }),
        })}
      />,
    );
    expect(screen.queryByTestId("row-one-rm-badge-9")).not.toBeInTheDocument();
  });

  it("Enter in the editor does not save while the table is busy (keyboard path honors the lock)", async () => {
    const user = userEvent.setup();
    const onSetOneRm = vi.fn().mockResolvedValue({ one_rm: "140", one_rm_source: "manual" });
    const props = baseProps({
      onSetOneRm,
      grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] }),
    });
    const { rerender } = render(<MesoTable {...props} />);
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    await user.type(screen.getByTestId("row-one-rm-input-9"), "140");
    // A structural mutation flips busy while the editor is open.
    rerender(<MesoTable {...props} busy={true} />);
    await user.type(screen.getByTestId("row-one-rm-input-9"), "{Enter}");
    expect(onSetOneRm).not.toHaveBeenCalled();
  });

  it("renders NO badge when the row's only pct cell is skipped", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            days: [day({ rows: [row({ cells: { "1": pctCell({ skipped: true }) } })] })],
          }),
        })}
      />,
    );
    expect(screen.queryByTestId("row-one-rm-badge-9")).not.toBeInTheDocument();
  });

  it("falls back to a live alternative week's identity cell when week[0] has none for this row", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2", current: false })],
            days: [day({ rows: [row({ cells: { "2": pctCell({ prescription_id: 101 }) } })] })],
          }),
        })}
      />,
    );
    expect(screen.getByTestId("row-one-rm-badge-9")).toHaveTextContent("+ set 1RM");
  });

  it("clicking the badge opens the editor seeded with the current value", async () => {
    const user = userEvent.setup();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell({ one_rm: "140" }) } })] })] }),
        })}
      />,
    );
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    expect(screen.getByTestId("row-one-rm-input-9")).toHaveValue("140");
  });

  it("Enter saves the typed value", async () => {
    const user = userEvent.setup();
    const onSetOneRm = vi.fn().mockResolvedValue({ one_rm: "150", one_rm_source: "manual" });
    render(
      <MesoTable
        {...baseProps({
          onSetOneRm,
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] }),
        })}
      />,
    );
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    const input = screen.getByTestId("row-one-rm-input-9");
    await user.clear(input);
    await user.type(input, "150{enter}");
    expect(onSetOneRm).toHaveBeenCalledWith(9, "150");
  });

  it("Escape cancels without saving", async () => {
    const user = userEvent.setup();
    const onSetOneRm = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          onSetOneRm,
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] }),
        })}
      />,
    );
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    const input = screen.getByTestId("row-one-rm-input-9");
    await user.type(input, "999{escape}");
    expect(onSetOneRm).not.toHaveBeenCalled();
    expect(screen.getByTestId("row-one-rm-badge-9")).toBeInTheDocument();
  });

  it("an invalid value shows an inline error and does not call onSetOneRm", async () => {
    const user = userEvent.setup();
    const onSetOneRm = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          onSetOneRm,
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] }),
        })}
      />,
    );
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    const input = screen.getByTestId("row-one-rm-input-9");
    await user.type(input, "abc");
    await user.click(screen.getByTestId("row-one-rm-save-9"));
    expect(screen.getByTestId("row-one-rm-error-9")).toBeInTheDocument();
    expect(onSetOneRm).not.toHaveBeenCalled();
  });

  it("a successful save calls onSetOneRm with the ROW id (not the cell id) and closes the editor", async () => {
    const user = userEvent.setup();
    const onSetOneRm = vi.fn().mockResolvedValue({ one_rm: "150", one_rm_source: "manual" });
    render(
      <MesoTable
        {...baseProps({
          onSetOneRm,
          grid: grid({ days: [day({ rows: [row({ exercise_slot_id: 9, cells: { "1": pctCell({ prescription_id: 100 }) } })] })] }),
        })}
      />,
    );
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    const input = screen.getByTestId("row-one-rm-input-9");
    await user.clear(input);
    await user.type(input, "150");
    await user.click(screen.getByTestId("row-one-rm-save-9"));
    await waitFor(() => expect(onSetOneRm).toHaveBeenCalledWith(9, "150"));
    await waitFor(() => expect(screen.queryByTestId("row-one-rm-input-9")).not.toBeInTheDocument());
  });

  it("a rejected save keeps the editor open with an error", async () => {
    const user = userEvent.setup();
    const onSetOneRm = vi.fn().mockRejectedValue(new Error("boom"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <MesoTable
        {...baseProps({
          onSetOneRm,
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] }),
        })}
      />,
    );
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    const input = screen.getByTestId("row-one-rm-input-9");
    await user.type(input, "150");
    await user.click(screen.getByTestId("row-one-rm-save-9"));
    await waitFor(() => expect(screen.getByTestId("row-one-rm-error-9")).toBeInTheDocument());
    expect(screen.getByTestId("row-one-rm-input-9")).toBeInTheDocument();
  });

  it("disables save/cancel while busy", async () => {
    const user = userEvent.setup();
    const gridFixture = grid({ days: [day({ rows: [row({ cells: { "1": pctCell() } })] })] });
    const { rerender } = render(<MesoTable {...baseProps({ grid: gridFixture })} />);
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    // Local state (the open editor) persists across a prop-only re-render of
    // the same mounted tree — mirrors CellActions' swap-input idiom.
    rerender(<MesoTable {...baseProps({ busy: true, grid: gridFixture })} />);
    expect(screen.getByTestId("row-one-rm-save-9")).toBeDisabled();
    expect(screen.getByTestId("row-one-rm-cancel-9")).toBeDisabled();
  });

  it("badge and input carry NO data-grid-cell (outside A1 keyboard-nav space)", async () => {
    const user = userEvent.setup();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": pctCell({ one_rm: "140" }) } })] })] }),
        })}
      />,
    );
    expect(screen.getByTestId("row-one-rm-badge-9")).not.toHaveAttribute("data-grid-cell");
    await user.click(screen.getByTestId("row-one-rm-badge-9"));
    expect(screen.getByTestId("row-one-rm-input-9")).not.toHaveAttribute("data-grid-cell");
  });
});

describe("remove exercise (arm -> confirm)", () => {
  it("arms then confirms, calling onRemoveExercise", async () => {
    const user = userEvent.setup();
    const onRemoveExercise = vi.fn();
    render(<MesoTable {...baseProps({ onRemoveExercise })} />);
    await user.click(screen.getByTestId("remove-exercise-9"));
    await user.click(screen.getByTestId("confirm-remove-exercise-9"));
    expect(onRemoveExercise).toHaveBeenCalledWith(9);
  });

  it("cancel disarms without calling onRemoveExercise", async () => {
    const user = userEvent.setup();
    const onRemoveExercise = vi.fn();
    render(<MesoTable {...baseProps({ onRemoveExercise })} />);
    await user.click(screen.getByTestId("remove-exercise-9"));
    await user.click(screen.getByTestId("cancel-remove-exercise-9"));
    expect(onRemoveExercise).not.toHaveBeenCalled();
    expect(screen.getByTestId("remove-exercise-9")).toBeInTheDocument();
  });
});

describe("remove day (arm -> confirm)", () => {
  it("arms then confirms, calling onRemoveDay with the day", async () => {
    const user = userEvent.setup();
    const onRemoveDay = vi.fn();
    render(<MesoTable {...baseProps({ onRemoveDay })} />);
    await user.click(screen.getByTestId("remove-day-1"));
    await user.click(screen.getByTestId("confirm-remove-day-1"));
    expect(onRemoveDay).toHaveBeenCalledWith(expect.objectContaining({ session_slot_id: 1 }));
  });
});

describe("weeks: make-current / remove (arm -> confirm)", () => {
  it("make-current calls onSetCurrentWeek", async () => {
    const user = userEvent.setup();
    const onSetCurrentWeek = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ weeks: [week({ id: 1, current: true }), week({ id: 2, label: "Wk 2", current: false })] }),
          onSetCurrentWeek,
        })}
      />,
    );
    await user.click(screen.getByTestId("make-current-2"));
    expect(onSetCurrentWeek).toHaveBeenCalledWith(2);
  });

  it("remove-week arms then confirms, calling onRemoveWeek", async () => {
    const user = userEvent.setup();
    const onRemoveWeek = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ weeks: [week({ id: 1, current: true }), week({ id: 2, label: "Wk 2", current: false })] }),
          onRemoveWeek,
        })}
      />,
    );
    await user.click(screen.getByTestId("remove-week-2"));
    await user.click(screen.getByTestId("confirm-remove-week-2"));
    expect(onRemoveWeek).toHaveBeenCalledWith(2);
  });

  it("does not offer make-current/remove-week for the current week", () => {
    render(<MesoTable {...baseProps({ grid: grid({ weeks: [week({ id: 1, current: true })] }) })} />);
    expect(screen.queryByTestId("make-current-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("remove-week-1")).not.toBeInTheDocument();
  });
});

describe("add affordances", () => {
  it("calls onAddExercise with the day, onAddDay, and onAddWeek", async () => {
    const user = userEvent.setup();
    const onAddExercise = vi.fn();
    const onAddDay = vi.fn();
    const onAddWeek = vi.fn();
    render(<MesoTable {...baseProps({ onAddExercise, onAddDay, onAddWeek })} />);
    await user.click(screen.getByTestId("add-exercise-1"));
    expect(onAddExercise).toHaveBeenCalledWith(expect.objectContaining({ session_slot_id: 1 }));
    await user.click(screen.getByTestId("add-day"));
    expect(onAddDay).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("add-week"));
    expect(onAddWeek).toHaveBeenCalledTimes(1);
  });
});

describe("undo/redo toolbar", () => {
  it("reflects history.can_undo/can_redo and calls onUndo/onRedo", async () => {
    const user = userEvent.setup();
    const onUndo = vi.fn();
    const onRedo = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          history: { can_undo: true, can_redo: false, undo_label: "Edited Squat", redo_label: "" },
          onUndo,
          onRedo,
        })}
      />,
    );
    expect(screen.getByTestId("grid-undo")).not.toBeDisabled();
    expect(screen.getByTestId("grid-redo")).toBeDisabled();
    await user.click(screen.getByTestId("grid-undo"));
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it("disables both buttons while busy", () => {
    render(
      <MesoTable
        {...baseProps({
          history: { can_undo: true, can_redo: true, undo_label: "x", redo_label: "y" },
          busy: true,
        })}
      />,
    );
    expect(screen.getByTestId("grid-undo")).toBeDisabled();
    expect(screen.getByTestId("grid-redo")).toBeDisabled();
  });
});

// --- P2 exceptions: skip / swap / fill / add-this-week write UX -----------
// CONTRACT.md "MesoTable.tsx" — exact data-testids; `id` = prescription_id,
// `slotId` = session_slot_id, `weekId` = week id.

describe("skip / unskip", () => {
  it("clicking skip on a non-skipped cell calls onSkipCell(id, true)", async () => {
    const user = userEvent.setup();
    const onSkipCell = vi.fn();
    render(<MesoTable {...baseProps({ onSkipCell })} />);
    await user.click(screen.getByTestId("cell-skip-100"));
    expect(onSkipCell).toHaveBeenCalledWith(100, true);
  });

  it("clicking unskip on a skipped cell calls onSkipCell(id, false)", async () => {
    const user = userEvent.setup();
    const onSkipCell = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ days: [day({ rows: [row({ cells: { "1": cell({ skipped: true }) } })] })] }),
          onSkipCell,
        })}
      />,
    );
    expect(screen.getByTestId("cell-skipped-100")).toHaveTextContent("—");
    await user.click(screen.getByTestId("cell-unskip-100"));
    expect(onSkipCell).toHaveBeenCalledWith(100, false);
  });
});

describe("swap / clear", () => {
  it("swap: reveals an input, typing a name then saving calls onSwapCell(id, name)", async () => {
    const user = userEvent.setup();
    const onSwapCell = vi.fn();
    render(<MesoTable {...baseProps({ onSwapCell })} />);
    expect(screen.queryByTestId("cell-swap-input-100")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("cell-swap-btn-100"));
    const input = screen.getByTestId("cell-swap-input-100");
    await user.type(input, "Front Squat");
    await user.click(screen.getByTestId("cell-swap-save-100"));
    expect(onSwapCell).toHaveBeenCalledWith(100, "Front Squat");
  });

  it("swap: Enter in the input also submits", async () => {
    const user = userEvent.setup();
    const onSwapCell = vi.fn();
    render(<MesoTable {...baseProps({ onSwapCell })} />);
    await user.click(screen.getByTestId("cell-swap-btn-100"));
    const input = screen.getByTestId("cell-swap-input-100");
    await user.type(input, "Front Squat{Enter}");
    expect(onSwapCell).toHaveBeenCalledWith(100, "Front Squat");
  });

  it('clicking clear on a swapped cell calls onSwapCell(id, "")', async () => {
    const user = userEvent.setup();
    const onSwapCell = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            days: [
              day({
                rows: [
                  row({
                    cells: { "1": cell({ swap_name: "Leg Press", swap_exercise_id: 77, swap_display: "Leg Press" }) },
                  }),
                ],
              }),
            ],
          }),
          onSwapCell,
        })}
      />,
    );
    expect(screen.getByTestId("cell-swap-100")).toHaveTextContent("Leg Press");
    await user.click(screen.getByTestId("cell-swap-clear-100"));
    expect(onSwapCell).toHaveBeenCalledWith(100, "");
  });
});

describe("fill across weeks (arm -> confirm)", () => {
  it("arms then confirms, calling onFillAcrossWeeks(id)", async () => {
    const user = userEvent.setup();
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ onFillAcrossWeeks })} />);
    await user.click(screen.getByTestId("cell-fill-100"));
    await user.click(screen.getByTestId("cell-fill-confirm-100"));
    expect(onFillAcrossWeeks).toHaveBeenCalledWith(100);
  });
});

describe("add exercise this week", () => {
  it("toggling the week picker and clicking a week calls onAddExerciseThisWeek(day, weekId)", async () => {
    const user = userEvent.setup();
    const onAddExerciseThisWeek = vi.fn();
    const targetDay = day({ session_slot_id: 1 });
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [week({ id: 1, current: true }), week({ id: 2, label: "Wk 2", current: false })],
            days: [targetDay],
          }),
          onAddExerciseThisWeek,
        })}
      />,
    );
    expect(screen.queryByTestId("add-this-week-1-2")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("add-this-week-1"));
    await user.click(screen.getByTestId("add-this-week-1-2"));
    expect(onAddExerciseThisWeek).toHaveBeenCalledWith(expect.objectContaining({ session_slot_id: 1 }), 2);
  });
});

// --- P5 group: per-cell per-athlete adjust badge ---------------------------
// Mirrors ExerciseRow's group "+ adjust"/`ex.adj` badge (.meso-adjust-badge /
// .meso-adjust-empty, testid override-badge-<id>), but scoped PER CELL on the
// multi-week table — testid `cell-override-badge-<prescription_id>`. Only
// shown when a `group` with members is passed (individual plans carry no
// group) and never on a skipped cell. Clicking it hands (row, cell) up to
// DesignerRoot, which synthesizes an Exercise and opens the override editor.
describe("group per-athlete adjust badge", () => {
  const adjustsFor = (id: string) => [
    { id, name: "Maya Okonkwo", initials: "MO", label: "-10%", swap: "", load_pct: 90, sets: "", reps: "", note: "" },
  ];

  it("renders the adj summary as the badge text for a group cell that has one", () => {
    render(
      <MesoTable
        {...baseProps({
          group: group(),
          grid: grid({
            days: [day({ rows: [row({ cells: { "1": cell({ adj: "MO -10%", adjusts: adjustsFor("a1") }) } })] })],
          }),
        })}
      />,
    );
    expect(screen.getByTestId("cell-override-badge-100")).toHaveTextContent("MO -10%");
  });

  it('renders a "+ adjust" affordance for a group cell with no adj', () => {
    render(<MesoTable {...baseProps({ group: group() })} />);
    expect(screen.getByTestId("cell-override-badge-100")).toHaveTextContent("+ adjust");
  });

  it("renders NO adjust control for an individual plan (group is null)", () => {
    render(<MesoTable {...baseProps({ group: null })} />);
    expect(screen.queryByTestId("cell-override-badge-100")).not.toBeInTheDocument();
  });

  it("renders no adjust control for a skipped cell", () => {
    render(
      <MesoTable
        {...baseProps({
          group: group(),
          grid: grid({ days: [day({ rows: [row({ cells: { "1": cell({ skipped: true }) } })] })] }),
        })}
      />,
    );
    expect(screen.queryByTestId("cell-override-badge-100")).not.toBeInTheDocument();
  });

  it("clicking the badge calls onOpenOverride with the owning row and cell", async () => {
    const user = userEvent.setup();
    const onOpenOverride = vi.fn();
    render(<MesoTable {...baseProps({ group: group(), onOpenOverride })} />);
    await user.click(screen.getByTestId("cell-override-badge-100"));
    expect(onOpenOverride).toHaveBeenCalledWith(
      expect.objectContaining({ exercise_slot_id: 9 }),
      expect.objectContaining({ prescription_id: 100 }),
    );
  });
});

// --- Issue #455 phase A1: keyboard grid navigation ------------------------
// useTableNav (../hooks/useTableNav) is instantiated ONCE inside MesoTable —
// GridCellEditor/RowNameEditor are module-private, so there's no externally
// injectable gridNav prop and no INERT fallback to test; every spec here
// renders the real table and drives real keyboard events through RTL,
// mirroring WeekGrid.test.tsx's "Phase 3" block (the one-week precedent).
//
// Fixture: day 1 (session_slot_id 1) has row 9 "Box Squat" (cells 900/901)
// and row 10 "RDL" (cells 1000/1001); day 2 (session_slot_id 2) has row 11
// "Bench" (cells 1100/1101). Two weeks (id 1 "Wk 1", id 2 "Wk 2") so
// ArrowRight/Left week-crossing is exercisable directly.
const NAV_GRID: MesoGrid = grid({
  weeks: [week({ id: 1, label: "Wk 1", current: true }), week({ id: 2, label: "Wk 2", current: false })],
  days: [
    day({
      session_slot_id: 1,
      name: "Lower",
      rows: [
        row({
          exercise_slot_id: 9,
          name: "Box Squat",
          cells: { "1": cell({ prescription_id: 900 }), "2": cell({ prescription_id: 901 }) },
        }),
        row({
          exercise_slot_id: 10,
          name: "RDL",
          cells: { "1": cell({ prescription_id: 1000 }), "2": cell({ prescription_id: 1001 }) },
        }),
      ],
    }),
    day({
      session_slot_id: 2,
      name: "Upper",
      rows: [
        row({
          exercise_slot_id: 11,
          name: "Bench",
          cells: { "1": cell({ prescription_id: 1100 }), "2": cell({ prescription_id: 1101 }) },
        }),
      ],
    }),
  ],
});

describe("keyboard grid navigation", () => {
  describe("data-grid-cell + aria-label", () => {
    it("stamps data-grid-cell on every field input and the row-name input, keyed by (rowId, weekId, field)", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, null, "name"));
      expect(screen.getByTestId("cell-sets-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "sets"));
      expect(screen.getByTestId("cell-reps-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "reps"));
      expect(screen.getByTestId("cell-load-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "load"));
      expect(screen.getByTestId("cell-rpe-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "rpe"));
      expect(screen.getByTestId("cell-rest-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "rest"));
      expect(screen.getByTestId("cell-note-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "note"));
      expect(screen.getByTestId("cell-sets-901")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 2, "sets"));
    });

    it("gives every field input an aria-label reflecting row/week/field, and the name input its own", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      expect(screen.getByTestId("cell-sets-900")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", "Wk 1", "sets"));
      expect(screen.getByTestId("cell-rpe-900")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", "Wk 1", "rpe"));
      expect(screen.getByTestId("cell-sets-901")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", "Wk 2", "sets"));
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", null, "name"));
    });
  });

  describe("roving tabindex", () => {
    it("exactly one cell (the table's first: row-name) is tabbable on initial render", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("tabindex", "0");
      expect(screen.getByTestId("cell-sets-900")).toHaveAttribute("tabindex", "-1");
      expect(screen.getByTestId("row-name-10")).toHaveAttribute("tabindex", "-1");
      expect(screen.getByTestId("row-name-11")).toHaveAttribute("tabindex", "-1");
    });

    it("focusing another cell moves the roving tabIndex to it", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-rpe-1000"));
      expect(screen.getByTestId("cell-rpe-1000")).toHaveAttribute("tabindex", "0");
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("tabindex", "-1");
    });

    it("roving tabIndex spans multiple day-tables as ONE grid", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-load-1000")); // day 1's last row
      await user.keyboard("{ArrowDown}");
      expect(screen.getByTestId("cell-load-1100")).toHaveFocus(); // day 2's row
      expect(screen.getByTestId("cell-load-1100")).toHaveAttribute("tabindex", "0");
    });
  });

  describe("ArrowDown / ArrowUp navigate rows across day-table boundaries", () => {
    it("ArrowDown moves focus to the same field on the next row within a day", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-sets-900"));
      await user.keyboard("{ArrowDown}");
      expect(screen.getByTestId("cell-sets-1000")).toHaveFocus();
    });

    it("ArrowDown crosses a day-table boundary (last row of day 1 -> first row of day 2)", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-load-1000"));
      await user.keyboard("{ArrowDown}");
      expect(screen.getByTestId("cell-load-1100")).toHaveFocus();
    });

    it("ArrowUp mirrors ArrowDown, crossing day boundaries upward", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-rpe-1100"));
      await user.keyboard("{ArrowUp}");
      expect(screen.getByTestId("cell-rpe-1000")).toHaveFocus();
    });
  });

  describe("ArrowRight / ArrowLeft move fields only at caret extremes, crossing weeks and the name column", () => {
    it("ArrowRight at the end of the text moves to the next field within the same week", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const setsInput = screen.getByTestId("cell-sets-900") as HTMLInputElement;
      setsInput.focus();
      setsInput.setSelectionRange(setsInput.value.length, setsInput.value.length);
      fireEvent.keyDown(setsInput, { key: "ArrowRight" });
      expect(screen.getByTestId("cell-reps-900")).toHaveFocus();
    });

    it("ArrowLeft at the start of the text moves to the previous field within the same week", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const repsInput = screen.getByTestId("cell-reps-900") as HTMLInputElement;
      repsInput.focus();
      repsInput.setSelectionRange(0, 0);
      fireEvent.keyDown(repsInput, { key: "ArrowLeft" });
      expect(screen.getByTestId("cell-sets-900")).toHaveFocus();
    });

    it("ArrowRight at the last field of week 1 crosses into week 2's first field", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const noteInput = screen.getByTestId("cell-note-900") as HTMLInputElement;
      noteInput.focus();
      noteInput.setSelectionRange(noteInput.value.length, noteInput.value.length);
      fireEvent.keyDown(noteInput, { key: "ArrowRight" });
      expect(screen.getByTestId("cell-sets-901")).toHaveFocus();
    });

    it("ArrowRight at the end of the name column moves into week 1's sets field", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const nameInput = screen.getByTestId("row-name-9") as HTMLInputElement;
      nameInput.focus();
      nameInput.setSelectionRange(nameInput.value.length, nameInput.value.length);
      fireEvent.keyDown(nameInput, { key: "ArrowRight" });
      expect(screen.getByTestId("cell-sets-900")).toHaveFocus();
    });

    it("ArrowLeft at the start of week 1's sets field moves back to the name column", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const setsInput = screen.getByTestId("cell-sets-900") as HTMLInputElement;
      setsInput.focus();
      setsInput.setSelectionRange(0, 0);
      fireEvent.keyDown(setsInput, { key: "ArrowLeft" });
      expect(screen.getByTestId("row-name-9")).toHaveFocus();
    });
  });

  describe("Enter commits / Escape reverts (integrated)", () => {
    it("Enter commits only when the cell is dirty, and keeps focus in place", async () => {
      const user = userEvent.setup();
      const onPatchCell = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onPatchCell })} />);
      const loadInput = screen.getByTestId("cell-load-900");
      await user.click(loadInput);
      await user.keyboard("{Enter}"); // clean cell: no-op (existing dirty gate)
      expect(onPatchCell).not.toHaveBeenCalled();
      await user.clear(loadInput);
      await user.type(loadInput, "150");
      await user.keyboard("{Enter}");
      expect(onPatchCell).toHaveBeenCalledWith(900, { load: "150" });
      expect(onPatchCell).toHaveBeenCalledTimes(1);
      expect(loadInput).toHaveFocus();
    });

    it("Escape reverts only the focused field's draft, leaving a second dirty field on the same cell untouched", () => {
      // A per-cell dirty Set (not a per-row boolean) needs per-field removal
      // — revertField (MesoTable.tsx) is the new code under test here.
      // fireEvent.change without a prior real .focus() dirties "load"
      // without giving it real DOM focus, so focusing "sets" next doesn't
      // trigger a real blur-commit of "load" and contaminate the assertion.
      const onPatchCell = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onPatchCell })} />);
      const setsInput = screen.getByTestId("cell-sets-900") as HTMLInputElement;
      const loadInput = screen.getByTestId("cell-load-900") as HTMLInputElement;

      fireEvent.change(loadInput, { target: { value: "150" } });

      setsInput.focus();
      fireEvent.change(setsInput, { target: { value: "9" } });
      fireEvent.keyDown(setsInput, { key: "Escape" });

      expect(setsInput).toHaveValue("3"); // reverted to the original cell value
      expect(loadInput).toHaveValue("150"); // untouched dirty draft survives

      fireEvent.blur(setsInput); // commits whatever's still dirty on this cell
      expect(onPatchCell).toHaveBeenCalledWith(900, { load: "150" });
      expect(onPatchCell).not.toHaveBeenCalledWith(900, expect.objectContaining({ sets: expect.anything() }));
    });

    it("Escape keeps focus on the field and suppresses its next blur-commit", () => {
      const onPatchCell = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onPatchCell })} />);
      const setsInput = screen.getByTestId("cell-sets-900") as HTMLInputElement;
      setsInput.focus();
      fireEvent.change(setsInput, { target: { value: "9" } });
      fireEvent.keyDown(setsInput, { key: "Escape" });
      expect(setsInput).toHaveValue("3");
      expect(setsInput).toHaveFocus();
      fireEvent.blur(setsInput);
      expect(onPatchCell).not.toHaveBeenCalled();
    });
  });

  describe("data-grid-restore integration", () => {
    it("refocuses the replacement cell after a fresh grid identity from a data-grid-restore control (Undo)", () => {
      const { rerender } = render(
        <MesoTable
          {...baseProps({
            grid: NAV_GRID,
            history: { can_undo: true, can_redo: false, undo_label: "Edited", redo_label: "" },
          })}
        />,
      );
      const setsInput = screen.getByTestId("cell-sets-900") as HTMLInputElement;
      setsInput.focus();

      // Simulates the coach clicking Undo — real DOM focus moves to the
      // (data-grid-restore-marked) button before the refetch resolves.
      const undoButton = screen.getByTestId("grid-undo") as HTMLButtonElement;
      undoButton.focus();

      const NEXT_GRID = grid({ weeks: NAV_GRID.weeks, days: NAV_GRID.days });
      rerender(
        <MesoTable
          {...baseProps({
            grid: NEXT_GRID,
            history: { can_undo: false, can_redo: true, undo_label: "", redo_label: "Edited" },
          })}
        />,
      );

      expect(screen.getByTestId("cell-sets-900")).toHaveFocus();
    });

    it("clicking the unmarked load-type toggle across a grid identity change does NOT steal focus", () => {
      const { rerender } = render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const setsInput = screen.getByTestId("cell-sets-900") as HTMLInputElement;
      setsInput.focus();

      const toggle = screen.getByTestId("cell-loadtype-900") as HTMLButtonElement;
      toggle.focus(); // the load-type toggle is intentionally NOT data-grid-restore

      const NEXT_GRID = grid({ weeks: NAV_GRID.weeks, days: NAV_GRID.days });
      rerender(<MesoTable {...baseProps({ grid: NEXT_GRID })} />);

      expect(toggle).toHaveFocus();
    });
  });
});

// Issue #455 review nit: arrow moves must skid past holes/skipped cells
// (no `data-grid-cell` node) to the next RENDERED cell rather than
// committing the anchor to a coordinate nothing renders — see
// useTableNav.test.tsx's "hole-skidding" describe for the hook-level specs.
// This is the one integration check that drives it through the real
// MesoTable render (a skipped cell is display-only real-app coverage the
// hook's own unit tests, mounting bare DOM nodes, can't provide).
describe("keyboard grid navigation: skidding over a skipped cell (issue #455 review nit)", () => {
  it("ArrowRight from the prior week's note field skips an entire skipped-week cell and lands on the following week's sets input", () => {
    const SKIP_GRID = grid({
      weeks: [
        week({ id: 1, label: "Wk 1", current: true }),
        week({ id: 2, label: "Wk 2", current: false }),
        week({ id: 3, label: "Wk 3", current: false }),
      ],
      days: [
        day({
          session_slot_id: 1,
          name: "Lower",
          rows: [
            row({
              exercise_slot_id: 9,
              name: "Box Squat",
              cells: {
                "1": cell({ prescription_id: 900 }),
                "2": cell({ prescription_id: 901, skipped: true }),
                "3": cell({ prescription_id: 902 }),
              },
            }),
          ],
        }),
      ],
    });
    render(<MesoTable {...baseProps({ grid: SKIP_GRID })} />);

    // Sanity: week 2 really is the skipped em-dash display, not an input.
    expect(screen.getByTestId("cell-skipped-901")).toHaveTextContent("—");
    expect(screen.queryByTestId("cell-sets-901")).not.toBeInTheDocument();

    const noteInput = screen.getByTestId("cell-note-900") as HTMLInputElement;
    noteInput.focus();
    noteInput.setSelectionRange(noteInput.value.length, noteInput.value.length);
    fireEvent.keyDown(noteInput, { key: "ArrowRight" });

    expect(screen.getByTestId("cell-sets-902")).toHaveFocus();
    expect(screen.getByTestId("cell-sets-902")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("cell-note-900")).toHaveAttribute("tabindex", "-1");
  });
});

// --- Issue #455 phase A2: drag reordering (row + day) -----------------------
// Real pointer/keyboard dnd-kit drags are browser-only-verifiable (see
// useTableReorder.ts's header + the WeekGrid/DayCard precedent, which never
// simulates an actual drag through RTL either) — these specs pin the seams
// MesoTable itself owns: the handles' rendering/a11y, and the three pure
// functions its DndContext wires its sensors to (mirrors WeekGrid.test.tsx's
// "keyboard drag candidate filtering" / "typedCollisionDetection" /
// "typedKeyboardCoordinates" suites). onDragEnd's own translation logic is
// covered by useTableReorder.test.ts.
describe("drag handles", () => {
  it("renders a row drag handle with testid/type/aria-label, first in the name cell", () => {
    render(<MesoTable {...baseProps()} />);
    const handle = screen.getByTestId("row-drag-9");
    expect(handle).toHaveAttribute("type", "button");
    expect(handle).toHaveAttribute("aria-label", "Reorder Squat");
  });

  it("falls back to a generic row handle label for a blank exercise name", () => {
    render(<MesoTable {...baseProps({ grid: grid({ days: [day({ rows: [row({ name: "" })] })] }) })} />);
    expect(screen.getByTestId("row-drag-9")).toHaveAttribute("aria-label", "Reorder exercise");
  });

  it("the row handle carries no data-grid-restore (Undo/etc. must not steal-refocus it)", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByTestId("row-drag-9")).not.toHaveAttribute("data-grid-restore");
  });

  it("the row handle carries no data-grid-cell (outside useTableNav's grid entirely)", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByTestId("row-drag-9")).not.toHaveAttribute("data-grid-cell");
  });

  it("disables the row handle while busy", () => {
    render(<MesoTable {...baseProps({ busy: true })} />);
    expect(screen.getByTestId("row-drag-9")).toBeDisabled();
  });

  it("renders a day drag handle with testid/type/aria-label, first in the day header", () => {
    render(<MesoTable {...baseProps()} />);
    const handle = screen.getByTestId("day-drag-1");
    expect(handle).toHaveAttribute("type", "button");
    expect(handle).toHaveAttribute("aria-label", "Reorder Lower");
  });

  it("falls back to 'Reorder Day <n>' for a blank day name", () => {
    render(<MesoTable {...baseProps({ grid: grid({ days: [day({ name: "", day_number: 2 })] }) })} />);
    expect(screen.getByTestId("day-drag-1")).toHaveAttribute("aria-label", "Reorder Day 2");
  });

  it("the day handle carries no data-grid-restore", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByTestId("day-drag-1")).not.toHaveAttribute("data-grid-restore");
  });

  it("disables the day handle while busy", () => {
    render(<MesoTable {...baseProps({ busy: true })} />);
    expect(screen.getByTestId("day-drag-1")).toBeDisabled();
  });
});

describe("filterTableDragCandidates (row drags stay within their own day; day drags target only days)", () => {
  it("keeps only day containers for a day-active drag", async () => {
    const { filterTableDragCandidates } = await import("./MesoTable");
    const containers = [{ id: "day-1" }, { id: "row-1-9" }, { id: "row-2-11" }, { id: "day-2" }] as never[];
    expect(filterTableDragCandidates("day-1", containers).map((c: { id: string }) => c.id)).toEqual([
      "day-1",
      "day-2",
    ]);
  });

  it("keeps only SAME-DAY row containers for a row-active drag (cross-day is OUT of A2 scope)", async () => {
    const { filterTableDragCandidates } = await import("./MesoTable");
    const containers = [
      { id: "day-1" },
      { id: "row-1-9" },
      { id: "row-1-10" },
      { id: "row-2-11" },
      { id: "day-2" },
    ] as never[];
    expect(filterTableDragCandidates("row-1-9", containers).map((c: { id: string }) => c.id)).toEqual([
      "row-1-9",
      "row-1-10",
    ]);
  });
});

describe("tableCollisionDetection (day drags collide only with day containers)", () => {
  it("returns only day collisions for a day drag", async () => {
    const { tableCollisionDetection } = await import("./MesoTable");
    const rect = (top: number) => ({ top, bottom: top + 200, left: 0, right: 800, width: 800, height: 200 });
    const containers = [
      { id: "day-1", rect: { current: rect(0) }, data: { current: {} }, disabled: false },
      { id: "row-1-9", rect: { current: rect(40) }, data: { current: {} }, disabled: false },
      { id: "day-2", rect: { current: rect(220) }, data: { current: {} }, disabled: false },
    ];
    const collisions = tableCollisionDetection({
      active: { id: "day-2", rect: { current: { initial: rect(220), translated: rect(10) } }, data: { current: {} } },
      collisionRect: rect(10),
      droppableRects: new Map(containers.map((c) => [c.id, c.rect.current])),
      droppableContainers: containers,
      pointerCoordinates: null,
    } as never);
    expect(collisions.length).toBeGreaterThan(0);
    expect(collisions.every((c: { id: unknown }) => String(c.id).startsWith("day-"))).toBe(true);
    expect(String(collisions[0]!.id)).toBe("day-1");
  });

  it("returns NO collision when a row is dropped nowhere near its own day's candidates (no phantom same-day reorder)", async () => {
    // closestCenter would have snapped this far-away drop to the nearest
    // same-day row and committed an unintended reorder; intersection-based
    // collision leaves `over` null so the drop no-ops (Codex #455 A2 review).
    const { tableCollisionDetection } = await import("./MesoTable");
    const rect = (top: number, height = 40) => ({ top, bottom: top + height, left: 0, right: 800, width: 800, height });
    const containers = [
      { id: "row-1-9", rect: { current: rect(0) }, data: { current: {} }, disabled: false },
      { id: "row-1-10", rect: { current: rect(40) }, data: { current: {} }, disabled: false },
      { id: "row-2-11", rect: { current: rect(400) }, data: { current: {} }, disabled: false },
    ];
    const collisions = tableCollisionDetection({
      active: { id: "row-1-9", rect: { current: { initial: rect(0), translated: rect(400) } }, data: { current: {} } },
      collisionRect: rect(400), // dropped over day 2's territory — no same-day candidate there
      droppableRects: new Map(containers.map((c) => [c.id, c.rect.current])),
      droppableContainers: containers,
      pointerCoordinates: { x: 100, y: 420 },
    } as never);
    expect(collisions).toEqual([]);
  });

  it("returns the same-day row under the pointer for an in-day row drag", async () => {
    const { tableCollisionDetection } = await import("./MesoTable");
    const rect = (top: number, height = 40) => ({ top, bottom: top + height, left: 0, right: 800, width: 800, height });
    const containers = [
      { id: "row-1-9", rect: { current: rect(0) }, data: { current: {} }, disabled: false },
      { id: "row-1-10", rect: { current: rect(40) }, data: { current: {} }, disabled: false },
    ];
    const collisions = tableCollisionDetection({
      active: { id: "row-1-10", rect: { current: { initial: rect(40), translated: rect(10) } }, data: { current: {} } },
      collisionRect: rect(10),
      droppableRects: new Map(containers.map((c) => [c.id, c.rect.current])),
      droppableContainers: containers,
      pointerCoordinates: { x: 100, y: 20 },
    } as never);
    expect(collisions.length).toBeGreaterThan(0);
    expect(String(collisions[0]!.id)).toBe("row-1-9");
  });
});

describe("tableKeyboardCoordinates delegates to the real droppable map", () => {
  // dnd-kit's DroppableContainersMap is a real Map subclass; a spread/assign
  // clone borrows its prototype without Map internal slots, so .get() throws
  // "called on incompatible receiver" and keyboard reordering dies silently
  // (see WeekGrid.tsx's typedKeyboardCoordinates, ported verbatim-adapted).
  it("returns coordinates for a day drag without throwing on Map methods", async () => {
    const { tableKeyboardCoordinates } = await import("./MesoTable");
    class FakeContainers extends Map<string, unknown> {
      getEnabled() {
        return [...this.values()];
      }
    }
    const mk = (id: string, top: number) => {
      const node = document.createElement("div");
      document.body.appendChild(node);
      return {
        id,
        disabled: false,
        node: { current: node },
        data: { current: { sortable: { containerId: "table", index: 0, items: [] } } },
        rect: { current: { top, bottom: top + 100, left: 0, right: 800, width: 800, height: 100 } },
      };
    };
    const containers = new FakeContainers();
    for (const c of [mk("day-1", 0), mk("row-1-9", 30), mk("day-2", 200)]) containers.set(c.id, c);
    const rects = new Map(
      [...containers.values()].map((c) => {
        const e = c as { id: string; rect: { current: unknown } };
        return [e.id, e.rect.current] as const;
      }),
    );
    const event = new KeyboardEvent("keydown", { code: "ArrowDown", key: "ArrowDown" });
    const coords = tableKeyboardCoordinates(event, {
      currentCoordinates: { x: 0, y: 0 },
      context: {
        active: { id: "day-1" },
        over: null,
        collisionRect: { top: 0, bottom: 100, left: 0, right: 800, width: 800, height: 100 },
        droppableRects: rects,
        droppableContainers: containers,
        scrollableAncestors: [],
      },
    } as never);
    expect(coords).toBeTruthy();
    // Proposed target must be day-2 (top 200), never row-1-9 (top 30).
    expect(coords!.y).toBeGreaterThanOrEqual(150);
  });
});

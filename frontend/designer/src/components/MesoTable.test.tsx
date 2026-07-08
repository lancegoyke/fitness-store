// Specs for MesoTable (P1 multi-week table) — one <table> per training day,
// exercise rows down the side, WEEK COLUMNS across the top. Per-cell editing
// (sets/reps/load+load_type/rpe/rest/note) commits on blur/Enter, carrying
// forward ExerciseRow's dirtySinceFocus semantics (only actually-typed
// fields persist). Deload marker/skipped em-dash/swap badge are display-only
// in P1 — no write UX for swap/skip.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MesoTable } from "./MesoTable";
import type { GridCell, GridDay, GridRow, GridWeek, MesoGrid } from "../lib/api";

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

const HISTORY_NONE = { can_undo: false, can_redo: false, undo_label: "", redo_label: "" };

function baseProps(overrides: Partial<Parameters<typeof MesoTable>[0]> = {}) {
  return {
    grid: grid(),
    history: HISTORY_NONE,
    busy: false,
    unit: "kg",
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
            days: [day({ rows: [row({ cells: { "1": cell({ swap_name: "Leg Press", swap_exercise_id: 77 }) } })] })],
          }),
        })}
      />,
    );
    expect(screen.getByTestId("cell-swap-100")).toHaveTextContent("Leg Press");
    expect(screen.getByTestId("cell-sets-100")).toBeInTheDocument();
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

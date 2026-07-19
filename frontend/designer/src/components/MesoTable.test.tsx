// Specs for MesoTable (P1 multi-week table) — one <table> per training day,
// exercise rows down the side, WEEK COLUMNS across the top. Phase 2a
// (text-first cells): each cell is ONE freeform text input (committed on
// blur/Enter, carrying forward ExerciseRow's dirtySinceFocus semantics) plus
// sub-line inputs and a trailing ghost input that mints the next sub-line;
// Tempo/Notes/Rest are per-ROW columns off the slot. The %1RM editor, the
// load_type toggle, and the one-week swap UI are retired.
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MesoTable } from "./MesoTable";
import { tableCellDomKey, tableCellAriaLabel } from "../hooks/useTableNav";
import type { GridCell, GridDay, GridRow, GridWeek, MesoGrid } from "../lib/api";

function week(overrides: Partial<GridWeek> = {}): GridWeek {
  return {
    id: 1,
    index: 0,
    label: "Wk 1",
    phase: "Accum",
    deload: false,
    delivered_at: null,
    ...overrides,
  };
}

function cell(overrides: Partial<GridCell> = {}): GridCell {
  return {
    prescription_id: 100,
    text: "3 x 5, RPE 8, 100",
    skipped: false,
    lines: [],
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
    tempo: "",
    rest: "",
    note: "",
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

function baseProps(overrides: Partial<Parameters<typeof MesoTable>[0]> = {}) {
  return {
    grid: grid(),
    busy: false,
    onPatchCell: vi.fn(),
    onWriteCellLine: vi.fn(),
    onPatchRowColumns: vi.fn(),
    onRenameExercise: vi.fn(),
    onAddExercise: vi.fn(),
    onRemoveExercise: vi.fn(),
    onAddDay: vi.fn(),
    onRemoveDay: vi.fn(),
    onAddWeek: vi.fn(),
    onRemoveWeek: vi.fn(),
    onSkipCell: vi.fn(),
    onFillAcrossWeeks: vi.fn(),
    onAddExerciseThisWeek: vi.fn(),
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
  it("renders each week's label and deload marker (no current-week highlight — programs are date-less)", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2", deload: true })],
          }),
        })}
      />,
    );
    const col1 = screen.getByTestId("week-col-1");
    expect(col1).toHaveTextContent("Wk 1");
    expect(col1).not.toHaveAttribute("aria-current");

    const col2 = screen.getByTestId("week-col-2");
    expect(col2).toHaveTextContent("Wk 2");
    expect(col2).toHaveTextContent("▽");
    expect(col2).not.toHaveAttribute("aria-current");
  });
});

describe("cells", () => {
  it("renders the cell's freeform text verbatim", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByTestId("cell-text-100")).toHaveValue("3 x 5, RPE 8, 100");
  });

  it("commits {text} on blur when dirty", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    const textInput = screen.getByTestId("cell-text-100");
    await user.clear(textInput);
    await user.type(textInput, "4 x 6, RPE 9");
    await user.tab();
    expect(onPatchCell).toHaveBeenCalledWith(100, { text: "4 x 6, RPE 9" });
  });

  it("commits on Enter the same as blur", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    const textInput = screen.getByTestId("cell-text-100");
    await user.clear(textInput);
    await user.type(textInput, "AMRAP");
    await user.keyboard("{Enter}");
    expect(onPatchCell).toHaveBeenCalledWith(100, { text: "AMRAP" });
  });

  it("does not call onPatchCell on a no-op focus+blur (dirtySinceFocus gate)", async () => {
    const user = userEvent.setup();
    const onPatchCell = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell })} />);
    await user.click(screen.getByTestId("cell-text-100"));
    await user.tab();
    expect(onPatchCell).not.toHaveBeenCalled();
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
    expect(screen.queryByTestId("cell-text-100")).not.toBeInTheDocument();
  });
});

// --- Phase 2a: freeform sub-lines (cell.lines) + the ghost input -----------
// One input per existing sub-line, edited via onWriteCellLine (upsert by
// (slot, week, line) — the line may not have a pk yet), plus a trailing
// ghost input that mints the NEXT line (max existing + 1, or 1) on its first
// non-blank commit. Blanking an existing line commits "" (clears in place).
describe("cell sub-lines", () => {
  const LINES = [
    { id: 5, line: 1, text: "RPE 8" },
    { id: 6, line: 2, text: "slow eccentric" },
  ];

  function linesGrid() {
    return grid({ days: [day({ rows: [row({ cells: { "1": cell({ lines: LINES }) } })] })] });
  }

  it("renders one input per existing sub-line, in order, plus the ghost", () => {
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    expect(screen.getByTestId("cell-line-100-1")).toHaveValue("RPE 8");
    expect(screen.getByTestId("cell-line-100-2")).toHaveValue("slow eccentric");
    expect(screen.getByTestId("cell-line-new-100")).toHaveValue("");
  });

  // designer-simplify: the ghost must stay a real, focusable keyboard grid
  // stop AT ALL TIMES — ArrowUp from the row below reaches it via
  // useTableNav's querySelector(...).focus(), and a shipped regression once
  // hid it until :focus-within, which made .focus() a no-op and stranded the
  // grid anchor (docs/meso/decisions.md ~1567-1585). It must render, and be
  // focusable, with nothing else in the table holding focus.
  it("the ghost cell-line-new-<id> is rendered and focusable without the cell having focus", () => {
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    const ghost = screen.getByTestId("cell-line-new-100");
    expect(document.activeElement).not.toBe(ghost); // nothing in the table holds focus yet
    ghost.focus();
    expect(ghost).toHaveFocus();
  });

  it("editing a sub-line commits via onWriteCellLine(slotId, weekId, line, text) on blur", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onWriteCellLine })} />);
    const input = screen.getByTestId("cell-line-100-1");
    await user.clear(input);
    await user.type(input, "RPE 9");
    await user.tab();
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 1, "RPE 9");
  });

  it("Enter commits a sub-line the same as blur", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onWriteCellLine })} />);
    const input = screen.getByTestId("cell-line-100-2");
    await user.clear(input);
    await user.type(input, "pause at pins");
    await user.keyboard("{Enter}");
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 2, "pause at pins");
  });

  it('blanking an existing sub-line commits "" (clears in place, the row stays)', async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onWriteCellLine })} />);
    await user.clear(screen.getByTestId("cell-line-100-1"));
    await user.tab();
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 1, "");
  });

  it("a clean sub-line blur is a no-op (dirty gate)", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onWriteCellLine })} />);
    await user.click(screen.getByTestId("cell-line-100-1"));
    await user.tab();
    expect(onWriteCellLine).not.toHaveBeenCalled();
  });

  it("Escape reverts a sub-line draft without committing", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onWriteCellLine })} />);
    const input = screen.getByTestId("cell-line-100-1");
    await user.clear(input);
    await user.type(input, "garbage{Escape}");
    expect(input).toHaveValue("RPE 8");
    await user.tab();
    expect(onWriteCellLine).not.toHaveBeenCalled();
  });

  it("the ghost input mints the NEXT line (max existing + 1) on a non-blank commit", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onWriteCellLine })} />);
    const ghost = screen.getByTestId("cell-line-new-100");
    await user.type(ghost, "Cable Crunch");
    await user.tab();
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 3, "Cable Crunch");
  });

  it("the ghost mints line 1 when the cell has no sub-lines yet", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ onWriteCellLine })} />); // default fixture: lines []
    const ghost = screen.getByTestId("cell-line-new-100");
    await user.type(ghost, "RPE 8");
    await user.keyboard("{Enter}");
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 1, "RPE 8");
  });

  it("a blank (or blanked-back) ghost commit creates nothing", async () => {
    const user = userEvent.setup();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ onWriteCellLine })} />);
    const ghost = screen.getByTestId("cell-line-new-100");
    await user.type(ghost, "x");
    await user.clear(ghost);
    await user.tab();
    expect(onWriteCellLine).not.toHaveBeenCalled();
  });

  it("sub-line and ghost inputs carry line-keyed data-grid-cell (INSIDE nav since 2b)", () => {
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    expect(screen.getByTestId("cell-line-100-1")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "text", 1));
    expect(screen.getByTestId("cell-line-100-2")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "text", 2));
    expect(screen.getByTestId("cell-line-new-100")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "text", 3));
  });

  it("ArrowDown from the prescription steps INTO the stack: line 1, line 2, ghost, in order (D3)", async () => {
    const user = userEvent.setup();
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    await user.click(screen.getByTestId("cell-text-100"));
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("cell-line-100-1")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("cell-line-100-2")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("cell-line-new-100")).toHaveFocus();
  });
});

// --- Phase 2b: cell stack copy/paste (the duplicate-forward primitive) ------
describe("cell stack copy/paste", () => {
  const LINES = [
    { id: 5, line: 1, text: "RPE 8" },
    { id: 6, line: 2, text: "slow eccentric" },
  ];

  function linesGrid() {
    return grid({ days: [day({ rows: [row({ cells: { "1": cell({ lines: LINES }) } })] })] });
  }

  function clipboard(text = "") {
    return { getData: vi.fn(() => text), setData: vi.fn() };
  }

  it("copy with a collapsed selection copies the WHOLE stack, newline-joined", () => {
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    input.setSelectionRange(2, 2);
    const clipboardData = clipboard();
    fireEvent.copy(input, { clipboardData });
    expect(clipboardData.setData).toHaveBeenCalledWith("text/plain", "3 x 5, RPE 8, 100\nRPE 8\nslow eccentric");
  });

  it("copy skips blank (cleared-in-place) sub-lines", () => {
    const CLEARED = grid({
      days: [day({ rows: [row({ cells: { "1": cell({ lines: [{ id: 5, line: 1, text: "" }, { id: 6, line: 2, text: "cue" }] }) } })] })],
    });
    render(<MesoTable {...baseProps({ grid: CLEARED })} />);
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    input.setSelectionRange(0, 0);
    const clipboardData = clipboard();
    fireEvent.copy(input, { clipboardData });
    expect(clipboardData.setData).toHaveBeenCalledWith("text/plain", "3 x 5, RPE 8, 100\ncue");
  });

  it("copy with a REAL text selection stays native (no stack copy)", () => {
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    input.setSelectionRange(0, 3);
    const clipboardData = clipboard();
    fireEvent.copy(input, { clipboardData });
    expect(clipboardData.setData).not.toHaveBeenCalled();
  });

  it("multi-line paste replaces the stack: line 0 via onPatchCell, sub-lines via onWriteCellLine, longer old lines blanked", () => {
    const onPatchCell = vi.fn();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ grid: linesGrid(), onPatchCell, onWriteCellLine })} />);
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    fireEvent.paste(input, { clipboardData: clipboard("4 x 6\nRPE 9") });
    expect(onPatchCell).toHaveBeenCalledWith(100, { text: "4 x 6" });
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 1, "RPE 9");
    // Old line 2 ("slow eccentric") is beyond the pasted stack: blanked.
    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 2, "");
    expect(input).toHaveValue("4 x 6");
  });

  it("trailing blank pasted lines are dropped, not minted", () => {
    const onPatchCell = vi.fn();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell, onWriteCellLine })} />); // default fixture: lines []
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    fireEvent.paste(input, { clipboardData: clipboard("4 x 6\n\n") });
    expect(onPatchCell).toHaveBeenCalledWith(100, { text: "4 x 6" });
    expect(onWriteCellLine).not.toHaveBeenCalled();
  });

  it("Escape after a multi-line paste reverts to the pasted head, never past the commit", () => {
    render(<MesoTable {...baseProps({ grid: linesGrid() })} />);
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    input.focus(); // seeds the Escape baseline with the pre-paste text
    fireEvent.paste(input, { clipboardData: clipboard("4 x 6\nRPE 9") });
    expect(input).toHaveValue("4 x 6");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveValue("4 x 6"); // the committed paste IS the baseline now
  });

  it("single-line paste stays native caret insertion (no stack write)", () => {
    const onPatchCell = vi.fn();
    const onWriteCellLine = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell, onWriteCellLine })} />);
    const input = screen.getByTestId("cell-text-100") as HTMLInputElement;
    fireEvent.paste(input, { clipboardData: clipboard("4 x 6") });
    expect(onPatchCell).not.toHaveBeenCalled();
    expect(onWriteCellLine).not.toHaveBeenCalled();
  });
});

// --- Phase 2a (D2): per-exercise row columns (Tempo / Notes / Rest) --------
// Row attributes off the block-shared ExerciseSlot, matching the source
// spreadsheet layout Exercise | Tempo | weeks… | Notes | Rest — committed
// via onPatchRowColumns (fire-and-forget, like onPatchCell).
describe("row columns (tempo / notes / rest)", () => {
  function colsGrid() {
    return grid({ days: [day({ rows: [row({ tempo: "31X1", rest: "2 min", note: "brace hard" })] })] });
  }

  it("renders the Tempo / Notes / Rest column headers", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByRole("columnheader", { name: "Tempo" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Notes" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Rest" })).toBeInTheDocument();
  });

  it("renders the row's tempo/note/rest values", () => {
    render(<MesoTable {...baseProps({ grid: colsGrid() })} />);
    expect(screen.getByTestId("row-tempo-9")).toHaveValue("31X1");
    expect(screen.getByTestId("row-note-9")).toHaveValue("brace hard");
    expect(screen.getByTestId("row-rest-9")).toHaveValue("2 min");
  });

  it("commits a dirtied tempo on blur via onPatchRowColumns(slotId, {tempo})", async () => {
    const user = userEvent.setup();
    const onPatchRowColumns = vi.fn();
    render(<MesoTable {...baseProps({ onPatchRowColumns })} />);
    await user.type(screen.getByTestId("row-tempo-9"), "20X0");
    await user.tab();
    expect(onPatchRowColumns).toHaveBeenCalledWith(9, { tempo: "20X0" });
  });

  it("commits a dirtied note on Enter via onPatchRowColumns(slotId, {note})", async () => {
    const user = userEvent.setup();
    const onPatchRowColumns = vi.fn();
    render(<MesoTable {...baseProps({ onPatchRowColumns })} />);
    await user.type(screen.getByTestId("row-note-9"), "long hip hinge");
    await user.keyboard("{Enter}");
    expect(onPatchRowColumns).toHaveBeenCalledWith(9, { note: "long hip hinge" });
  });

  it("commits a dirtied rest on blur via onPatchRowColumns(slotId, {rest})", async () => {
    const user = userEvent.setup();
    const onPatchRowColumns = vi.fn();
    render(<MesoTable {...baseProps({ onPatchRowColumns })} />);
    await user.type(screen.getByTestId("row-rest-9"), "3 min");
    await user.tab();
    expect(onPatchRowColumns).toHaveBeenCalledWith(9, { rest: "3 min" });
  });

  it("a clean focus+blur is a no-op (dirty gate)", async () => {
    const user = userEvent.setup();
    const onPatchRowColumns = vi.fn();
    render(<MesoTable {...baseProps({ grid: colsGrid(), onPatchRowColumns })} />);
    await user.click(screen.getByTestId("row-tempo-9"));
    await user.tab();
    expect(onPatchRowColumns).not.toHaveBeenCalled();
  });

  it("Escape reverts the draft without committing", async () => {
    const user = userEvent.setup();
    const onPatchRowColumns = vi.fn();
    render(<MesoTable {...baseProps({ grid: colsGrid(), onPatchRowColumns })} />);
    const input = screen.getByTestId("row-tempo-9");
    await user.clear(input);
    await user.type(input, "9999{Escape}");
    expect(input).toHaveValue("31X1");
    await user.tab();
    expect(onPatchRowColumns).not.toHaveBeenCalled();
  });

  it("row-column inputs carry row-sentinel data-grid-cell keys (INSIDE nav since 2b)", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.getByTestId("row-tempo-9")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, null, "tempo"));
    expect(screen.getByTestId("row-note-9")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, null, "note"));
    expect(screen.getByTestId("row-rest-9")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, null, "rest"));
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

// --- Issue #455 phase A2.5: menu-based cross-day move (row-name column,
// 2nd line, alongside the A3 %1RM badge) --------------------------------
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

describe("weeks: remove (arm -> confirm)", () => {
  it("remove-week arms then confirms, calling onRemoveWeek", async () => {
    const user = userEvent.setup();
    const onRemoveWeek = vi.fn();
    render(
      <MesoTable
        {...baseProps({
          grid: grid({ weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2" })] }),
          onRemoveWeek,
        })}
      />,
    );
    await user.click(screen.getByTestId("remove-week-2"));
    await user.click(screen.getByTestId("confirm-remove-week-2"));
    expect(onRemoveWeek).toHaveBeenCalledWith(2);
  });

  // Programs are date-less and carry no "current week" pointer
  // (docs/meso/remove-current-week-plan.md) — there is no exempt week
  // anymore; every live week's pill offers remove (the backend's own
  // last-live-week guard, not this component, is what stops the final one).
  it("offers remove-week for every week, including the first", () => {
    render(<MesoTable {...baseProps({ grid: grid({ weeks: [week({ id: 1 })] }) })} />);
    expect(screen.getByTestId("remove-week-1")).toBeInTheDocument();
  });

  // designer-simplify: a week spans every day, so its lifecycle controls live
  // ONCE in the mesocycle-level WeekManagerStrip above the day tables — never
  // inside a day. The week LABEL column still appears per day (so the grid
  // stays column-aligned), but it carries no controls.
  it("renders the week remove control once (in the week strip), not in any day-table", () => {
    render(
      <MesoTable
        {...baseProps({
          grid: grid({
            weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2" })],
            days: [day({ session_slot_id: 1, name: "Lower" }), day({ session_slot_id: 2, name: "Upper" })],
          }),
        })}
      />,
    );
    // both day tables render, and each still shows the Wk 2 label column…
    expect(screen.getByTestId("meso-day-table-1")).toBeInTheDocument();
    expect(screen.getByTestId("meso-day-table-2")).toBeInTheDocument();
    expect(screen.getAllByTestId("week-col-2")).toHaveLength(2);
    // …but the control appears exactly once, in the strip's week pill.
    expect(screen.getAllByTestId("remove-week-2")).toHaveLength(1);
    const pill = screen.getByTestId("week-pill-2");
    expect(pill).toContainElement(screen.getByTestId("remove-week-2"));
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

// --- designer-simplify: the per-cell Skip/Fill button cluster (CellActions)
// is GONE — it grew the <td> on :focus-within (confusing layout shift at
// exercises × weeks cardinality). A "skip" is now typed text on a sub-line
// (parse_prescription classifies skip/skipped/-/— — parsing.py); the table
// can no longer CREATE a skipped cell from the UI, only clear one via the
// skipped branch's "Unskip" button (kept as-is). Fill-across-weeks moved to
// a keybinding, Ctrl/Cmd+Enter, on GridCellEditor's wrapping div.
// `id` = prescription_id, `slotId` = session_slot_id, `weekId` = week id.

describe("skip / unskip", () => {
  it("renders no cell-skip-* or cell-fill-* controls on a non-skipped cell (CellActions removed)", () => {
    render(<MesoTable {...baseProps()} />);
    expect(screen.queryByTestId("cell-skip-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cell-fill-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cell-fill-confirm-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cell-fill-cancel-100")).not.toBeInTheDocument();
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

describe("fill across weeks (Ctrl/Cmd+Enter keybinding)", () => {
  it("Ctrl+Enter inside the prescription input calls onFillAcrossWeeks(id)", () => {
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ onFillAcrossWeeks })} />);
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter", ctrlKey: true });
    expect(onFillAcrossWeeks).toHaveBeenCalledWith(100);
  });

  it("Cmd+Enter (metaKey) also calls onFillAcrossWeeks(id)", () => {
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ onFillAcrossWeeks })} />);
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter", metaKey: true });
    expect(onFillAcrossWeeks).toHaveBeenCalledWith(100);
  });

  it("does nothing when busy", () => {
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ busy: true, onFillAcrossWeeks })} />);
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter", ctrlKey: true });
    expect(onFillAcrossWeeks).not.toHaveBeenCalled();
  });

  it("a plain Enter (no modifier) does not fill — that is commit-and-move-down", () => {
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ onFillAcrossWeeks })} />);
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter" });
    expect(onFillAcrossWeeks).not.toHaveBeenCalled();
  });

  it("bubbles from a sub-line/ghost input up to the cell editor's handler", () => {
    const onFillAcrossWeeks = vi.fn();
    const LINES_GRID = grid({ days: [day({ rows: [row({ cells: { "1": cell({ lines: [{ id: 5, line: 1, text: "RPE 8" }] }) } })] })] });
    render(<MesoTable {...baseProps({ grid: LINES_GRID, onFillAcrossWeeks })} />);
    fireEvent.keyDown(screen.getByTestId("cell-line-new-100"), { key: "Enter", ctrlKey: true });
    expect(onFillAcrossWeeks).toHaveBeenCalledWith(100);
  });

  // The retired Fill BUTTON committed the draft implicitly (clicking it blurred
  // the input). A keybinding fires with focus still in the cell, so without an
  // explicit commit the server would copy the stale stored stack to the other
  // weeks and the refetch would clobber the in-progress edit.
  it("commits an uncommitted prescription draft BEFORE dispatching the fill", () => {
    const onPatchCell = vi.fn();
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ onPatchCell, onFillAcrossWeeks })} />);
    const input = screen.getByTestId("cell-text-100");
    input.focus();
    fireEvent.change(input, { target: { value: "5 x 5" } });
    expect(onPatchCell).not.toHaveBeenCalled(); // still an uncommitted draft

    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });

    expect(onPatchCell).toHaveBeenCalledWith(100, { text: "5 x 5" });
    expect(onFillAcrossWeeks).toHaveBeenCalledWith(100);
    expect(onPatchCell.mock.invocationCallOrder[0]!).toBeLessThan(onFillAcrossWeeks.mock.invocationCallOrder[0]!);
    expect(document.activeElement).toBe(input); // anchor restored
  });

  it("commits an uncommitted SUB-LINE draft before dispatching the fill", () => {
    const onWriteCellLine = vi.fn();
    const onFillAcrossWeeks = vi.fn();
    const LINES_GRID = grid({ days: [day({ rows: [row({ cells: { "1": cell({ lines: [{ id: 5, line: 1, text: "RPE 8" }] }) } })] })] });
    render(<MesoTable {...baseProps({ grid: LINES_GRID, onWriteCellLine, onFillAcrossWeeks })} />);
    const line = screen.getByTestId("cell-line-100-1");
    line.focus();
    fireEvent.change(line, { target: { value: "RPE 9" } });
    expect(onWriteCellLine).not.toHaveBeenCalled();

    fireEvent.keyDown(line, { key: "Enter", ctrlKey: true });

    expect(onWriteCellLine).toHaveBeenCalledWith(9, 1, 1, "RPE 9");
    expect(onWriteCellLine.mock.invocationCallOrder[0]!).toBeLessThan(onFillAcrossWeeks.mock.invocationCallOrder[0]!);
  });

  it("modified Ctrl/Cmd+Enter (Shift or Alt) does NOT fill", () => {
    const onFillAcrossWeeks = vi.fn();
    render(<MesoTable {...baseProps({ onFillAcrossWeeks })} />);
    // Kept as one unambiguous chord, so a modified variant never mutates
    // data by accident — the reason Ctrl/Cmd+R was abandoned in the first
    // place (it shadowed the browser's reload and hard-refresh).
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter", ctrlKey: true, shiftKey: true });
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter", metaKey: true, shiftKey: true });
    fireEvent.keyDown(screen.getByTestId("cell-text-100"), { key: "Enter", ctrlKey: true, altKey: true });
    expect(onFillAcrossWeeks).not.toHaveBeenCalled();
  });

  // Fill has no per-cell control anymore, so the keybinding would be
  // undiscoverable without this. ONE hint per page (the week strip), not one
  // per cell — reintroducing per-cell chrome is the thing this work removed.
  it("advertises the keybinding once, in the week strip", () => {
    render(
      <MesoTable
        {...baseProps({ grid: grid({ weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2" })] }) })}
      />,
    );
    const hints = screen.getAllByTestId("week-strip-fill-hint");
    expect(hints).toHaveLength(1);
    expect(hints[0]).toHaveTextContent("Ctrl/⌘+Enter fills a cell across all weeks");
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
            weeks: [week({ id: 1 }), week({ id: 2, label: "Wk 2" })],
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

// --- Issue #455 phase A1: keyboard grid navigation ------------------------
// useTableNav (../hooks/useTableNav) is instantiated ONCE inside MesoTable —
// GridCellEditor/RowNameEditor are module-private, so there's no externally
// injectable gridNav prop and no INERT fallback to test; every spec here
// renders the real table and drives real keyboard events through RTL,
// mirroring WeekGrid.test.tsx's "Phase 3" block (the one-week precedent).
//
// Phase 2a: the per-week editable surface is ONE freeform "text" input.
// Phase 2b widened the axes to the full sheet: horizontally name → tempo →
// week 1 text → week 2 text → notes → rest (Tab-walkable), vertically each
// cell's sub-line stack (existing lines + the trailing ghost) before the
// next row; Enter = commit + move down, appending a row at a day's last
// stop via onAddExercise.
//
// Fixture: day 1 (session_slot_id 1) has row 9 "Box Squat" (cells 900/901)
// and row 10 "RDL" (cells 1000/1001); day 2 (session_slot_id 2) has row 11
// "Bench" (cells 1100/1101). Two weeks (id 1 "Wk 1", id 2 "Wk 2") so
// ArrowRight/Left week-crossing is exercisable directly.
const NAV_GRID: MesoGrid = grid({
  weeks: [week({ id: 1, label: "Wk 1" }), week({ id: 2, label: "Wk 2" })],
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
    it("stamps data-grid-cell on every text input and the row-name input, keyed by (rowId, weekId, field)", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, null, "name"));
      expect(screen.getByTestId("cell-text-900")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 1, "text"));
      expect(screen.getByTestId("cell-text-901")).toHaveAttribute("data-grid-cell", tableCellDomKey(9, 2, "text"));
      expect(screen.getByTestId("cell-text-1100")).toHaveAttribute("data-grid-cell", tableCellDomKey(11, 1, "text"));
    });

    it("gives every text input an aria-label reflecting row/week, and the name input its own", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      expect(screen.getByTestId("cell-text-900")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", "Wk 1", "text"));
      expect(screen.getByTestId("cell-text-901")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", "Wk 2", "text"));
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("aria-label", tableCellAriaLabel("Box Squat", null, "name"));
    });
  });

  describe("roving tabindex", () => {
    it("exactly one cell (the table's first: row-name) is tabbable on initial render", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("tabindex", "0");
      expect(screen.getByTestId("cell-text-900")).toHaveAttribute("tabindex", "-1");
      expect(screen.getByTestId("row-name-10")).toHaveAttribute("tabindex", "-1");
      expect(screen.getByTestId("row-name-11")).toHaveAttribute("tabindex", "-1");
    });

    it("focusing another cell moves the roving tabIndex to it", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-text-1000"));
      expect(screen.getByTestId("cell-text-1000")).toHaveAttribute("tabindex", "0");
      expect(screen.getByTestId("row-name-9")).toHaveAttribute("tabindex", "-1");
    });

    it("roving tabIndex spans multiple day-tables as ONE grid", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-text-1000")); // day 1's last row
      await user.keyboard("{ArrowDown}{ArrowDown}"); // through the ghost, into day 2
      expect(screen.getByTestId("cell-text-1100")).toHaveFocus(); // day 2's row
      expect(screen.getByTestId("cell-text-1100")).toHaveAttribute("tabindex", "0");
    });
  });

  describe("ArrowDown / ArrowUp navigate the stack, then rows, across day-table boundaries", () => {
    it("ArrowDown from a prescription steps into its ghost, then the next row's cell", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-text-900"));
      await user.keyboard("{ArrowDown}");
      expect(screen.getByTestId("cell-line-new-900")).toHaveFocus(); // the ghost is a real stop (D3)
      await user.keyboard("{ArrowDown}");
      expect(screen.getByTestId("cell-text-1000")).toHaveFocus();
    });

    it("ArrowDown crosses a day-table boundary (last stop of day 1 -> first row of day 2)", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-text-1001"));
      await user.keyboard("{ArrowDown}{ArrowDown}");
      expect(screen.getByTestId("cell-text-1101")).toHaveFocus();
    });

    it("ArrowUp mirrors ArrowDown, crossing day boundaries upward onto the previous row's last stop", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("cell-text-1100"));
      await user.keyboard("{ArrowUp}");
      expect(screen.getByTestId("cell-line-new-1000")).toHaveFocus(); // row 10's ghost
      await user.keyboard("{ArrowUp}");
      expect(screen.getByTestId("cell-text-1000")).toHaveFocus();
    });
  });

  describe("ArrowRight / ArrowLeft move cells only at caret extremes, crossing weeks and the name column", () => {
    it("ArrowRight at the end of the text moves to the next week's cell", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const textInput = screen.getByTestId("cell-text-900") as HTMLInputElement;
      textInput.focus();
      textInput.setSelectionRange(textInput.value.length, textInput.value.length);
      fireEvent.keyDown(textInput, { key: "ArrowRight" });
      expect(screen.getByTestId("cell-text-901")).toHaveFocus();
    });

    it("ArrowLeft at the start of the text moves back to the previous week's cell", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const textInput = screen.getByTestId("cell-text-901") as HTMLInputElement;
      textInput.focus();
      textInput.setSelectionRange(0, 0);
      fireEvent.keyDown(textInput, { key: "ArrowLeft" });
      expect(screen.getByTestId("cell-text-900")).toHaveFocus();
    });

    it("ArrowRight at the end of the name column moves into the TEMPO column (the sheet's order)", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const nameInput = screen.getByTestId("row-name-9") as HTMLInputElement;
      nameInput.focus();
      nameInput.setSelectionRange(nameInput.value.length, nameInput.value.length);
      fireEvent.keyDown(nameInput, { key: "ArrowRight" });
      expect(screen.getByTestId("row-tempo-9")).toHaveFocus();
    });

    it("ArrowRight at the end of the tempo column moves into week 1's text cell", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const tempoInput = screen.getByTestId("row-tempo-9") as HTMLInputElement;
      tempoInput.focus();
      tempoInput.setSelectionRange(tempoInput.value.length, tempoInput.value.length);
      fireEvent.keyDown(tempoInput, { key: "ArrowRight" });
      expect(screen.getByTestId("cell-text-900")).toHaveFocus();
    });

    it("ArrowRight at the end of the LAST week's cell moves into the notes column", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const textInput = screen.getByTestId("cell-text-901") as HTMLInputElement;
      textInput.focus();
      textInput.setSelectionRange(textInput.value.length, textInput.value.length);
      fireEvent.keyDown(textInput, { key: "ArrowRight" });
      expect(screen.getByTestId("row-note-9")).toHaveFocus();
    });

    it("ArrowLeft at the start of week 1's text cell moves back to the tempo column", () => {
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const textInput = screen.getByTestId("cell-text-900") as HTMLInputElement;
      textInput.focus();
      textInput.setSelectionRange(0, 0);
      fireEvent.keyDown(textInput, { key: "ArrowLeft" });
      expect(screen.getByTestId("row-tempo-9")).toHaveFocus();
    });
  });

  describe("Tab walks the row unconditionally, wrapping between rows (Phase 2b)", () => {
    it("Tab moves name -> tempo even with the caret mid-text", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const nameInput = screen.getByTestId("row-name-9") as HTMLInputElement;
      await user.click(nameInput);
      nameInput.setSelectionRange(1, 1);
      await user.tab();
      expect(screen.getByTestId("row-tempo-9")).toHaveFocus();
    });

    it("Tab at the rest column wraps to the next row's name", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("row-rest-9"));
      await user.tab();
      expect(screen.getByTestId("row-name-10")).toHaveFocus();
    });

    it("Shift+Tab at the name column wraps back to the previous row's rest", async () => {
      const user = userEvent.setup();
      render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      await user.click(screen.getByTestId("row-name-10"));
      await user.tab({ shift: true });
      expect(screen.getByTestId("row-rest-9")).toHaveFocus();
    });
  });

  describe("Enter-adds-row (integrated)", () => {
    it("Enter at a day's last stop calls onAddExercise with that day (row has content)", async () => {
      const user = userEvent.setup();
      const onAddExercise = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onAddExercise })} />);
      // Row 10's name is day 1's last vertical stop of the name column.
      await user.click(screen.getByTestId("row-name-10"));
      await user.keyboard("{Enter}");
      expect(onAddExercise).toHaveBeenCalledTimes(1);
      expect(onAddExercise.mock.calls[0]![0].session_slot_id).toBe(1);
    });

    it("Enter mid-day moves down instead of appending", async () => {
      const user = userEvent.setup();
      const onAddExercise = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onAddExercise })} />);
      await user.click(screen.getByTestId("row-name-9"));
      await user.keyboard("{Enter}");
      expect(onAddExercise).not.toHaveBeenCalled();
      expect(screen.getByTestId("row-name-10")).toHaveFocus();
    });

    it("Enter at the last stop of a fully blank row does NOT append (the guard), and busy blocks the verb", async () => {
      const user = userEvent.setup();
      const onAddExercise = vi.fn();
      const BLANK_GRID = grid({
        weeks: NAV_GRID.weeks,
        days: [
          day({
            session_slot_id: 1,
            rows: [
              row({
                exercise_slot_id: 9,
                name: "",
                cells: { "1": cell({ prescription_id: 900, text: "" }), "2": cell({ prescription_id: 901, text: "" }) },
              }),
            ],
          }),
        ],
      });
      render(<MesoTable {...baseProps({ grid: BLANK_GRID, onAddExercise })} />);
      await user.click(screen.getByTestId("row-name-9"));
      await user.keyboard("{Enter}");
      expect(onAddExercise).not.toHaveBeenCalled();
    });
  });

  describe("Enter commits / Escape reverts (integrated)", () => {
    it("Enter commits only when the cell is dirty, and moves down to the next stop (the ghost)", async () => {
      const user = userEvent.setup();
      const onPatchCell = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onPatchCell })} />);
      const textInput = screen.getByTestId("cell-text-900");
      await user.click(textInput);
      await user.keyboard("{Enter}"); // clean cell: no commit (existing dirty gate) — still moves
      expect(onPatchCell).not.toHaveBeenCalled();
      expect(screen.getByTestId("cell-line-new-900")).toHaveFocus();
      await user.clear(textInput); // refocuses the prescription input
      await user.type(textInput, "4 x 6");
      await user.keyboard("{Enter}");
      expect(onPatchCell).toHaveBeenCalledWith(900, { text: "4 x 6" });
      expect(onPatchCell).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId("cell-line-new-900")).toHaveFocus(); // spreadsheet Enter: down one stop
    });

    it("Escape reverts only the focused cell's draft, leaving a different dirty cell untouched", () => {
      // fireEvent.change without a prior real .focus() dirties week 2's cell
      // without giving it real DOM focus, so focusing week 1's next doesn't
      // trigger a real blur-commit of week 2 and contaminate the assertion.
      const onPatchCell = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onPatchCell })} />);
      const w1Input = screen.getByTestId("cell-text-900") as HTMLInputElement;
      const w2Input = screen.getByTestId("cell-text-901") as HTMLInputElement;

      fireEvent.change(w2Input, { target: { value: "5 x 5" } });

      w1Input.focus();
      fireEvent.change(w1Input, { target: { value: "9 x 9" } });
      fireEvent.keyDown(w1Input, { key: "Escape" });

      expect(w1Input).toHaveValue("3 x 5, RPE 8, 100"); // reverted to the original cell value
      expect(w2Input).toHaveValue("5 x 5"); // untouched dirty draft survives

      fireEvent.blur(w2Input); // commits whatever's still dirty on that cell
      expect(onPatchCell).toHaveBeenCalledWith(901, { text: "5 x 5" });
      expect(onPatchCell).not.toHaveBeenCalledWith(900, expect.anything());
    });

    it("Escape keeps focus on the cell and suppresses its next blur-commit", () => {
      const onPatchCell = vi.fn();
      render(<MesoTable {...baseProps({ grid: NAV_GRID, onPatchCell })} />);
      const textInput = screen.getByTestId("cell-text-900") as HTMLInputElement;
      textInput.focus();
      fireEvent.change(textInput, { target: { value: "9 x 9" } });
      fireEvent.keyDown(textInput, { key: "Escape" });
      expect(textInput).toHaveValue("3 x 5, RPE 8, 100");
      expect(textInput).toHaveFocus();
      fireEvent.blur(textInput);
      expect(onPatchCell).not.toHaveBeenCalled();
    });
  });

  describe("data-grid-restore integration", () => {
    it("refocuses the replacement cell after a fresh grid identity from a data-grid-restore control (+ Add week)", () => {
      const { rerender } = render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const textInput = screen.getByTestId("cell-text-900") as HTMLInputElement;
      textInput.focus();

      // Simulates the coach clicking a data-grid-restore control (+ Add week;
      // Undo/Redo now live in the top bar but carry the same marker) — real DOM
      // focus moves to the button before the grid re-renders, and the marker
      // lets useTableNav refocus the grid's replacement cell.
      const restoreControl = screen.getByTestId("add-week") as HTMLButtonElement;
      restoreControl.focus();

      const NEXT_GRID = grid({ weeks: NAV_GRID.weeks, days: NAV_GRID.days });
      rerender(<MesoTable {...baseProps({ grid: NEXT_GRID })} />);

      expect(screen.getByTestId("cell-text-900")).toHaveFocus();
    });

    it("focusing an unmarked control (the row drag handle) across a grid identity change does NOT steal focus", () => {
      const { rerender } = render(<MesoTable {...baseProps({ grid: NAV_GRID })} />);
      const textInput = screen.getByTestId("cell-text-900") as HTMLInputElement;
      textInput.focus();

      const dragHandle = screen.getByTestId("row-drag-9") as HTMLButtonElement;
      dragHandle.focus(); // the drag handle is intentionally NOT data-grid-restore

      const NEXT_GRID = grid({ weeks: NAV_GRID.weeks, days: NAV_GRID.days });
      rerender(<MesoTable {...baseProps({ grid: NEXT_GRID })} />);

      expect(dragHandle).toHaveFocus();
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
  it("ArrowRight from the prior week's cell skips an entire skipped-week cell and lands on the following week's text input", () => {
    const SKIP_GRID = grid({
      weeks: [
        week({ id: 1, label: "Wk 1" }),
        week({ id: 2, label: "Wk 2" }),
        week({ id: 3, label: "Wk 3" }),
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
    expect(screen.queryByTestId("cell-text-901")).not.toBeInTheDocument();

    const textInput = screen.getByTestId("cell-text-900") as HTMLInputElement;
    textInput.focus();
    textInput.setSelectionRange(textInput.value.length, textInput.value.length);
    fireEvent.keyDown(textInput, { key: "ArrowRight" });

    expect(screen.getByTestId("cell-text-902")).toHaveFocus();
    expect(screen.getByTestId("cell-text-902")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("cell-text-900")).toHaveAttribute("tabindex", "-1");
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

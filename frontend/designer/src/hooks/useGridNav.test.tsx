// Specs for useGridNav (Phase 3, docs/meso/designer-framework-plan.md +
// scratchpad phase3-spec.md) — the grid's roving-tabindex + keyboard-nav +
// focus-restoration hook. Does NOT exist yet; this file is RED until a
// later agent implements `../hooks/useGridNav`.
//
// === API decisions this file pins (spec left these open) ===
// 1. Hook lives at frontend/designer/src/hooks/useGridNav.ts, called ONCE
//    inside WeekGrid (has `program` already; DesignerRoot needs no change).
// 2. Cell DOM identity: every grid `<input>` carries
//    `data-grid-cell="{prescriptionId}:{column}"` (see `gridCellDomKey`
//    below) so the hook can locate + `.focus()` siblings via
//    `document.querySelector` instead of ref-plumbing through
//    DayCard/ExerciseRow. `cellProps()` itself returns only
//    `{ tabIndex, onFocus, onKeyDown }` — NOT the data-attribute or the
//    aria-label; those are the caller's job (ExerciseRow sets
//    `data-grid-cell={gridCellDomKey(...)}` and
//    `aria-label={cellAriaLabel(...)}` directly) so aria-labels stay a
//    pure, hook-independent a11y guarantee (present even if a row is
//    somehow rendered without a wired-up gridNav).
// 3. `cellProps(prescriptionId, column, callbacks)` — `callbacks` is
//    `{ onChange(value), onCommit(), onRevert(value) }`, supplied fresh
//    per render by ExerciseRow: `onCommit` IS the row's existing
//    dirty-gated `commitIfDirty` (so Enter's "existing dirty-gated
//    commit" requirement is satisfied by construction, no duplicate dirty
//    tracking inside the hook); `onRevert` is a NEW ExerciseRow-side
//    function that calls the raw (non-dirtying) `onFieldChange` prop and
//    clears the row's `dirtySinceFocus` ref — this is how Escape avoids
//    re-arming a commit-on-blur after reverting.
// 4. Focus-time value capture (needed for Escape) lives INSIDE the hook:
//    `onFocus` records `event.currentTarget.value` keyed by cell id, and
//    ALSO updates `anchor` (roving tabindex follows real focus, mouse or
//    keyboard). Arrow-key moves set `anchor` directly (not via a focus
//    event round-trip) so the hook works identically whether or not
//    React's synthetic event system is attached (true in these
//    hook-only specs, which build fake event objects and never mount
//    real DOM via React).
// 5. ArrowLeft/Right at a ROW extreme (no column to wrap to): the spec
//    only says "no wrap"; resolved here as "well, no preventDefault
//    either" — there's no browser default to suppress at a boundary
//    caret position, so the native no-op is harmless.
// 6. Restoration tiers apply on every `program` reference change
//    (rerender), gated on "the grid had focus before this change" (a
//    ref flipped true by any cell's onFocus) — the spec's documented
//    simpler alternative to a call-site "pending restore" flag. Tier-1
//    (same prescriptionId+column) calling `.focus()` on an
//    already-focused node is a no-op in real browsers, so this cannot
//    disturb an in-progress keystroke during ordinary per-field edits.
// 7. `cellAriaLabel(exerciseName, column)` is exported as a pure
//    function (no hook state needed) — testable standalone, and usable
//    unconditionally by ExerciseRow even before/without a live gridNav.

import { act, renderHook } from "@testing-library/react";
import type { FocusEvent, KeyboardEvent } from "react";
import {
  useGridNav,
  cellAriaLabel,
  gridCellDomKey,
  GRID_COLUMNS,
} from "./useGridNav";
import type { Day, Exercise } from "../lib/api";

function ex(id: number, overrides: Partial<Exercise> = {}): Exercise {
  return { id, name: `Ex ${id}`, sets: "3", reps: "5", load: "100", load_type: "abs", rpe: "8", note: "", ...overrides };
}

function day(id: number, exercises: Exercise[], overrides: Partial<Day> = {}): Day {
  return { id, n: id, name: `Day ${id}`, exercises, ...overrides };
}

// day 1 (id 1): ex 9, ex 10 — day 2 (id 2): ex 11. Flattened row order
// (per spec, day-major/exercise-minor): (9), (10), (11).
const PROGRAM: Day[] = [day(1, [ex(9), ex(10)]), day(2, [ex(11)])];

const NOOP_CALLBACKS = { onChange: vi.fn(), onCommit: vi.fn(), onRevert: vi.fn() };

/** Mounts one real `<input>` per (prescriptionId, column) in `program`, so
 * the hook's `document.querySelector`-based focus moves have somewhere
 * real to land — mirrors what ExerciseRow would render, without needing a
 * full React render of the grid tree. */
function mountCells(program: Day[]) {
  const nodes: Record<string, HTMLInputElement> = {};
  for (const d of program) {
    for (const e of d.exercises) {
      for (const column of GRID_COLUMNS) {
        const input = document.createElement("input");
        input.type = "text";
        input.value = String((e as unknown as Record<string, unknown>)[column] ?? "");
        input.setAttribute("data-grid-cell", gridCellDomKey(e.id, column));
        document.body.appendChild(input);
        nodes[gridCellDomKey(e.id, column)] = input;
      }
    }
  }
  return nodes;
}

/** Simulates the DOM swap a real applyPlanData-driven re-render performs:
 * tear down every existing grid cell and mount fresh ones for the new
 * program (see decision 6 — the hook's own effect runs against whatever
 * `document.querySelector` finds after this). */
function resyncCells(program: Day[]) {
  document.querySelectorAll("[data-grid-cell]").forEach((n) => n.remove());
  return mountCells(program);
}

function keyEvent(
  key: string,
  target: HTMLInputElement,
  extra: Partial<{ ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }> = {},
): KeyboardEvent<HTMLInputElement> {
  return {
    key,
    currentTarget: target,
    target,
    preventDefault: vi.fn(),
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    ...extra,
  } as unknown as KeyboardEvent<HTMLInputElement>;
}

function focusEvent(target: HTMLInputElement): FocusEvent<HTMLInputElement> {
  return { currentTarget: target, target } as unknown as FocusEvent<HTMLInputElement>;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("anchor + roving tabindex", () => {
  it("starts anchored on the first cell (first exercise, name column)", () => {
    mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    expect(result.current.anchor).toEqual({ prescriptionId: 9, column: "name" });
  });

  it("exactly one cell is tabbable (0) initially — every other cell is -1", () => {
    mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    expect(result.current.cellProps(9, "name", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, "sets", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(10, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
    expect(result.current.cellProps(11, "note", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("focusing another cell moves the roving 0 to it", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    act(() => {
      result.current.cellProps(10, "load", NOOP_CALLBACKS).onFocus(focusEvent(cells["10:load"]!));
    });
    expect(result.current.anchor).toEqual({ prescriptionId: 10, column: "load" });
    expect(result.current.cellProps(10, "load", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(9, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });

  it("cell identity is (prescriptionId, column), never an index — a same-column cell on a different exercise is a different cell", () => {
    mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    // Two different exercises' "name" cells must be distinguishable even
    // though both are "column 0 of their row".
    expect(result.current.cellProps(9, "name", NOOP_CALLBACKS).tabIndex).toBe(0);
    expect(result.current.cellProps(10, "name", NOOP_CALLBACKS).tabIndex).toBe(-1);
  });
});

describe("ArrowDown / ArrowUp", () => {
  it("ArrowDown moves focus to the same column on the next exercise row within a day", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowDown", cells["9:sets"]!);
    act(() => {
      result.current.cellProps(9, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells["10:sets"]);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ prescriptionId: 10, column: "sets" });
  });

  it("ArrowDown crosses a day-card boundary (last row of day N -> first row of day N+1)", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowDown", cells["10:load"]!);
    act(() => {
      result.current.cellProps(10, "load", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells["11:load"]);
  });

  it("ArrowDown at the very last row is a no-op but still preventDefault", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowDown", cells["11:note"]!);
    act(() => {
      result.current.cellProps(11, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).toHaveBeenCalled();
    expect(result.current.anchor).toEqual({ prescriptionId: 11, column: "note" });
  });

  it("ArrowUp mirrors ArrowDown, crossing day boundaries upward", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowUp", cells["11:rpe"]!);
    act(() => {
      result.current.cellProps(11, "rpe", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells["10:rpe"]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("ArrowUp at the very first row is a no-op but still preventDefault", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowUp", cells["9:name"]!);
    act(() => {
      result.current.cellProps(9, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).toHaveBeenCalled();
  });
});

describe("ArrowRight / ArrowLeft (caret-conditional)", () => {
  it("ArrowRight at the end of the text moves to the next column and preventDefaults", () => {
    const cells = mountCells(PROGRAM);
    const setsInput = cells["9:sets"]!;
    setsInput.value = "42";
    setsInput.setSelectionRange(2, 2); // caret at end (value.length === 2)
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowRight", setsInput);
    act(() => {
      result.current.cellProps(9, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells["9:reps"]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("ArrowLeft at the start of the text moves to the previous column and preventDefaults", () => {
    const cells = mountCells(PROGRAM);
    const repsInput = cells["9:reps"]!;
    repsInput.value = "5";
    repsInput.setSelectionRange(0, 0); // caret at start
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowLeft", repsInput);
    act(() => {
      result.current.cellProps(9, "reps", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(cells["9:sets"]);
    expect(event.preventDefault).toHaveBeenCalled();
  });

  it("ArrowRight mid-text does NOT move focus or preventDefault (native caret move wins)", () => {
    const cells = mountCells(PROGRAM);
    const setsInput = cells["9:sets"]!;
    setsInput.value = "4200";
    setsInput.setSelectionRange(2, 2); // caret in the middle
    setsInput.focus(); // must actually hold focus for "stays put" to mean anything
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowRight", setsInput);
    act(() => {
      result.current.cellProps(9, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(setsInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("ArrowLeft with a non-collapsed selection does NOT move focus (selectionStart !== selectionEnd)", () => {
    const cells = mountCells(PROGRAM);
    const repsInput = cells["9:reps"]!;
    repsInput.value = "5";
    repsInput.setSelectionRange(0, 1); // a selection, not a collapsed caret
    repsInput.focus();
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowLeft", repsInput);
    act(() => {
      result.current.cellProps(9, "reps", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(repsInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("no wrap at a row extreme: ArrowLeft at the start of the first column (name) is a pure no-op", () => {
    const cells = mountCells(PROGRAM);
    const nameInput = cells["9:name"]!;
    nameInput.setSelectionRange(0, 0);
    nameInput.focus();
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowLeft", nameInput);
    act(() => {
      result.current.cellProps(9, "name", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(nameInput);
    // Decision 5: nothing to prevent at a boundary caret, so no preventDefault.
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("no wrap at a row extreme: ArrowRight at the end of the last column (note) is a pure no-op", () => {
    const cells = mountCells(PROGRAM);
    const noteInput = cells["9:note"]!;
    noteInput.value = "";
    noteInput.setSelectionRange(0, 0); // empty value: start === end === length === 0
    noteInput.focus();
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent("ArrowRight", noteInput);
    act(() => {
      result.current.cellProps(9, "note", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(document.activeElement).toBe(noteInput);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});

describe("Enter (commit) / Escape (revert)", () => {
  it("Enter calls the cell's onCommit, preventDefaults, and does not move focus", () => {
    const cells = mountCells(PROGRAM);
    cells["9:load"]!.focus(); // must actually hold focus for "does not move" to mean anything
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const onCommit = vi.fn();
    const event = keyEvent("Enter", cells["9:load"]!);
    act(() => {
      result.current.cellProps(9, "load", { ...NOOP_CALLBACKS, onCommit }).onKeyDown(event);
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(cells["9:load"]);
  });

  it("Escape reverts to the focus-time value via onRevert, preventDefaults, and keeps focus", () => {
    const cells = mountCells(PROGRAM);
    const loadInput = cells["9:load"]!;
    loadInput.value = "100";
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const onRevert = vi.fn();
    const callbacks = { ...NOOP_CALLBACKS, onRevert };
    // Focus first (captures the focus-time value: "100")...
    loadInput.focus();
    act(() => {
      result.current.cellProps(9, "load", callbacks).onFocus(focusEvent(loadInput));
    });
    // ...then the coach types a draft the row hasn't committed yet.
    loadInput.value = "999";
    const event = keyEvent("Escape", loadInput);
    act(() => {
      result.current.cellProps(9, "load", callbacks).onKeyDown(event);
    });
    expect(onRevert).toHaveBeenCalledWith("100");
    expect(event.preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(loadInput);
  });

  it("Escape without a prior focus call reverts to the DOM value at keydown time (no captured value yet)", () => {
    // Open API question the spec doesn't resolve: what's the "focus-time
    // value" if onKeyDown fires before this cell's own onFocus ever ran
    // (e.g. a synthetic/test harness quirk)? Decision: fall back to the
    // current DOM value, i.e. a same-value no-op revert — never throw.
    const cells = mountCells(PROGRAM);
    const rpeInput = cells["9:rpe"]!;
    rpeInput.value = "8";
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const onRevert = vi.fn();
    const event = keyEvent("Escape", rpeInput);
    act(() => {
      result.current.cellProps(9, "rpe", { ...NOOP_CALLBACKS, onRevert }).onKeyDown(event);
    });
    expect(onRevert).toHaveBeenCalledWith("8");
  });
});

describe("regression: undo/redo keys are left alone", () => {
  it("Ctrl+Z / Cmd+Z / Shift+Ctrl+Z on a cell are NOT intercepted (no preventDefault, no callback) — bubbles to the window-level undo listener", () => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { ctrlKey: true, shiftKey: true }]) {
      const event = keyEvent("z", cells["9:name"]!, extra);
      act(() => {
        result.current.cellProps(9, "name", NOOP_CALLBACKS).onKeyDown(event);
      });
      expect(event.preventDefault).not.toHaveBeenCalled();
    }
    expect(NOOP_CALLBACKS.onCommit).not.toHaveBeenCalled();
    expect(NOOP_CALLBACKS.onRevert).not.toHaveBeenCalled();
  });
});

describe("native keys pass through untouched", () => {
  it.each(["Home", "End", "PageUp", "PageDown", "a", "Tab"])("%s is not preventDefault'd", (key) => {
    const cells = mountCells(PROGRAM);
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const event = keyEvent(key, cells["9:sets"]!);
    act(() => {
      result.current.cellProps(9, "sets", NOOP_CALLBACKS).onKeyDown(event);
    });
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});

describe("cellAriaLabel (pure helper, decision 7)", () => {
  it("builds '<name> — <column label>' for a non-name column", () => {
    expect(cellAriaLabel("Box Squat", "sets")).toBe("Box Squat — sets");
    expect(cellAriaLabel("Box Squat", "reps")).toBe("Box Squat — reps");
    expect(cellAriaLabel("Box Squat", "load")).toBe("Box Squat — load");
    expect(cellAriaLabel("Box Squat", "rpe")).toBe("Box Squat — RPE");
    expect(cellAriaLabel("Box Squat", "note")).toBe("Box Squat — note");
  });

  it("labels the name cell as '<name> — exercise name'", () => {
    expect(cellAriaLabel("Box Squat", "name")).toBe("Box Squat — exercise name");
  });

  it("falls back to 'exercise' when the row has no name yet", () => {
    expect(cellAriaLabel("", "sets")).toBe("exercise — sets");
  });
});

describe("gridCellDomKey (pure helper, decision 2)", () => {
  it("joins prescriptionId and column with a colon", () => {
    expect(gridCellDomKey(9, "sets")).toBe("9:sets");
    expect(gridCellDomKey("abc", "name")).toBe("abc:name");
  });
});

describe("focus restoration across a program swap (applyPlanData), decision 6", () => {
  function focus(result: ReturnType<typeof useGridNav>, cells: Record<string, HTMLInputElement>, id: number, column: "name" | "sets") {
    act(() => {
      result.cellProps(id, column, NOOP_CALLBACKS).onFocus(focusEvent(cells[gridCellDomKey(id, column)]!));
    });
  }

  it("tier 1: re-focuses the same (prescriptionId, column) when it survives the swap", () => {
    let cells = mountCells(PROGRAM);
    const { result, rerender } = renderHook(({ program }) => useGridNav({ program }), {
      initialProps: { program: PROGRAM },
    });
    focus(result.current, cells, 9, "sets");

    const NEXT: Day[] = [day(1, [ex(9, { load: "105" }), ex(10)]), day(2, [ex(11)])];
    cells = resyncCells(NEXT);
    act(() => rerender({ program: NEXT }));

    expect(document.activeElement).toBe(cells["9:sets"]);
    expect(result.current.anchor).toEqual({ prescriptionId: 9, column: "sets" });
  });

  it("tier 2: falls back to the first cell of the same day when the prescription is gone but the day survives", () => {
    let cells = mountCells(PROGRAM);
    const { result, rerender } = renderHook(({ program }) => useGridNav({ program }), {
      initialProps: { program: PROGRAM },
    });
    focus(result.current, cells, 9, "sets");

    // Day 1 survives (id 1) but exercise 9 is gone; exercise 10 remains.
    const NEXT: Day[] = [day(1, [ex(10)]), day(2, [ex(11)])];
    cells = resyncCells(NEXT);
    act(() => rerender({ program: NEXT }));

    expect(document.activeElement).toBe(cells["10:name"]);
    expect(result.current.anchor).toEqual({ prescriptionId: 10, column: "name" });
  });

  it("tier 3: falls back to the grid's first cell when the day itself is gone", () => {
    let cells = mountCells(PROGRAM);
    const { result, rerender } = renderHook(({ program }) => useGridNav({ program }), {
      initialProps: { program: PROGRAM },
    });
    focus(result.current, cells, 11, "sets"); // day 2's exercise

    // Day 2 (id 2) is gone entirely; only day 1 remains.
    const NEXT: Day[] = [day(1, [ex(9), ex(10)])];
    cells = resyncCells(NEXT);
    act(() => rerender({ program: NEXT }));

    expect(document.activeElement).toBe(cells["9:name"]);
    expect(result.current.anchor).toEqual({ prescriptionId: 9, column: "name" });
  });

  it("tier 4: does nothing (no throw, no focus) when the whole grid is emptied", () => {
    const cells = mountCells(PROGRAM);
    const { result, rerender } = renderHook(({ program }) => useGridNav({ program }), {
      initialProps: { program: PROGRAM },
    });
    focus(result.current, cells, 9, "name");

    resyncCells([]);
    expect(() => act(() => rerender({ program: [] }))).not.toThrow();

    expect(result.current.anchor).toBe(null);
  });

  it("does NOT steal focus on a program swap when the grid was never focused", () => {
    let cells = mountCells(PROGRAM);
    const { rerender } = renderHook(({ program }) => useGridNav({ program }), {
      initialProps: { program: PROGRAM },
    });
    // No onFocus call at all — the coach has been typing in the chat panel,
    // not the grid, when e.g. another tab's undo swaps the program.
    const NEXT: Day[] = [day(1, [ex(9), ex(10)]), day(2, [ex(11)])];
    cells = resyncCells(NEXT);
    act(() => rerender({ program: NEXT }));

    expect(document.activeElement).toBe(document.body);
    expect(cells["9:name"]).not.toBe(document.activeElement);
  });
});

describe("review hardening: Escape baseline + focus-steal guard", () => {
  it("Enter resets the Escape baseline to the committed value", () => {
    // Without the reset, an Enter-commit followed by a fresh draft and Escape
    // would roll the UI back PAST the committed value, desyncing it from the
    // server (which kept the Enter-committed state).
    const cells = mountCells(PROGRAM);
    const loadInput = cells["9:load"]!;
    loadInput.value = "100";
    const { result } = renderHook(() => useGridNav({ program: PROGRAM }));
    const onCommit = vi.fn();
    const onRevert = vi.fn();
    const callbacks = { ...NOOP_CALLBACKS, onCommit, onRevert };
    loadInput.focus();
    act(() => {
      result.current.cellProps(9, "load", callbacks).onFocus(focusEvent(loadInput));
    });
    loadInput.value = "150";
    act(() => {
      result.current.cellProps(9, "load", callbacks).onKeyDown(keyEvent("Enter", loadInput));
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    loadInput.value = "175";
    act(() => {
      result.current.cellProps(9, "load", callbacks).onKeyDown(keyEvent("Escape", loadInput));
    });
    expect(onRevert).toHaveBeenCalledWith("150");
  });

  it("restoration never steals focus from a non-grid form field", () => {
    // A program swap landing while the coach types in e.g. the chat composer
    // must not yank focus back into the grid — restoration only fires when a
    // grid cell held focus (or the swap just orphaned it).
    let cells = mountCells(PROGRAM);
    const composer = document.createElement("input");
    document.body.appendChild(composer);
    const { result, rerender } = renderHook(({ program }) => useGridNav({ program }), {
      initialProps: { program: PROGRAM },
    });
    act(() => {
      result.current.cellProps(9, "sets", NOOP_CALLBACKS).onFocus(focusEvent(cells[gridCellDomKey(9, "sets")]!));
    });
    composer.focus();

    const NEXT: Day[] = [day(1, [ex(10)]), day(2, [ex(11)])];
    cells = resyncCells(NEXT);
    act(() => rerender({ program: NEXT }));

    expect(document.activeElement).toBe(composer);
  });
});

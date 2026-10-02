// MesoTable (P1 multi-week table) — one <table> per training day, exercise
// rows down the side, WEEK COLUMNS across the top. THE coach's editing
// surface — issue #455 phase A5 deleted the one-week-at-a-time view
// (WeekStrip/WeekGrid/DayCard/ExerciseRow) and every hook that only existed
// to feed it (usePlanData/useAutosave/useDeletes/useUndoRedo/useReorder/
// useOneRmEditor/useGridNav); this file's comments below still reference
// those retired files by name as historical "ported from" / "mirrors"
// context for where a given pattern originated, not because they still
// exist in the tree.
//
// Per-cell edits commit on blur/Enter, carrying forward ExerciseRow's
// dirtySinceFocus pattern (CONTRACT.md "ExerciseRow"). Phase 2a (spreadsheet
// parity) collapsed the cell's six structured inputs (sets/reps/load+
// load_type/rpe/rest/note) to ONE freeform text input (`cell.text`, via
// useGrid.patchCell) plus one input per sub-line of the cell's stack
// (`cell.lines`, upserted by (week × line) via onWriteCellLine) and a
// trailing ghost input that mints the next sub-line on its first non-blank
// commit. Tempo/Notes/Rest moved off the cell onto per-ROW columns
// (row.tempo/note/rest, committed via onPatchRowColumns), matching the
// source spreadsheet's Exercise | Tempo | weeks… | Notes | Rest layout.
//
// Keyboard grid navigation (issue #455 A1) is owned by useTableNav
// (../hooks/useTableNav), a sibling of the one-week path's useGridNav —
// instantiated ONCE here, below, and threaded into GridCellEditor/
// RowNameEditor/CellSubLineInput/RowColumnInput as a required prop (they're
// module-private, so there's no INERT-fallback case to support). Phase 2b
// (spreadsheet keyboard flow) widened its axes to the full sheet: sub-lines
// are vertical stops (D3's arrow-down RPE row, ghost included), Tempo/
// Notes/Rest are horizontal columns, Tab walks the row, Enter commits +
// moves down and appends a row at a day's last stop (wired to onAddExercise
// via useTableNav's onAppendRow), and the prescription input carries the
// stack copy/paste handlers (Ctrl-C with no selection copies the whole
// stack; multi-line paste replaces it — the duplicate-forward primitive).
//
// Drag reordering (issue #455 A2) — row + day — is owned by useTableReorder
// (a sibling of DesignerRoot's instantiation, NOT this file): MesoTable only
// wires up dnd-kit's DndContext/sensors/DragOverlay and the two drag
// handles, translating dnd-kit's real DragEndEvent into the pure
// TableDragEndEvent shape and forwarding it to the optional `onDragEnd`
// prop, mirroring WeekGrid.tsx's onDragEnd/handleDragEnd split exactly.
// Cross-day row moves are OUT of scope for this phase (see
// useTableReorder.ts's header) — enforced at the collision-filter layer
// below (filterTableDragCandidates) so a row drag never even collides with
// another day's rows. No live CSS.Transform on a <tr> or a day block (a
// transformed row inside border-collapse + a sticky first column, or a
// transformed block inside .meso-table-scroll's overflow-x:auto, are both
// known cross-browser glitches, unverifiable in jsdom) — a DragOverlay ghost
// plus `.is-dragging` opacity only.
//
// Phase 2a RETIRED two whole control clusters from this file: the per-ROW
// %1RM badge/editor (RowOneRmEditor — a % load is just text now, no typed
// load to resolve) and the one-week swap badge/menu (a substitution is
// sub-line text, written like any other line). skip/unskip, fill-across-
// weeks, add-this-week and move-to-day all stay. (The per-cell group
// adjust badge went with the group subsystem itself.)
import type { RefObject } from "react";
import { createContext, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ClipboardEvent, FocusEvent, KeyboardEvent } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  pointerWithin,
  rectIntersection,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { CollisionDetection, DragEndEvent, DragStartEvent, KeyboardCoordinateGetter } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import type { CellLine, GridCell, GridDay, GridRow, GridWeek, MesoGrid } from "../lib/api";
import { cellUiKey } from "../hooks/useGrid";
import type { CellLineWriteOpts, CellUiState, GridCellPatch, GridRowPatch, Id } from "../hooks/useGrid";
import { useTableNav, tableCellDomKey, tableCellAriaLabel } from "../hooks/useTableNav";
import type { UseTableNavResult } from "../hooks/useTableNav";
import type { TableDragData, TableDragEndEvent } from "../hooks/useTableReorder";
import { EMPTY_SUGGESTIONS, mergeMine, suggestExercises } from "../lib/exerciseSuggest";
import type { ExerciseSuggestions, Suggestion } from "../lib/exerciseSuggest";
import { TABLE_DAY_DRAG_PREFIX, tableDayDragId, tableRowDragId, tableRowDragPrefix } from "../lib/tableDragIds";

// Fixed column widths (px), the single source for every day's <table> so the
// columns align across days (with table-layout: fixed). Exercise is wide
// enough to read a full exercise name; Tempo and Rest are compact. The cols
// AND the table's own width are set from this so the fixed layout is exact.
const COL_WIDTHS = { exercise: 264, tempo: 66, week: 150, notes: 150, rest: 66 };
function tableWidthFor(weekCount: number): number {
  return COL_WIDTHS.exercise + COL_WIDTHS.tempo + weekCount * COL_WIDTHS.week + COL_WIDTHS.notes + COL_WIDTHS.rest;
}

export interface MesoTableProps {
  grid: MesoGrid | null;
  busy: boolean;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  // Phase 2a: upsert one freeform (week × line) sub-line of a row's stack —
  // addressed by slot/week/line, not pk, since the line may not exist yet
  // (useGrid.writeCellLine). Fire-and-forget, like onPatchCell.
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string, opts?: CellLineWriteOpts): void;
  // Phase 2a (D2): the per-exercise Tempo/Notes/Rest row columns
  // (useGrid.patchRowColumns). Fire-and-forget, like onPatchCell.
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string, exerciseId?: string | null): void;
  onRenameDay(sessionSlotId: Id, name: string): void;
  onAddExercise(day: GridDay): void;
  onRemoveExercise(exerciseSlotId: Id): void;
  onAddDay(): void;
  onRemoveDay(day: GridDay): void;
  onAddWeek(): void;
  onRemoveWeek(weekId: Id): void;
  // P2 exceptions: one-week skip + text-stack fill-across-weeks + a
  // this-week-only add — CONTRACT.md "MesoTable.tsx — new props".
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
  // Issue #455 phase A2 (drag reordering): optional, with a no-op fallback
  // in handleDragEnd below — mirrors WeekGrid.tsx's onDragEnd prop, so
  // MesoTable.test.tsx's existing baseProps() (which never sets it) keeps
  // passing untouched.
  onDragEnd?(event: TableDragEndEvent): void;
  // #608: the coach's own exercise names + the shared catalog, for the
  // row-name combobox (DesignerRoot reads `#meso-exercise-suggest`). Optional
  // so existing callers/tests keep working with no suggestions.
  exerciseSuggestions?: ExerciseSuggestions;
  // #709: transient per-cell UI from useGrid (notices, refused drafts, retry
  // marks), keyed by cellUiKey(slot, week), and the verbs that act on it. All
  // optional so callers that don't log sets for an athlete keep working.
  cellUi?: Record<string, CellUiState>;
  onRetryCellLine?(exerciseSlotId: Id, weekId: Id, line: number): void;
  onDismissCellNotice?(exerciseSlotId: Id, weekId: Id): void;
  onDiscardRefusal?(exerciseSlotId: Id, weekId: Id, refusalId: number): void;
  /** The plan is a template: no athlete, so a line can never be a logged set. */
  isTemplate?: boolean;
}

interface CellExtras {
  cellUi: Record<string, CellUiState>;
  /** The athlete's first name, or null when the grid has no athlete name. */
  athleteFirst: string | null;
  /** A coach can log sets here: the plan has an athlete and isn't a template. */
  canLogSets: boolean;
  onRetryCellLine(exerciseSlotId: Id, weekId: Id, line: number): void;
  onDismissCellNotice(exerciseSlotId: Id, weekId: Id): void;
  onDiscardRefusal(exerciseSlotId: Id, weekId: Id, refusalId: number): void;
}

const NOOP = () => {};
const CellExtrasContext = createContext<CellExtras>({
  cellUi: {},
  athleteFirst: null,
  canLogSets: false,
  onRetryCellLine: NOOP,
  onDismissCellNotice: NOOP,
  onDiscardRefusal: NOOP,
});

/** The single arm/confirm slot — mirrors usePlanData's PendingDelete
 * (one thing armed at a time), but kept local to MesoTable since useGrid's
 * remove verbs fire the mutation directly with no confirm step of their own. */
type ArmedKind = "exercise" | "day" | "week";
type Armed = { type: ArmedKind; id: Id } | null;

// Issue #455 phase A2: sortable ids are "day-<sessionSlotId>" and
// "row-<daySlotId>-<exerciseSlotId>" (id-string encoded, mirroring
// WeekGrid.tsx's "day-"/"ex-" prefix convention) — built EXCLUSIVELY via
// tableDayDragId/tableRowDragId (../lib/tableDragIds), the single source of
// truth for this encoding (Codex #455 A2 review finding 1). A day drag
// targets only day containers; a row drag targets only ROW containers of
// its OWN day — cross-day row moves are OUT of scope for A2 (decisions
// 5/7), enforced here at the collision-filter layer (and independently
// again inside useTableReorder's onDragEnd, off TableDragData — never off
// these strings). Unlike WeekGrid's exercise-active filter (which keeps day
// containers too, for the one-week grid's exercise-over-day append path),
// the table has no cross-type drop target at all in A2.
export function filterTableDragCandidates<T extends { id: unknown }>(activeId: unknown, containers: T[]): T[] {
  const activeIdStr = String(activeId);
  if (activeIdStr.startsWith(TABLE_DAY_DRAG_PREFIX)) {
    return containers.filter((c) => String(c.id).startsWith(TABLE_DAY_DRAG_PREFIX));
  }
  const daySlotId = activeIdStr.split("-")[1] ?? "";
  return containers.filter((c) => String(c.id).startsWith(tableRowDragPrefix(daySlotId)));
}

// Same type/scope filtering at the collision layer as
// filterTableDragCandidates above. INTERSECTION-based on purpose, with NO
// closest-center fallback: closestCenter always returns the nearest
// candidate even when the drop lands nowhere near it, which would turn an
// unsupported cross-day drop (candidates are same-day only) into a phantom
// same-day reorder against whichever row happened to be nearest (Codex
// #455 A2 review). Outside every candidate → no collision → `over` stays
// null → the drop no-ops. pointerWithin first (precise for real pointer
// drags), rectIntersection as the fallback (keyboard drags move the overlay
// rect with no pointer coordinates).
export const tableCollisionDetection: CollisionDetection = (args) => {
  const droppableContainers = filterTableDragCandidates(args.active.id, args.droppableContainers);
  const within = pointerWithin({ ...args, droppableContainers });
  if (within.length > 0) return within;
  return rectIntersection({ ...args, droppableContainers });
};

// Ported verbatim-adapted from WeekGrid.tsx's typedKeyboardCoordinates:
// DroppableContainersMap is a real Map subclass — a spread/assign clone
// borrows its prototype WITHOUT Map internal slots, and .get() then throws
// "called on incompatible receiver". Delegate every member to the original
// map (methods bound to it), overriding only getEnabled with the filter.
export const tableKeyboardCoordinates: KeyboardCoordinateGetter = (event, args) => {
  const containers = args.context.droppableContainers;
  const filtered = new Proxy(containers, {
    get(target, prop) {
      if (prop === "getEnabled") {
        return () => filterTableDragCandidates(args.context.active?.id ?? "", target.getEnabled());
      }
      const value = Reflect.get(target, prop, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  return sortableKeyboardCoordinates(event, {
    ...args,
    context: { ...args.context, droppableContainers: filtered },
  });
};

interface CellSubLineInputProps {
  cellId: number;
  lineId?: number;
  rowId: number;
  weekId: number;
  line: number;
  text: string;
  /** Who owns the line: a coach cue, a set the coach logged, or one the
   * athlete entered (read-only here). Defaults to "cue". */
  origin?: "cue" | "coach" | "athlete";
  /** The athlete's first name for "logged by …" labels (null: "your athlete"). */
  athleteFirst?: string | null;
  /** The trailing "next line" input — commits only non-blank (a blank ghost
   * has nothing to create). Its React key is stable, so an external update
   * that moves `line` never remounts it and drops the coach's typing. */
  ghost?: boolean;
  /** The kind chip: "logged" (a coach set line) or "log as set" (a loggable cue). */
  chip?: "logged" | "log-as-set" | null;
  onFlip?(line: number, text: string, kind: "set" | "cue"): void;
  /** This line's last write never reached the server. */
  unsaved?: boolean;
  onRetry?(line: number): void;
  tableNav: UseTableNavResult;
  onWrite(line: number, text: string): void;
}

/** One freeform sub-line of a cell's stack (Phase 2a) — same dirty-tracking
 * commit-on-blur/Enter + Escape-revert shape as GridCellEditor's main text
 * input. Phase 2b put sub-lines INSIDE useTableNav's axes (each line is a
 * vertical stop at (rowId, weekId, "text", line) — including the ghost, so
 * D3's RPE row is literally "arrow down and type"), so Enter/Escape and the
 * arrows all come from cellProps now instead of a local onKeyDown.
 * Blanking an EXISTING line commits "" — the line clears in place (the row
 * stays), mirroring the server's blank-text upsert.
 *
 * #709: the unsaved draft always wins on screen — a `text` change from
 * outside (a server answer, an undo) never overwrites a dirty draft; the
 * coach's commit then goes to the server, which refuses it visibly if the
 * line changed hands. */
function CellSubLineInput({
  cellId,
  lineId,
  rowId,
  weekId,
  line,
  text,
  origin = "cue",
  athleteFirst,
  ghost,
  chip,
  onFlip,
  unsaved,
  onRetry,
  tableNav,
  onWrite,
}: CellSubLineInputProps) {
  const [draft, setDraft] = useState(text);
  const dirtyRef = useRef(false);
  const athlete = origin === "athlete";
  const first = athleteFirst ?? "your athlete";

  useEffect(() => {
    if (dirtyRef.current) return;
    setDraft(text);
  }, [text]);

  // Commit-on-unmount: a response can flip this line to the athlete's (the
  // input unmounts), and an uncommitted draft must not vanish with it. The
  // server then refuses it visibly, so the text survives as a refusal row.
  const latestRef = useRef({ draft, line, ghost, onWrite });
  latestRef.current = { draft, line, ghost, onWrite };
  useEffect(
    () => () => {
      const cur = latestRef.current;
      if (!dirtyRef.current) return;
      dirtyRef.current = false;
      if (cur.ghost && cur.draft.trim() === "") return;
      cur.onWrite(cur.line, cur.draft);
    },
    [],
  );

  function commitIfDirty() {
    if (athlete || !dirtyRef.current) return;
    dirtyRef.current = false;
    if (ghost && draft.trim() === "") return;
    onWrite(line, draft);
    if (ghost) setDraft("");
  }

  const navProps = tableNav.cellProps(
    rowId,
    weekId,
    "text",
    {
      onCommit: commitIfDirty,
      onRevert: (value) => {
        dirtyRef.current = false;
        setDraft(value);
      },
    },
    line,
  );

  const label = ghost
    ? "Add a line"
    : athlete
      ? `Line ${line} — logged by ${first}`
      : `Line ${line}`;

  return (
    <div
      className={`meso-line-row${athlete ? " meso-line-row--athlete" : ""}${chip ? " meso-line-row--chip" : ""}`}
    >
      {athlete ? (
        <span
          className="meso-line-athlete-mark"
          data-testid={lineId != null ? `cell-line-athlete-${lineId}` : undefined}
          title={`Logged by ${first}`}
        >
          logged by {first}
        </span>
      ) : null}
      <input
        className={`meso-cell meso-line-input${ghost ? " meso-line-input--ghost" : ""}`}
        data-testid={ghost ? `cell-line-new-${cellId}` : `cell-line-${cellId}-${line}`}
        data-grid-cell={tableCellDomKey(rowId, weekId, "text", line)}
        aria-label={label}
        placeholder={ghost ? "+ line" : "—"}
        value={draft}
        readOnly={athlete}
        onChange={
          athlete
            ? undefined
            : (e) => {
                dirtyRef.current = true;
                setDraft(e.target.value);
              }
        }
        onBlur={commitIfDirty}
        {...navProps}
      />
      {chip ? (
        <button
          type="button"
          tabIndex={-1}
          className={`meso-line-chip${chip === "logged" ? " meso-line-chip--on" : ""}`}
          data-testid={`cell-line-kind-${cellId}-${line}`}
          aria-pressed={chip === "logged"}
          title={
            chip === "logged"
              ? `Counts as ${athleteFirst ? `${athleteFirst}'s` : "your athlete's"} set — you logged it. Click to make it a cue.`
              : "Log this line as a set for your athlete."
          }
          // Keep the input's focus: a click must not blur-commit before the
          // handler below runs (it commits this line's own draft first).
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => {
            commitIfDirty();
            onFlip?.(line, draft, chip === "logged" ? "cue" : "set");
          }}
        >
          {chip === "logged" ? "logged" : "log as set"}
        </button>
      ) : null}
      {unsaved ? (
        <button
          type="button"
          className="meso-line-retry"
          data-testid={`cell-line-retry-${cellId}-${line}`}
          onClick={() => onRetry?.(line)}
        >
          Not saved — retry
        </button>
      ) : null}
    </div>
  );
}

interface GridCellEditorProps {
  cell: GridCell;
  row: GridRow;
  week: GridWeek;
  busy: boolean;
  tableNav: UseTableNavResult;
  /** This day has a live session in this week (a set can be logged on it). */
  sessionLive: boolean;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string, opts?: CellLineWriteOpts): void;
  onFillAcrossWeeks(cellId: number): void;
}

/** Phase 2a text-first cell: ONE freeform text input for the prescription
 * line (`cell.text`, committed via onPatchCell — the pk path), then one
 * CellSubLineInput per existing sub-line (`cell.lines`, upserted by
 * (week × line) via onWriteCellLine), then a trailing ghost input that mints
 * the NEXT sub-line (max existing line + 1, or 1) on its first non-blank
 * commit. Dirty-tracking commit-on-blur/Enter + Escape-revert carries
 * forward from the retired six-field editor unchanged in kind.
 *
 * Phase 2b adds stack copy/paste on the prescription input (the cell-level
 * duplicate-forward primitive — copy a cell, arrow to another week, paste):
 * Ctrl-C with NOTHING selected copies the whole stack (line 0 + non-blank
 * sub-lines, newline-joined) — a real text selection keeps native copy —
 * and pasting MULTI-LINE text replaces the whole stack (line 0 via
 * onPatchCell, the rest via onWriteCellLine, any longer existing lines
 * blanked so the result equals the source). Single-line paste stays native
 * caret insertion into the draft.
 *
 * designer-simplify retired the per-cell Skip/Fill button cluster
 * (CellActions) — unacceptable chrome at exercises × weeks cardinality, and
 * it grew the <td> on :focus-within, causing layout shift. Fill-across-weeks
 * is now a KEYBINDING, Ctrl/Cmd+Enter, bound on the wrapping
 * `.meso-table-cell-editor` div so it fires from the prescription input
 * (line 0) OR any sub-line/ghost of the same cell — keydown bubbles up from
 * whichever input is focused. It reads as "commit this — everywhere",
 * extending the plain-Enter commit the grid already uses. Sheets/Excel's own
 * fill binding is Ctrl/Cmd+R, but that shadows the browser's reload: a
 * spreadsheet owns its whole page and can afford to steal it, a Django app
 * in an ordinary tab cannot, and in practice it just read as "the page
 * didn't refresh". Discoverability lives in the WeekManagerStrip hint (one
 * per page) rather than per-cell chrome. No arm/confirm step: fill is
 * recorded in plan history via record_plan_action (views.py), so Ctrl+Z
 * undoes an accidental fill same as any other edit.
 * Skip has NO keybinding — the product decision (docs/meso/decisions.md) is
 * that a "skip" is typed text on a sub-line (parse_prescription classifies
 * skip/skipped/-/— — parsing.py), not a control; the table can no longer
 * CREATE a skipped cell, only the skipped branch's "Unskip" button clears
 * one (asymmetric, intentional). */
function GridCellEditor({
  cell,
  row,
  week,
  busy,
  tableNav,
  sessionLive,
  onPatchCell,
  onWriteCellLine,
  onFillAcrossWeeks,
}: GridCellEditorProps) {
  const extras = useContext(CellExtrasContext);
  const ui = extras.cellUi[cellUiKey(row.exercise_slot_id, week.id)];
  const [draft, setDraft] = useState(cell.text);
  const dirtyRef = useRef(false);

  // Resync the draft whenever the source of truth changes — our own commit's
  // optimistic update, or an external refetch (undo/redo, another coach
  // action) — never while the coach is mid-edit, since this only runs when
  // the value actually changes.
  useEffect(() => {
    setDraft(cell.text);
    dirtyRef.current = false;
  }, [cell.text]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onPatchCell(cell.prescription_id, { text: draft });
  }

  function revert(value: string) {
    dirtyRef.current = false;
    setDraft(value);
  }

  const navProps = tableNav.cellProps(row.exercise_slot_id, week.id, "text", {
    onCommit: commitIfDirty,
    onRevert: revert,
  });

  const cellId = cell.prescription_id;
  const lines = cell.lines ?? [];
  const nextLine = lines.reduce((max, l) => Math.max(max, l.line), 0) + 1;

  // #645: athlete-authored lines collapse to ONE roll-up marker (read-only
  // presentation; the lines, their writes and the ghost numbering are
  // untouched). Expanding renders them as the editable athlete rows.
  // #709: athlete-ENTERED lines (athlete_authored && !entered_by_coach) are
  // the roll-up group, read-only when expanded; coach set lines render inline
  // beside the cues, in line order, each with its own "logged" chip.
  const athleteLines = lines.filter((l) => l.athlete_authored && !l.entered_by_coach && l.text.trim() !== "");
  const inlineLines = lines.filter((l) => !l.athlete_authored || l.entered_by_coach);
  const canChip = extras.canLogSets && sessionLive;
  const writeLine = (line: number, text: string, current: CellLine | undefined) =>
    onWriteCellLine(row.exercise_slot_id, week.id, line, text, {
      intent: current && current.text.trim() !== "" ? "edit" : "new",
    });
  const flipLine = (line: number, text: string, kind: "set" | "cue") =>
    onWriteCellLine(row.exercise_slot_id, week.id, line, text, { intent: "edit", kind });
  const [athleteOpen, setAthleteOpen] = useState(false);
  const markerRef = useRef<HTMLButtonElement>(null);
  const athleteSetCount = cell.athlete_summary?.sets ?? athleteLines.length;
  const summary = cell.athlete_summary;
  const loadPart = summary && summary.load ? ` · ${summary.load}${summary.unit ? ` ${summary.unit}` : ""}` : "";
  const missed = summary?.missed ?? 0;
  const athleteMarkerText = `✓ ${athleteSetCount} ${athleteSetCount === 1 ? "set" : "sets"}${loadPart}${
    summary && summary.rpe ? ` @${summary.rpe}` : ""
  }${missed > 0 ? ` · ${missed} ${missed === 1 ? "miss" : "misses"}` : ""}`;
  const athleteSetsLabel = `${athleteSetCount} ${athleteSetCount === 1 ? "set" : "sets"} logged by your athlete${
    missed > 0 ? `, ${missed} missed` : ""
  }`;

  function onAthleteKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Escape" || !athleteOpen) return;
    // Park focus on the cell's own text input (a real grid stop, so useTableNav
    // re-anchors there) BEFORE the athlete inputs unmount.
    document.querySelector<HTMLInputElement>(`[data-testid="cell-text-${cellId}"]`)?.focus();
    setAthleteOpen(false);
  }

  function onAthleteBlur(e: FocusEvent<HTMLDivElement>) {
    const next = e.relatedTarget;
    if (next instanceof Node && e.currentTarget.contains(next)) return;
    setAthleteOpen(false);
  }

  function onCopy(e: ClipboardEvent<HTMLInputElement>) {
    const el = e.currentTarget;
    if (el.selectionStart !== el.selectionEnd) return; // real selection: native copy wins.
    e.preventDefault();
    const stack = [draft, ...lines.filter((l) => l.text.trim() !== "").map((l) => l.text)].join("\n");
    e.clipboardData.setData("text/plain", stack);
  }

  function onPaste(e: ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text/plain");
    if (!pasted.includes("\n")) return; // single line: native caret insertion into the draft.
    e.preventDefault();
    const parts = pasted.replace(/\r\n/g, "\n").split("\n");
    const head = parts[0] ?? "";
    const rest = parts.slice(1);
    while (rest.length && (rest[rest.length - 1] ?? "").trim() === "") rest.pop();
    dirtyRef.current = false;
    setDraft(head);
    onPatchCell(cellId, { text: head });
    // The pasted head is committed while focus stays here — it's the new
    // Escape baseline (same rule as the Enter handler's), or Escape would
    // roll the UI back past the commit.
    tableNav.setRevertBaseline(row.exercise_slot_id, week.id, "text", head);
    // The pasted lines go onto the NEXT free line numbers (>= 1), skipping
    // every performance line (athlete_authored, whoever entered it): a coach-
    // logged set is the athlete's record, not plan text, and a plan operation
    // never rewrites it.
    const athleteNums = new Set(lines.filter((l) => l.athlete_authored).map((l) => l.line));
    let n = 0;
    let lastUsed = 0;
    for (const text of rest) {
      n += 1;
      while (athleteNums.has(n)) n += 1;
      const current = lines.find((l) => l.line === n);
      onWriteCellLine(row.exercise_slot_id, week.id, n, text, {
        intent: !current || current.text.trim() === "" ? "new" : "edit",
      });
      lastUsed = n;
    }
    // Blank any existing, non-athlete line beyond the pasted stack so the
    // result equals the source cell (a cleared line stays rendered in place —
    // Phase 2a's blank-upsert semantics — rather than carrying stale text).
    for (const l of lines) {
      if (l.line > lastUsed && l.text !== "" && !athleteNums.has(l.line)) {
        onWriteCellLine(row.exercise_slot_id, week.id, l.line, "", { intent: "edit" });
      }
    }
  }

  // Ctrl/Cmd+Enter fill-across-weeks — see the doc comment above. Bound on
  // the wrapping div (not a single input) so it fires from line 0 or any
  // sub-line/ghost via bubbling; useTableNav's own cellProps.onKeyDown
  // deliberately bails on ctrl/meta keys (it's a generic per-input grid
  // handler, not the place for a one-off verb like this), so plain Enter
  // still commits-and-moves-down there without ever reaching this.
  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    // Shift/Alt must be absent so this stays one unambiguous chord and never
    // shadows a text-editing or browser combination.
    if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey || e.key !== "Enter") return;
    e.preventDefault();
    if (busy) return;
    // Commit the focused draft BEFORE dispatching. The retired Fill button
    // committed implicitly — clicking it blurred the input — but a keybinding
    // fires with focus still in the cell, so an in-progress edit would not yet
    // be queued. fillAcrossWeeks' flushPendingWrites() only awaits writes
    // ALREADY queued, and the server copies the source stack from the DB, so
    // without this the fill spreads the stale stored text and the refetch then
    // clobbers the edit. Blur runs commitIfDirty synchronously (queueing the
    // write in time for the flush); refocus restores the grid anchor, and the
    // re-fire of onFocus reseeds the Escape baseline past the commit.
    const active = document.activeElement;
    if (active instanceof HTMLInputElement && e.currentTarget.contains(active)) {
      active.blur();
      active.focus();
    }
    onFillAcrossWeeks(cellId);
  }

  return (
    <div className="meso-table-cell-editor" onKeyDown={onKeyDown}>
      <input
        className="meso-cell meso-text-input"
        data-testid={`cell-text-${cellId}`}
        data-grid-cell={tableCellDomKey(row.exercise_slot_id, week.id, "text")}
        aria-label={tableCellAriaLabel(row.name, week.label, "text")}
        placeholder="—"
        value={draft}
        onChange={(e) => {
          dirtyRef.current = true;
          setDraft(e.target.value);
        }}
        onBlur={commitIfDirty}
        onCopy={onCopy}
        onPaste={onPaste}
        {...navProps}
      />
      {inlineLines.map((l) => {
        const isCoachSet = !!l.athlete_authored && !!l.entered_by_coach;
        const chip = isCoachSet
          ? "logged"
          : canChip && l.loggable && l.text.trim() !== ""
            ? "log-as-set"
            : null;
        return (
          <CellSubLineInput
            key={l.line}
            cellId={cellId}
            lineId={l.id}
            rowId={row.exercise_slot_id}
            weekId={week.id}
            line={l.line}
            text={l.text}
            origin={isCoachSet ? "coach" : "cue"}
            athleteFirst={extras.athleteFirst}
            chip={chip}
            onFlip={flipLine}
            unsaved={ui?.unsaved?.includes(l.line)}
            onRetry={(line) => extras.onRetryCellLine(row.exercise_slot_id, week.id, line)}
            tableNav={tableNav}
            onWrite={(line, text) => writeLine(line, text, l)}
          />
        );
      })}
      {athleteLines.length > 0 ? (
        <div
          className="meso-athlete-group"
          data-testid={`cell-athlete-group-${cellId}`}
          onKeyDown={onAthleteKeyDown}
          onBlur={onAthleteBlur}
        >
          <button
            type="button"
            ref={markerRef}
            className="meso-athlete-marker"
            data-testid={`cell-athlete-marker-${cellId}`}
            aria-expanded={athleteOpen}
            // The cell column is narrow, so the marker wraps; the title
            // carries the full roll-up as a tooltip.
            title={athleteMarkerText}
            aria-label={`${athleteSetsLabel} — ${athleteOpen ? "hide" : "show"} lines`}
            onClick={() => {
              // Collapsing unmounts the athlete inputs that may own the nav
              // anchor — re-anchor on the cell's text input first.
              if (athleteOpen) {
                document.querySelector<HTMLInputElement>(`[data-testid="cell-text-${cellId}"]`)?.focus();
              }
              setAthleteOpen((o) => !o);
            }}
          >
            {athleteMarkerText}
          </button>
          {athleteOpen
            ? athleteLines.map((l) => (
                <CellSubLineInput
                  key={l.line}
                  cellId={cellId}
                  lineId={l.id}
                  rowId={row.exercise_slot_id}
                  weekId={week.id}
                  line={l.line}
                  text={l.text}
                  origin="athlete"
                  athleteFirst={extras.athleteFirst}
                  tableNav={tableNav}
                  onWrite={() => {}}
                />
              ))
            : null}
        </div>
      ) : null}
      <CellSubLineInput
        key="ghost"
        cellId={cellId}
        rowId={row.exercise_slot_id}
        weekId={week.id}
        line={nextLine}
        text=""
        ghost
        tableNav={tableNav}
        onWrite={(line, text) => writeLine(line, text, undefined)}
      />
      {ui?.refusals?.length ? (
        <div className="meso-cell-alert" role="alert" data-testid={`cell-refusal-${cellId}`}>
          {ui.refusals.map((r, i) => (
            <div key={r.id} className="meso-cell-refusal" data-testid={`cell-refusal-row-${cellId}-${i}`}>
              <span>{r.message}</span>{" "}
              <q className="meso-cell-alert-text" data-testid={`cell-refusal-text-${cellId}-${i}`}>
                {r.text}
              </q>
              <span className="meso-cell-alert-actions">
                {r.canAdd ? (
                  <button
                    type="button"
                    data-testid={`cell-refusal-add-${cellId}-${i}`}
                    onClick={() => {
                      onWriteCellLine(row.exercise_slot_id, week.id, nextLine, r.text, { intent: "new" });
                      extras.onDiscardRefusal(row.exercise_slot_id, week.id, r.id);
                    }}
                  >
                    Add as a new line
                  </button>
                ) : null}
                <button
                  type="button"
                  data-testid={`cell-refusal-discard-${cellId}-${i}`}
                  onClick={() => extras.onDiscardRefusal(row.exercise_slot_id, week.id, r.id)}
                >
                  Discard
                </button>
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {ui?.notice?.kind === "error" ? (
        <div className="meso-cell-alert" role="alert" data-testid={`cell-error-${cellId}`}>
          <span>{ui.notice.message}</span>
          <button
            type="button"
            className="meso-cell-alert-dismiss"
            aria-label="Dismiss"
            onClick={() => extras.onDismissCellNotice(row.exercise_slot_id, week.id)}
          >
            ×
          </button>
        </div>
      ) : null}
      {ui?.notice?.kind === "moved" ? (
        <div className="meso-cell-note" role="status" data-testid={`cell-notice-${cellId}`}>
          {ui.notice.message}
        </div>
      ) : null}
    </div>
  );
}

interface RowColumnInputProps {
  row: GridRow;
  field: "tempo" | "rest" | "note";
  label: string;
  tableNav: UseTableNavResult;
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
}

/** Phase 2a (D2): one per-exercise row column (Tempo / Notes / Rest) — a row
 * attribute off the block-shared ExerciseSlot, NOT a per-week cell value.
 * Same dirty-tracking commit-on-blur/Enter + Escape-revert shape as
 * CellSubLineInput above. Phase 2b put these columns INSIDE useTableNav's
 * horizontal axis (name → tempo → weeks… → notes → rest, the source
 * spreadsheet's order), so Enter/Escape and the arrows come from cellProps
 * now instead of a local onKeyDown. */
function RowColumnInput({ row, field, label, tableNav, onPatchRowColumns }: RowColumnInputProps) {
  const synced = row[field];
  const [draft, setDraft] = useState(synced);
  const dirtyRef = useRef(false);

  useEffect(() => {
    setDraft(synced);
    dirtyRef.current = false;
  }, [synced]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onPatchRowColumns(row.exercise_slot_id, { [field]: draft });
  }

  const navProps = tableNav.cellProps(row.exercise_slot_id, null, field, {
    onCommit: commitIfDirty,
    onRevert: (value) => {
      dirtyRef.current = false;
      setDraft(value);
    },
  });

  return (
    <input
      className="meso-cell meso-row-col-input"
      data-testid={`row-${field}-${row.exercise_slot_id}`}
      data-grid-cell={tableCellDomKey(row.exercise_slot_id, null, field)}
      aria-label={`${row.name || "exercise"} — ${label}`}
      placeholder="—"
      value={draft}
      onChange={(e) => {
        dirtyRef.current = true;
        setDraft(e.target.value);
      }}
      onBlur={commitIfDirty}
      {...navProps}
    />
  );
}

interface RowNameEditorProps {
  row: GridRow;
  tableNav: UseTableNavResult;
  onRename(exerciseSlotId: Id, name: string, exerciseId?: string | null): void;
}

// The server names a freshly added exercise "New exercise" (views.py). The
// input shows it as a PLACEHOLDER over an empty value (#636) so the coach's
// first keystrokes replace rather than append. The server accepts a blank
// name verbatim (an athlete would see an unnamed exercise), so a name left
// empty is saved back as this default instead.
const DEFAULT_EXERCISE_NAME = "New exercise";
const displayName = (name: string) => (name === DEFAULT_EXERCISE_NAME ? "" : name);

// #608: the suggestion source, provided once by MesoTable (hydrated lists
// merged with the grid's own row names) and read by every RowNameEditor — a
// context rather than a prop so it needn't thread through TableDayBlock/
// TableRow, which never use it.
const ExerciseSuggestContext = createContext<ExerciseSuggestions>(EMPTY_SUGGESTIONS);

function RowNameEditor({ row, tableNav, onRename }: RowNameEditorProps) {
  const [value, setValue] = useState(displayName(row.name));
  // Combobox state (#608). `open` flips on only when a KEYSTROKE edits the
  // value (onChange), never on mere focus; `active` is the highlighted
  // option (-1 = none: free text always wins, nothing auto-replaces).
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dirtyRef = useRef(false);
  const listId = useId();
  const source = useContext(ExerciseSuggestContext);
  const suggestions = open ? suggestExercises(value, source) : [];
  const listVisible = suggestions.length > 0;
  const optionId = (i: number) => `${listId}-opt-${i}`;

  useEffect(() => {
    setValue(displayName(row.name));
    dirtyRef.current = false;
  }, [row.name]);

  // The popup is PORTALED to <body> with position: fixed from the input's
  // rect. The input lives inside .meso-table-scroll (overflow-x: auto, which
  // clips any absolutely positioned descendant on both axes) AND a sticky
  // first column with its own stacking context — an absolute list would be
  // clipped or sit under the week columns. Fixed + portal escapes both; the
  // rect is re-measured on scroll/resize (capture, to catch the scroll
  // container) so the list tracks the input.
  useLayoutEffect(() => {
    if (!listVisible) return;
    const measure = () => {
      if (inputRef.current) setRect(inputRef.current.getBoundingClientRect());
    };
    measure();
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [listVisible, value]);

  function commitIfDirty() {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onRename(row.exercise_slot_id, value.trim() === "" ? DEFAULT_EXERCISE_NAME : value);
  }

  // Mirrors GridCellEditor's revertField/ExerciseRow's revert: writes the
  // focus-time value directly (bypassing the dirtying onChange path) and
  // clears the dirty flag so a subsequent blur doesn't re-commit the draft
  // the coach just backed out of.
  function revert(newValue: string) {
    dirtyRef.current = false;
    setValue(newValue);
    setOpen(false);
    setActive(-1);
  }

  // A pick is ONE commit carrying name + link together (one POST, one undo
  // step). Clearing dirtyRef stops the blur that follows from re-committing
  // the same text as a plain (unlinking) typed rename; the Escape baseline
  // moves to the picked name, as Enter's own commit does, so a later Escape
  // can't roll the UI back past the write.
  function pick(s: Suggestion) {
    dirtyRef.current = false;
    setValue(s.name);
    setOpen(false);
    setActive(-1);
    tableNav.setRevertBaseline(row.exercise_slot_id, null, "name", s.name);
    onRename(row.exercise_slot_id, s.name, s.exerciseId);
  }

  const navProps = tableNav.cellProps(row.exercise_slot_id, null, "name", {
    onCommit: commitIfDirty,
    onRevert: revert,
  });

  // Compose, don't replace: the combobox consumes a key only when the list is
  // visible (and, for Enter/Tab, only with a highlighted option); anything
  // else falls through to the grid-nav handler exactly as before.
  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    const plain = !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey;
    if (listVisible && plain) {
      const n = suggestions.length;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        setActive((a) => (event.key === "ArrowDown" ? (a + 1) % n : a <= 0 ? n - 1 : a - 1));
        return;
      }
      if (event.key === "Escape") {
        // Closes the list ONLY — the draft stays; the next Escape reverts.
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
        setActive(-1);
        return;
      }
      const picked = active >= 0 ? suggestions[active] : undefined;
      if (picked && event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        pick(picked);
        return;
      }
      if (picked && event.key === "Tab") {
        // Accept, then let the normal Tab navigation move on.
        pick(picked);
      }
    }
    navProps.onKeyDown(event);
  }

  return (
    <>
      <input
        ref={inputRef}
        className="meso-cell meso-ex-name-input"
        data-testid={`row-name-${row.exercise_slot_id}`}
        data-grid-cell={tableCellDomKey(row.exercise_slot_id, null, "name")}
        aria-label={tableCellAriaLabel(row.name, null, "name")}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={listVisible}
        aria-controls={listId}
        aria-activedescendant={listVisible && active >= 0 ? optionId(active) : undefined}
        placeholder={DEFAULT_EXERCISE_NAME}
        value={value}
        onChange={(e) => {
          dirtyRef.current = true;
          setValue(e.target.value);
          setOpen(true);
          setActive(-1);
        }}
        onBlur={() => {
          setOpen(false);
          setActive(-1);
          commitIfDirty();
        }}
        {...navProps}
        onKeyDown={onKeyDown}
      />
      {listVisible &&
        rect &&
        createPortal(
          <ul
            id={listId}
            role="listbox"
            aria-label="Exercise suggestions"
            className="meso-suggest-list"
            style={{ top: rect.bottom + 2, left: rect.left, minWidth: rect.width }}
          >
            {suggestions.map((s, i) => (
              <li
                key={`${s.source}:${s.name}`}
                id={optionId(i)}
                role="option"
                aria-selected={i === active}
                className={i === active ? "meso-suggest-option is-active" : "meso-suggest-option"}
                // preventDefault keeps focus in the input: a blur here would
                // commit the half-typed text as a plain rename before the pick.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pick(s)}
              >
                <span className="meso-suggest-name">{s.name}</span>
                <span className="meso-suggest-hint">{s.source === "mine" ? "yours" : "catalog"}</span>
              </li>
            ))}
          </ul>,
          document.body,
        )}
    </>
  );
}

interface AddThisWeekControlProps {
  day: GridDay;
  weeks: GridWeek[];
  busy: boolean;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
}

/** P2: alongside the existing block-wide "+ Add exercise" — a toggle that
 * reveals a week picker (one button per live week), for adding an exercise
 * to just one week instead of the whole block. Local open/closed state,
 * independent per day. */
function AddThisWeekControl({ day, weeks, busy, onAddExerciseThisWeek }: AddThisWeekControlProps) {
  const [open, setOpen] = useState(false);
  const slotId = day.session_slot_id;

  return (
    <div className="meso-table-add-this-week">
      <button
        type="button"
        data-hover="add"
        className="meso-add-row"
        data-testid={`add-this-week-${slotId}`}
        disabled={busy}
        aria-label="Add exercise for this week only"
        onClick={() => setOpen((v) => !v)}
      >
        + Add this week only
      </button>
      {open && (
        <div className="meso-week-picker">
          {weeks.map((week) => (
            <button
              type="button"
              key={week.id}
              data-testid={`add-this-week-${slotId}-${week.id}`}
              className="meso-week-strip-btn"
              disabled={busy}
              onClick={() => {
                onAddExerciseThisWeek(day, week.id);
                setOpen(false);
              }}
            >
              {week.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Keys on the CELL itself (after Escape parked focus there, #653): a second
 * Escape leaves the table; Enter/F2 re-enter the cell's first input; Tab and
 * arrows move on from the cell (useTableNav.cellKeyDown). Keys
 * bubbling up from the inputs are not ours (target check). */
function cellKeyDown(event: KeyboardEvent<HTMLTableCellElement>, tableNav: UseTableNavResult) {
  if (event.target !== event.currentTarget) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.currentTarget.blur();
  } else if (event.key === "Enter" || event.key === "F2") {
    event.preventDefault();
    event.currentTarget.querySelector<HTMLInputElement>("input")?.focus();
  } else {
    // Tab / arrows move on from the cancelled cell (#656).
    tableNav.cellKeyDown(event);
  }
}

interface TableRowProps {
  row: GridRow;
  day: GridDay;
  weeks: GridWeek[];
  busy: boolean;
  tableNav: UseTableNavResult;
  rowArmed: boolean;
  onArmRow(): void;
  onConfirmRemoveRow(): void;
  onCancelRemoveRow(): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string, opts?: CellLineWriteOpts): void;
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string, exerciseId?: string | null): void;
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
}

/** Issue #455 phase A2: one exercise row — now a dnd-kit sortable item
 * within its day's own row SortableContext. Drag LISTENERS are bound only
 * to the handle button (dnd-kit's documented "drag handle" pattern, mirrors
 * ExerciseRow.tsx) — a click/drag anywhere else in the row (a cell input, a
 * badge) never starts a drag. No live CSS.Transform on the <tr> (see this
 * file's header) — only the `.is-dragging` opacity class. */
function TableRow({
  row,
  day,
  weeks,
  busy,
  tableNav,
  rowArmed,
  onArmRow,
  onConfirmRemoveRow,
  onCancelRemoveRow,
  onPatchCell,
  onWriteCellLine,
  onPatchRowColumns,
  onRenameExercise,
  onSkipCell,
  onFillAcrossWeeks,
}: TableRowProps) {
  const dragData: TableDragData = {
    type: "row",
    daySlotId: day.session_slot_id,
    exerciseSlotId: row.exercise_slot_id,
  };
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useSortable({
    id: tableRowDragId(day.session_slot_id, row.exercise_slot_id),
    data: dragData,
    disabled: busy,
  });

  return (
    <tr
      ref={setNodeRef}
      className={isDragging ? "is-dragging" : undefined}
      data-testid={`meso-row-${row.exercise_slot_id}`}
    >
      <td className="meso-table-row-name-col" tabIndex={-1} onKeyDown={(event) => cellKeyDown(event, tableNav)}>
        <div className="meso-table-row-name-row">
          <button
            type="button"
            ref={setActivatorNodeRef}
            data-testid={`row-drag-${row.exercise_slot_id}`}
            className="meso-drag-handle"
            aria-label={`Reorder ${row.name || "exercise"}`}
            disabled={busy}
            {...attributes}
            {...listeners}
          >
            ⠿
          </button>
          <RowNameEditor row={row} tableNav={tableNav} onRename={onRenameExercise} />
          {!rowArmed && (
            <button
              type="button"
              data-testid={`remove-exercise-${row.exercise_slot_id}`}
              className="meso-remove-x meso-remove-x--sm"
              disabled={busy}
              aria-label="Remove exercise"
              title="Remove exercise"
              onClick={onArmRow}
            >
              ×
            </button>
          )}
          {rowArmed && (
            <span className="meso-confirm-pair">
              <button
                type="button"
                data-testid={`confirm-remove-exercise-${row.exercise_slot_id}`}
                className="meso-confirm-btn"
                disabled={busy}
                aria-label="Confirm remove exercise"
                onClick={onConfirmRemoveRow}
              >
                Confirm?
              </button>
              <button
                type="button"
                data-testid={`cancel-remove-exercise-${row.exercise_slot_id}`}
                className="meso-cancel-btn"
                disabled={busy}
                aria-label="Cancel remove exercise"
                onClick={onCancelRemoveRow}
              >
                Cancel
              </button>
            </span>
          )}
        </div>      </td>
      <td className="meso-table-row-col meso-table-row-col--tempo" tabIndex={-1} onKeyDown={(event) => cellKeyDown(event, tableNav)}>
        <RowColumnInput row={row} field="tempo" label="tempo" tableNav={tableNav} onPatchRowColumns={onPatchRowColumns} />
      </td>
      {weeks.map((week) => {
        const cell = row.cells[String(week.id)];
        const testId = `cell-${row.exercise_slot_id}-${week.id}`;
        if (!cell) return <td key={week.id} data-testid={testId} />;
        return (
          <td key={week.id} data-testid={testId} className="meso-table-cell" tabIndex={-1} data-grid-td={cell.skipped ? tableCellDomKey(row.exercise_slot_id, week.id, "text") : undefined} onKeyDown={(event) => cellKeyDown(event, tableNav)}>
            {cell.skipped ? (
              <>
                <span className="meso-table-skipped" data-testid={`cell-skipped-${cell.prescription_id}`}>
                  —
                </span>
                <button
                  type="button"
                  data-testid={`cell-unskip-${cell.prescription_id}`}
                  className="meso-cell-action-btn"
                  disabled={busy}
                  aria-label="Unskip this week"
                  title="Unskip this week"
                  onClick={() => onSkipCell(cell.prescription_id, false)}
                >
                  Unskip
                </button>
              </>
            ) : (
              <GridCellEditor
                cell={cell}
                row={row}
                week={week}
                busy={busy}
                tableNav={tableNav}
                sessionLive={day.session_ids[String(week.id)] != null}
                onPatchCell={onPatchCell}
                onWriteCellLine={onWriteCellLine}
                onFillAcrossWeeks={onFillAcrossWeeks}
              />
            )}
          </td>
        );
      })}
      <td className="meso-table-row-col meso-table-row-col--note" tabIndex={-1} onKeyDown={(event) => cellKeyDown(event, tableNav)}>
        <RowColumnInput row={row} field="note" label="notes" tableNav={tableNav} onPatchRowColumns={onPatchRowColumns} />
      </td>
      <td className="meso-table-row-col meso-table-row-col--rest" tabIndex={-1} onKeyDown={(event) => cellKeyDown(event, tableNav)}>
        <RowColumnInput row={row} field="rest" label="rest" tableNav={tableNav} onPatchRowColumns={onPatchRowColumns} />
      </td>
    </tr>
  );
}

interface TableDayBlockProps {
  day: GridDay;
  weeks: GridWeek[];
  busy: boolean;
  tableNav: UseTableNavResult;
  isArmed(type: ArmedKind, id: Id): boolean;
  arm(type: ArmedKind, id: Id): void;
  disarm(): void;
  onPatchCell(cellId: Id, patch: GridCellPatch): void;
  onWriteCellLine(exerciseSlotId: Id, weekId: Id, line: number, text: string, opts?: CellLineWriteOpts): void;
  onPatchRowColumns(exerciseSlotId: Id, patch: GridRowPatch): void;
  onRenameExercise(exerciseSlotId: Id, name: string, exerciseId?: string | null): void;
  onRenameDay(sessionSlotId: Id, name: string): void;
  onAddExercise(day: GridDay): void;
  onRemoveExercise(exerciseSlotId: Id): void;
  onRemoveDay(day: GridDay): void;
  onSkipCell(cellId: number, skipped: boolean): void;
  onFillAcrossWeeks(cellId: number): void;
  onAddExerciseThisWeek(day: GridDay, weekId: number): void;
}

/** Issue #455 phase A2: one training day's table — now also a dnd-kit
 * sortable item within the block's own day-strip SortableContext (mirrors
 * DayCard.tsx). Same no-live-transform rule as TableRow above — only
 * `.is-dragging` opacity, no CSS.Transform on the block itself. */
function TableDayBlock({
  day,
  weeks,
  busy,
  tableNav,
  isArmed,
  arm,
  disarm,
  onPatchCell,
  onWriteCellLine,
  onPatchRowColumns,
  onRenameExercise,
  onRenameDay,
  onAddExercise,
  onRemoveExercise,
  onRemoveDay,
  onSkipCell,
  onFillAcrossWeeks,
  onAddExerciseThisWeek,
}: TableDayBlockProps) {
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(day.name);
  const dayArmed = isArmed("day", day.session_slot_id);
  const dragData: TableDragData = { type: "day", sessionSlotId: day.session_slot_id };
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useSortable({
    id: tableDayDragId(day.session_slot_id),
    data: dragData,
    disabled: busy,
  });
  const dayHandleLabel = `Reorder ${day.name || `Day ${day.day_number}`}`;
  const dayLabel = day.name || `Day ${day.day_number}`;

  // #652: day names are editable, so Tab out of one must continue INTO that
  // day (its first exercise name, or "+ Add exercise" when empty). Native Tab
  // would fall to the roving-tabindex anchor, which can sit in another day.
  const tabIntoDay = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab" || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
    const first = day.rows[0];
    const target = first
      ? document.querySelector<HTMLElement>(`[data-grid-cell="${tableCellDomKey(first.exercise_slot_id, null, "name")}"]`)
      : document.querySelector<HTMLElement>(`[data-testid="add-exercise-${day.session_slot_id}"]`);
    if (!target) return;
    event.preventDefault();
    target.focus();
  };

  const beginNameEdit = () => {
    setNameDraft(day.name);
    setEditingName(true);
  };
  const cancelNameEdit = () => {
    setNameDraft(day.name);
    setEditingName(false);
  };
  const commitNameEdit = () => {
    const name = nameDraft.trim();
    setEditingName(false);
    setNameDraft(name);
    if (name !== day.name) onRenameDay(day.session_slot_id, name);
  };

  return (
    <div className={`meso-table-day${isDragging ? " is-dragging" : ""}`} ref={setNodeRef}>
      <div className="meso-table-day-header">
        <button
          type="button"
          ref={setActivatorNodeRef}
          data-testid={`day-drag-${day.session_slot_id}`}
          className="meso-drag-handle"
          aria-label={dayHandleLabel}
          disabled={busy}
          {...attributes}
          {...listeners}
        >
          ⠿
        </button>
        {editingName ? (
          <input
            autoFocus
            aria-label="Day name"
            className="meso-day-name-input"
            maxLength={255}
            value={nameDraft}
            onChange={(event) => setNameDraft(event.target.value)}
            onBlur={commitNameEdit}
            onKeyDown={(event) => {
              event.stopPropagation();
              tabIntoDay(event);
              if (event.key === "Enter") {
                event.preventDefault();
                commitNameEdit();
              } else if (event.key === "Escape") {
                event.preventDefault();
                cancelNameEdit();
              }
            }}
          />
        ) : (
          <button
            type="button"
            className="meso-day-name"
            aria-label={`Rename day: ${dayLabel}`}
            title="Rename day"
            disabled={busy}
            onKeyDown={tabIntoDay}
            onClick={beginNameEdit}
          >
            {dayLabel}
          </button>
        )}
        {day.bias && <div className="meso-day-bias">{day.bias}</div>}
        <div className="meso-flex-spacer" />
        {!dayArmed && (
          <button
            type="button"
            data-testid={`remove-day-${day.session_slot_id}`}
            className="meso-remove-x"
            disabled={busy}
            aria-label="Remove this day"
            title="Remove this day"
            onClick={() => arm("day", day.session_slot_id)}
          >
            ×
          </button>
        )}
        {dayArmed && (
          <span className="meso-confirm-pair">
            <button
              type="button"
              data-testid={`confirm-remove-day-${day.session_slot_id}`}
              className="meso-confirm-btn"
              disabled={busy}
              aria-label="Confirm remove day"
              onClick={() => {
                onRemoveDay(day);
                disarm();
              }}
            >
              Confirm?
            </button>
            <button
              type="button"
              data-testid={`cancel-remove-day-${day.session_slot_id}`}
              className="meso-cancel-btn"
              disabled={busy}
              aria-label="Cancel remove day"
              onClick={disarm}
            >
              Cancel
            </button>
          </span>
        )}
      </div>

      <div className="meso-table-wrap">
        <table
          className="meso-table"
          style={{ width: tableWidthFor(weeks.length) }}
          data-testid={`meso-day-table-${day.session_slot_id}`}
        >
          {/* Shared fixed column geometry so every day's table aligns column-
              for-column (table-layout: fixed keys off these widths, not
              content, so separators line up across days and tall sub-line
              rows). */}
          <colgroup>
            <col style={{ width: COL_WIDTHS.exercise }} />
            <col style={{ width: COL_WIDTHS.tempo }} />
            {weeks.map((week) => (
              <col key={week.id} style={{ width: COL_WIDTHS.week }} />
            ))}
            <col style={{ width: COL_WIDTHS.notes }} />
            <col style={{ width: COL_WIDTHS.rest }} />
          </colgroup>
          <thead>
            <tr>
              <th className="meso-table-exercise-col">Exercise</th>
              <th className="meso-table-row-col-th">Tempo</th>
              {weeks.map((week) => (
                <WeekColumnHeader key={week.id} week={week} />
              ))}
              <th className="meso-table-row-col-th">Notes</th>
              <th className="meso-table-row-col-th">Rest</th>
            </tr>
          </thead>
          <tbody>
            <SortableContext
              items={day.rows.map((r) => tableRowDragId(day.session_slot_id, r.exercise_slot_id))}
              strategy={verticalListSortingStrategy}
            >
              {day.rows.map((row) => (
                <TableRow
                  key={row.exercise_slot_id}
                  row={row}
                  day={day}
                  weeks={weeks}
                  busy={busy}
                  tableNav={tableNav}
                  rowArmed={isArmed("exercise", row.exercise_slot_id)}
                  onArmRow={() => arm("exercise", row.exercise_slot_id)}
                  onConfirmRemoveRow={() => {
                    onRemoveExercise(row.exercise_slot_id);
                    disarm();
                  }}
                  onCancelRemoveRow={disarm}
                  onPatchCell={onPatchCell}
                  onWriteCellLine={onWriteCellLine}
                  onPatchRowColumns={onPatchRowColumns}
                  onRenameExercise={onRenameExercise}
                  onSkipCell={onSkipCell}
                  onFillAcrossWeeks={onFillAcrossWeeks}
                />
              ))}
            </SortableContext>
          </tbody>
        </table>
      </div>

      <div className="meso-table-add-row-group">
        <button
          type="button"
          data-hover="add"
          className="meso-add-row"
          data-testid={`add-exercise-${day.session_slot_id}`}
          disabled={busy}
          onClick={() => onAddExercise(day)}
        >
          + Add exercise
        </button>
        <AddThisWeekControl day={day} weeks={weeks} busy={busy} onAddExerciseThisWeek={onAddExerciseThisWeek} />
      </div>
    </div>
  );
}

// Keyboard focus into a week column must not land under the sticky Exercise
// column (or past the right edge). `scroll-padding-left` does this where it's
// honoured (Chromium); WebKit's focus-scroll ignores it, so nudge the shared
// scroller ourselves. Cells inside the sticky column itself are left alone.
function keepFocusClearOfStickyColumn(event: FocusEvent<HTMLElement>) {
  const target = event.target as HTMLElement;
  // Anything pinned to the scroller's left (the Exercise column, a day's title row
  // and add bar) is always visible: scrolling for it would throw the table back.
  if (target.closest(".meso-table-row-name-col, .meso-table-exercise-col, .meso-table-day-header, .meso-table-add-row-group")) return;
  const scroller = event.currentTarget;
  const view = scroller.getBoundingClientRect();
  const box = target.getBoundingClientRect();
  const hiddenLeft = view.left + COL_WIDTHS.exercise - box.left;
  const hiddenRight = box.right - view.right;
  if (hiddenLeft > 0) scroller.scrollLeft -= hiddenLeft;
  else if (hiddenRight > 0) scroller.scrollLeft += hiddenRight;
}

/** A horizontal scrollbar pinned to the bottom of the canvas while the table is
 * on screen (#701). The one shared scroller's own scrollbar sits under the LAST
 * day, so a coach with a plain mouse looking at Day 1 couldn't reach weeks 5-6.
 * This is a mirror strip, a sibling right after the scroller: `position: sticky;
 * bottom: 0` keeps it at the canvas's bottom edge until the table's end is
 * reached, where it sits in its natural slot and hides (the real bar is then in
 * view). `aria-hidden`: the real scroller keeps keyboard and focus behaviour.
 *
 * Two-way scrollLeft sync with no echo loop: each side only writes when the
 * other differs, and assigning an equal scrollLeft fires no scroll event.
 * Hidden when the table doesn't overflow. The strip keeps its height while
 * hidden-because-the-real-bar-is-visible so showing/hiding never shifts layout. */
function ScrollMirror({ scrollerRef }: { scrollerRef: RefObject<HTMLDivElement | null> }) {
  const mirrorRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const [scrollWidth, setScrollWidth] = useState<number | null>(null);
  const [realBarInView, setRealBarInView] = useState(true);

  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    const mirror = mirrorRef.current;
    const end = endRef.current;
    if (!scroller || !mirror || !end) return;

    const measure = () => {
      setScrollWidth(scroller.scrollWidth > scroller.clientWidth ? scroller.scrollWidth : null);
    };
    const follow = (from: HTMLElement, to: HTMLElement) => () => {
      if (Math.abs(from.scrollLeft - to.scrollLeft) > 0.5) to.scrollLeft = from.scrollLeft;
    };
    const onScroller = follow(scroller, mirror);
    const onMirror = follow(mirror, scroller);
    scroller.addEventListener("scroll", onScroller, { passive: true });
    mirror.addEventListener("scroll", onMirror, { passive: true });

    measure();
    // (jsdom has neither observer; the strip just stays hidden there.)
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    resize?.observe(scroller);
    if (scroller.firstElementChild) resize?.observe(scroller.firstElementChild);
    // The zero-height marker right under the scroller: in view means the
    // scroller's own scrollbar is in view.
    const seen =
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver((entries) => {
            const last = entries[entries.length - 1];
            if (last) setRealBarInView(last.isIntersecting);
          });
    seen?.observe(end);
    return () => {
      scroller.removeEventListener("scroll", onScroller);
      mirror.removeEventListener("scroll", onMirror);
      resize?.disconnect();
      seen?.disconnect();
    };
  }, [scrollerRef]);

  // The strip is rendered at the scroller's scrollLeft once it gains a width.
  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    const mirror = mirrorRef.current;
    if (scroller && mirror && scrollWidth !== null) mirror.scrollLeft = scroller.scrollLeft;
  }, [scrollerRef, scrollWidth]);

  return (
    <>
      <div ref={endRef} className="meso-table-scroll-end" />
      <div
        ref={mirrorRef}
        className="meso-table-scrollbar"
        data-testid="meso-table-scrollbar"
        aria-hidden="true"
        tabIndex={-1}
        hidden={scrollWidth === null}
        style={{ visibility: realBarInView ? "hidden" : "visible" }}
      >
        <div style={{ width: scrollWidth ?? 0, height: 1 }} />
      </div>
    </>
  );
}

export function MesoTable(props: MesoTableProps) {
  const {
    grid,
    busy,
    onPatchCell,
    onWriteCellLine,
    onPatchRowColumns,
    onRenameExercise,
    onRenameDay,
    onAddExercise,
    onRemoveExercise,
    onAddDay,
    onRemoveDay,
    onAddWeek,
    onRemoveWeek,
    onSkipCell,
    onFillAcrossWeeks,
    onAddExerciseThisWeek,
    onDragEnd,
    exerciseSuggestions,
    cellUi,
    onRetryCellLine,
    onDismissCellNotice,
    onDiscardRefusal,
    isTemplate,
  } = props;

  const scrollerRef = useRef<HTMLDivElement>(null);
  const [armed, setArmed] = useState<Armed>(null);
  const isArmed = (type: ArmedKind, id: Id) => !!armed && armed.type === type && armed.id === id;
  const arm = (type: ArmedKind, id: Id) => setArmed({ type, id });
  const disarm = () => setArmed(null);

  // Rules of Hooks: called unconditionally, before the `!grid` early return
  // below — useTableNav tolerates a null grid the same way (anchor stays
  // null, no throw). Phase 2b: Enter at the last stop of a day appends a
  // blank exercise row to THAT day (Enter-adds-row) — same verb as the day's
  // "+ Add exercise" button, same busy gate. Returning false on a dropped
  // dispatch keeps the hook from recording a focus intent for an append
  // that never happened.
  const tableNav = useTableNav({
    grid,
    onAppendRow: (dayId) => {
      const day = grid?.days.find((d) => d.session_slot_id === dayId);
      if (!day || busy) return false;
      onAddExercise(day);
      return true;
    },
  });

  const [pendingAddedRow, setPendingAddedRow] = useState<{
    dayId: number;
    existingRowIds: Set<number>;
  } | null>(null);

  useEffect(() => {
    if (!pendingAddedRow || !grid) return;
    const targetDay = grid.days.find((day) => day.session_slot_id === pendingAddedRow.dayId);
    const addedRow = targetDay?.rows.find((row) => !pendingAddedRow.existingRowIds.has(row.exercise_slot_id));
    setPendingAddedRow(null);
    if (!addedRow) return;
    document.querySelector<HTMLInputElement>(`[data-testid="row-name-${addedRow.exercise_slot_id}"]`)?.focus();
  }, [grid, pendingAddedRow]);

  async function addExerciseAndFocus(day: GridDay) {
    const existingRowIds = new Set(day.rows.map((row) => row.exercise_slot_id));
    await onAddExercise(day);
    setPendingAddedRow({ dayId: day.session_slot_id, existingRowIds });
  }

  // Issue #455 phase A2 (drag reordering): PointerSensor gets a small
  // activation distance so a plain click into a cell input doesn't start a
  // drag; KeyboardSensor rides the handle buttons' tab-order focus
  // (Space/Enter lifts, arrows move, Space/Enter drops, Escape cancels) —
  // mirrors WeekGrid.tsx's sensors exactly, pointed at this file's own
  // tableKeyboardCoordinates.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: tableKeyboardCoordinates }),
  );

  // DragOverlay ghost label — set from the lifted item's own data on drag
  // start, cleared on drop. No live sibling reflow: rows/days snap to their
  // new position only once refetchGrid resolves, matching every other
  // structural verb's UX (skip/add-exercise etc.).
  const [activeDragLabel, setActiveDragLabel] = useState<string | null>(null);

  function handleDragStart(event: DragStartEvent) {
    if (!grid) return;
    const data = event.active.data.current as TableDragData | undefined;
    if (!data) return;
    if (data.type === "row") {
      const activeDay = grid.days.find((d) => d.session_slot_id === data.daySlotId);
      const activeRow = activeDay?.rows.find((r) => r.exercise_slot_id === data.exerciseSlotId);
      setActiveDragLabel(activeRow?.name || "exercise");
    } else {
      const activeDay = grid.days.find((d) => d.session_slot_id === data.sessionSlotId);
      setActiveDragLabel(activeDay ? activeDay.name || `Day ${activeDay.day_number}` : "day");
    }
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveDragLabel(null);
    if (!onDragEnd) return;
    const { active, over } = event;
    onDragEnd({
      active: { id: active.id, data: { current: active.data.current as TableDragData } },
      over: over ? { id: over.id, data: { current: over.data.current as TableDragData } } : null,
    });
  }

  // #608: hydrated names + whatever the grid already holds (so a name typed
  // this session suggests before any reload). Hooks stay above the early return.
  const suggestSource = useMemo<ExerciseSuggestions>(() => {
    const hydrated = exerciseSuggestions ?? EMPTY_SUGGESTIONS;
    const gridRows = (grid?.days ?? []).flatMap((d) =>
      d.rows
        .filter((r) => r.name !== DEFAULT_EXERCISE_NAME)
        .map((r) => ({ name: r.name, exercise_id: r.exercise_id })),
    );
    return { catalog: hydrated.catalog, mine: mergeMine(hydrated.mine, gridRows) };
  }, [exerciseSuggestions, grid]);

  const athlete = grid?.athlete ?? null;
  const cellExtras = useMemo<CellExtras>(
    () => ({
      cellUi: cellUi ?? {},
      athleteFirst: athlete?.name?.trim().split(/\s+/)[0] || null,
      canLogSets: athlete != null && !isTemplate,
      onRetryCellLine: onRetryCellLine ?? NOOP,
      onDismissCellNotice: onDismissCellNotice ?? NOOP,
      onDiscardRefusal: onDiscardRefusal ?? NOOP,
    }),
    [cellUi, athlete, isTemplate, onRetryCellLine, onDismissCellNotice, onDiscardRefusal],
  );

  if (!grid) return null;

  return (
    <CellExtrasContext.Provider value={cellExtras}>
    <ExerciseSuggestContext.Provider value={suggestSource}>
    <div className="meso-table-view" data-testid="meso-table-view">
      <WeekManagerStrip
        weeks={grid.weeks}
        busy={busy}
        isArmed={isArmed}
        arm={arm}
        disarm={disarm}
        onRemoveWeek={onRemoveWeek}
        onAddWeek={onAddWeek}
      />

      <DndContext
        sensors={sensors}
        collisionDetection={tableCollisionDetection}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        {/* ONE horizontal scroll container for every day (605.7): the day tables
            share a scrollLeft, so "Wk 1" in Day 1 stays above "Wk 1" in Day 2.
            Chosen over synchronised per-day scrollers because there is nothing to
            keep in sync (no scroll-event feedback loops, one scrollbar, native
            focus/keyboard scroll-into-view), and the sticky Exercise column and
            the dnd-kit overlay (no live transform) need no change. */}
        <div ref={scrollerRef} className="meso-table-scroll" style={{ scrollPaddingLeft: COL_WIDTHS.exercise }} onFocus={keepFocusClearOfStickyColumn}>
          <div className="meso-table-days" style={{ width: tableWidthFor(grid.weeks.length) }}>
        <SortableContext items={grid.days.map((d) => tableDayDragId(d.session_slot_id))} strategy={verticalListSortingStrategy}>
          {grid.days.map((day) => (
            <TableDayBlock
              key={day.session_slot_id}
              day={day}
              weeks={grid.weeks}
              busy={busy}
              tableNav={tableNav}
              isArmed={isArmed}
              arm={arm}
              disarm={disarm}
              onPatchCell={onPatchCell}
              onWriteCellLine={onWriteCellLine}
              onPatchRowColumns={onPatchRowColumns}
              onRenameExercise={onRenameExercise}
              onRenameDay={onRenameDay}
              onAddExercise={addExerciseAndFocus}
              onRemoveExercise={onRemoveExercise}
              onRemoveDay={onRemoveDay}
              onSkipCell={onSkipCell}
              onFillAcrossWeeks={onFillAcrossWeeks}
              onAddExerciseThisWeek={onAddExerciseThisWeek}
            />
          ))}
        </SortableContext>
          </div>
        </div>
        <ScrollMirror scrollerRef={scrollerRef} />
        <DragOverlay>
          {activeDragLabel ? <div className="meso-table-drag-ghost">{activeDragLabel}</div> : null}
        </DragOverlay>
      </DndContext>

      <button
        type="button"
        data-hover="add"
        className="meso-add-day-btn"
        data-testid="add-day"
        disabled={busy}
        onClick={onAddDay}
      >
        + Add day
      </button>
    </div>
    </ExerciseSuggestContext.Provider>
    </CellExtrasContext.Provider>
  );
}

interface WeekColumnHeaderProps {
  week: GridWeek;
}

/** A week column's header — just the label + deload marker. The lifecycle
 * controls (remove) live in the WeekManagerStrip above the day tables
 * (designer-simplify), never inside a day, since a week spans every day.
 * Programs are date-less and carry no "current week" pointer
 * (docs/meso/remove-current-week-plan.md), so there is no per-week
 * highlight here anymore either. */
function WeekColumnHeader({ week }: WeekColumnHeaderProps) {
  return (
    <th data-testid={`week-col-${week.id}`} className="meso-table-week-col">
      <div className="meso-table-week-label">
        <span>{week.label}</span>
        {week.deload && (
          <span aria-label="Deload week" title="Deload week" className="meso-table-deload-marker">
            ▽
          </span>
        )}
      </div>
    </th>
  );
}

interface WeekManagerStripProps {
  weeks: GridWeek[];
  busy: boolean;
  isArmed(type: ArmedKind, id: Id): boolean;
  arm(type: ArmedKind, id: Id): void;
  disarm(): void;
  onRemoveWeek(weekId: Id): void;
  onAddWeek(): void;
}

/** The mesocycle-level week manager, above the day tables (designer-simplify):
 * one pill per week with remove (arm→confirm), plus "+ Add week". A week
 * spans every training day, so its lifecycle belongs here once — not
 * repeated in, or tied to, any single day's table header. Programs are
 * date-less and carry no "current week" pointer
 * (docs/meso/remove-current-week-plan.md) — every week's pill looks and
 * behaves the same; there is no "Make current" control anymore. Buttons
 * keep `data-grid-restore` so a click returns focus to the grid's anchor
 * cell (see useTableNav). */
function WeekManagerStrip({ weeks, busy, isArmed, arm, disarm, onRemoveWeek, onAddWeek }: WeekManagerStripProps) {
  return (
    <div className="meso-week-strip" role="group" aria-label="Weeks">
      <span className="meso-week-strip-heading">Weeks</span>
      {weeks.map((week) => (
        <div key={week.id} data-testid={`week-pill-${week.id}`} className="meso-week-pill">
          <span className="meso-week-pill-label">
            {week.label}
            {week.deload && (
              <span aria-label="Deload week" title="Deload week" className="meso-table-deload-marker">
                {" "}
                ▽
              </span>
            )}
          </span>
          {isArmed("week", week.id) ? (
            <span className="meso-week-strip-confirm">
              <button
                type="button"
                data-testid={`confirm-remove-week-${week.id}`}
                data-grid-restore=""
                className="meso-week-strip-btn meso-week-strip-btn--confirm"
                disabled={busy}
                aria-label="Confirm remove week"
                onClick={() => {
                  onRemoveWeek(week.id);
                  disarm();
                }}
              >
                Confirm?
              </button>
              <button
                type="button"
                data-testid={`cancel-remove-week-${week.id}`}
                data-grid-restore=""
                className="meso-week-strip-btn"
                disabled={busy}
                aria-label="Cancel remove week"
                onClick={disarm}
              >
                Cancel
              </button>
            </span>
          ) : (
            <button
              type="button"
              data-testid={`remove-week-${week.id}`}
              data-grid-restore=""
              className="meso-week-pill-x"
              disabled={busy}
              aria-label="Remove this week"
              title="Remove this week"
              onClick={() => arm("week", week.id)}
            >
              ×
            </button>
          )}
        </div>
      ))}
      <button
        type="button"
        data-testid="add-week"
        data-hover="add"
        data-grid-restore=""
        className="meso-week-strip-btn meso-week-strip-btn--dashed"
        disabled={busy}
        onClick={onAddWeek}
      >
        + Add week
      </button>
      {/* The one place fill-across-weeks is advertised. It lives here, not on
          the cell, because per-cell chrome at exercises × weeks cardinality is
          exactly what designer-simplify removed — and this strip is already
          the week-scoped surface. Muted and non-interactive: a hint, not a
          control. */}
      <span className="meso-week-strip-hint" data-testid="week-strip-fill-hint">
        Ctrl/⌘+Enter fills a cell across all weeks
      </span>
    </div>
  );
}

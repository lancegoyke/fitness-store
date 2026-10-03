// useGrid — self-contained state-owning hook for the P1 multi-week table
// (MesoTable). Owns `grid`/`history` and every verb that mutates it. Issue
// #455 phase A5 retired the sibling one-week `usePlanData` owner (and
// everything that only existed to feed it) — this hook is now DesignerRoot's
// SOLE data owner island-wide, not just the table's: `grid.plan`/
// `.athlete`/`.phases` (added in A5 step 1) feed TopBar/LeftRail/BlockView/
// AthletePreview too, via the pure `lib/grid.ts` helpers `gridToProgram`/
// `cycleLabelFromGrid`.
//
// Cell edits (patchCell/renameExercise) are optimistic + fire-and-forget,
// mirroring useAutosave's semantics (CONTRACT.md "useAutosave") — updated in
// local state immediately, POSTed without being awaited by the caller, and
// NOT rolled back on failure (only console.error'd), same as persistRow. Each
// in-flight autosave POST is tracked in `pendingWritesRef` so fillAcrossWeeks
// can flush (await) them before it fills — the fill endpoint copies the
// source cell's already-committed DB values, so an in-flight edit must land
// first or the fill can copy stale data (Codex P2).
//
// Structural verbs (add/remove day|week|exercise, undo/redo)
// await their POST, then call refetchGrid() (a plain GET, mirroring
// usePlanData's switchWeek) to re-sync the whole grid — mirroring
// usePlanData/useReorder's ref-guard idiom, one shared in-flight guard across
// every structural verb so a double-click can't race two refetches.
import { useCallback, useEffect, useRef, useState } from "react";
import { apiPost, apiPostResult } from "../lib/api";
import type { CellLine, GridCell, GridDay, GridHistory, GridRow, GridWeek, MesoGrid } from "../lib/api";

export type Id = number | string;

/** The one cell field the coach types into (text-first, Phase 2a). Everything
 * else on GridCell — prescription_id/skipped/lines — is server-derived or
 * written through its own verb (writeCellLine), never this patch. */
export type GridCellPatch = Partial<Pick<GridCell, "text">>;

/** The per-exercise row columns (Phase 2a, D2) writable via patchRowColumns. */
export type GridRowPatch = Partial<Pick<GridRow, "tempo" | "rest" | "note">>;

/** Any payload carrying a fresh plan history — tolerant of `undo_label`/
 * `redo_label` arriving as `string | null` (the `serialize_plan_history`
 * convention) as well as `GridHistory`'s always-string labels. Coerced to
 * `GridHistory` on adoption below, so either convention lands cleanly. */
interface GridHistoryCarrier {
  history?: {
    can_undo: boolean;
    can_redo: boolean;
    undo_label: string | null;
    redo_label: string | null;
  };
}

interface PlanTitleCarrier extends GridHistoryCarrier {
  plan?: { title?: string };
}

interface MesocycleNameCarrier extends GridHistoryCarrier {
  mesocycle?: { id?: Id; name?: string };
}

interface DayNameCarrier extends GridHistoryCarrier {
  day?: { session_slot_id?: Id; name?: string; day_number?: number };
}

const EMPTY_GRID_HISTORY: GridHistory = {
  can_undo: false,
  can_redo: false,
  undo_label: "",
  redo_label: "",
};

export interface UseGridOptions {
  planId: Id;
  csrf: string;
  initialGrid: MesoGrid | null;
  /** #709 PR 2: called on every local write, so the live-sync poll can switch
   * to its fast cadence. Read through a ref (a new function each render is fine). */
  onActivity?: () => void;
}

function findRow(grid: MesoGrid | null, exerciseSlotId: Id): GridRow | undefined {
  if (!grid) return undefined;
  for (const day of grid.days) {
    const row = day.rows.find((r) => r.exercise_slot_id === exerciseSlotId);
    if (row) return row;
  }
  return undefined;
}

/** The (slot, week) key of the cell with this prescription id, if on screen. */
function cellKeyOfPrescription(grid: MesoGrid | null, cellId: Id): string | null {
  if (!grid) return null;
  for (const day of grid.days) {
    for (const r of day.rows) {
      for (const [weekId, c] of Object.entries(r.cells)) {
        if (c.prescription_id === cellId) return cellUiKey(r.exercise_slot_id, weekId);
      }
    }
  }
  return null;
}

/** Key-order-insensitive JSON, to tell whether a merge changed anything. */
function stableStringify(v: unknown): string {
  return JSON.stringify(v, (_k, val) => {
    if (val && typeof val === "object" && !Array.isArray(val)) {
      const o = val as Record<string, unknown>;
      return Object.fromEntries(Object.keys(o).sort().map((k) => [k, o[k]]));
    }
    return val;
  });
}

/** The row's FIRST live week's cell — always non-swapped in normal data (see
 * CONTRACT: renameExercise must retarget a swapped cell otherwise). */
function firstWeekCellId(grid: MesoGrid | null, row: GridRow | undefined): Id | undefined {
  if (!grid || !row) return undefined;
  const firstWeek = grid.weeks[0];
  if (!firstWeek) return undefined;
  return row.cells[String(firstWeek.id)]?.prescription_id;
}

/** The row's IDENTITY cell: the first live week's cell. (Phase 2a: the
 * one-week swap fields are gone — identity is always the block-shared
 * slot's, so any cell of the row identifies it; the first live week's is the
 * stable pick.) Used by rename (prescription_patch's `name` branch rewrites
 * the block ExerciseSlot.name). */
export function rowIdentityCellId(weeks: GridWeek[], row: GridRow | undefined): Id | undefined {
  if (!row) return undefined;
  const first = weeks[0];
  return first ? row.cells[String(first.id)]?.prescription_id : undefined;
}

// The viewed week for structural verbs that need ONE week id to anchor a
// POST (add-day's `week_id`, undo/redo's `week_id`) — programs are date-less
// and carry no "current" pointer (docs/meso/remove-current-week-plan.md), so
// this is simply the grid's first live week, mirroring the server's own
// `current_week(plan)` degrade (explicit week -> else earliest live week).
function currentWeekId(grid: MesoGrid | null): Id | undefined {
  if (!grid) return undefined;
  return grid.weeks[0]?.id;
}

/** Immutably patch every cell (across every day/row/week) whose
 * prescription_id matches — in practice exactly one, since prescription_id
 * is unique per (row, week). */
function updateCellInGrid(grid: MesoGrid, cellId: Id, patch: Partial<GridCell>): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) => {
        let changed = false;
        const cells: Record<string, GridCell> = {};
        for (const [weekId, c] of Object.entries(row.cells)) {
          if (c.prescription_id === cellId) {
            changed = true;
            cells[weekId] = { ...c, ...patch };
          } else {
            cells[weekId] = c;
          }
        }
        return changed ? { ...row, cells } : row;
      }),
    })),
  };
}

function updateMesocycleNameInGrid(
  grid: MesoGrid,
  mesocycleId: Id,
  name: string,
): MesoGrid {
  return {
    ...grid,
    phases: grid.phases?.map((phase) =>
      phase.id === mesocycleId ? { ...phase, name } : phase,
    ),
    mesocycle:
      grid.mesocycle.id === mesocycleId
        ? { ...grid.mesocycle, name }
        : grid.mesocycle,
  };
}

function updateDayNameInGrid(grid: MesoGrid, sessionSlotId: Id, name: string): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) =>
      day.session_slot_id === sessionSlotId ? { ...day, name } : day,
    ),
  };
}

function updateRowInGrid(grid: MesoGrid, exerciseSlotId: Id, patch: Partial<GridRow>): MesoGrid {
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) =>
        row.exercise_slot_id === exerciseSlotId ? { ...row, ...patch } : row,
      ),
    })),
  };
}

/** Highest sub-line number a cell can hold. Mirrors the server's
 * `views.MAX_CELL_LINE` (20); the server is the authority and its answer
 * replaces our optimistic prediction. */
const MAX_CELL_LINE = 20;

/** How long the "Moved below <First>'s line" notice stays up. */
const MOVED_NOTICE_MS = 6000;

export const SAVE_ERROR_MESSAGE =
  "Couldn't save your last change — it's still on screen but not saved. Check your connection and try again.";

/** One coach write to a (slot, week) cell, as queued/in flight. */
export interface CellLineWriteOpts {
  /** "new": the client believed the line blank/absent; "edit": it believed the
   * line held non-blank text of the coach's. Lets the server refuse (never
   * overwrite) when the line changed hands. */
  intent?: "new" | "edit";
  /** The chip: make the line a logged set / a cue. */
  kind?: "set" | "cue";
}

interface CellWrite extends CellLineWriteOpts {
  line: number;
  text: string;
  /** Idempotency token for an `intent:"new"` write; a retry re-sends the SAME
   * token so a write that already landed is not written twice. */
  token?: string;
  /** The line number the optimistic repaint put the text on (differs from
   * `line` when a "new" write was predicted to relocate). */
  placed: number;
}

export interface CellRefusal {
  id: number;
  text: string;
  message: string;
  /** false for `no_free_line`: adding would fail the same way. */
  canAdd: boolean;
}

/** Transient per-cell UI state the table renders under a cell. */
export interface CellUiState {
  /** "moved": informational (clears itself); "error": dismissable, role=alert. */
  notice?: { kind: "moved" | "error"; message: string };
  /** A refused write whose text is held as the ghost line's unsaved draft. */
  /** Refused writes whose text is held for the coach (a LIST: two refusals in
   * one cell never overwrite each other). Only the row's own Add or Discard
   * removes it. */
  refusals?: CellRefusal[];
  /** Line numbers whose last write did not reach the server (retry offered). */
  unsaved?: number[];
}

export function cellUiKey(exerciseSlotId: Id, weekId: Id): string {
  return `${exerciseSlotId}:${weekId}`;
}

/** First whitespace token of the athlete's name (null when there is none). */
function athleteFirstName(grid: MesoGrid | null): string | null {
  const first = grid?.athlete?.name?.trim().split(/\s+/)[0];
  return first ? first : null;
}

/** The line a "new" write lands on: its own number when absent/blank (or
 * already holding this exact text), else the next number above it that is
 * absent/blank — the server's relocation rule, predicted. */
function predictNewLine(lines: CellLine[], line: number, text: string): number {
  const free = (n: number) => {
    const l = lines.find((x) => x.line === n);
    return !l || l.text.trim() === "";
  };
  const own = lines.find((x) => x.line === line);
  if (!own || own.text.trim() === "" || own.text === text) return line;
  for (let n = line + 1; n <= MAX_CELL_LINE; n++) if (free(n)) return n;
  return line;
}

/** Apply one write's optimistic effect to a cell (pure). `asPlaced`: put it
 * on `write.placed` verbatim (re-applying an already-placed unsaved write). */
function applyWriteToCell(cell: GridCell, write: CellWrite, asPlaced = false): GridCell {
  if (write.line === 0) return { ...cell, text: write.text };
  const target = asPlaced
    ? write.placed
    : write.intent === "new"
      ? predictNewLine(cell.lines ?? [], write.line, write.text)
      : write.line;
  const lines = [...(cell.lines ?? [])];
  const flags: Partial<CellLine> =
    write.kind === "set"
      ? { athlete_authored: true, entered_by_coach: true }
      : write.kind === "cue"
        ? { athlete_authored: false, entered_by_coach: false }
        : {};
  const idx = lines.findIndex((l) => l.line === target);
  if (idx >= 0) {
    const cur = lines[idx]!;
    if (cur.text.trim() === "" && write.line !== 0) {
      // A blank line is free: a write onto it makes it a plain cue again
      // (unless this is the chip), whoever held it before.
      const { entered_by_coach: _kept, ...rest } = cur;
      lines[idx] = { ...rest, text: write.text, athlete_authored: false, ...flags };
    } else {
      // A coach write painted onto a line is always a coach line in the view:
      // never keep an athlete-entered line's flags under coach text.
      const athleteEntered = !!cur.athlete_authored && !cur.entered_by_coach;
      lines[idx] = {
        ...cur,
        text: write.text,
        ...(athleteEntered ? { athlete_authored: false } : {}),
        ...flags,
      };
    }
  } else {
    lines.push({ line: target, text: write.text, athlete_authored: false, ...flags });
    lines.sort((a, b) => a.line - b.line);
  }
  return { ...cell, lines };
}

function makeToken(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`.slice(0, 64);
}

/** Lay unsaved (at their placed line) and queued writes back over a cell
 * taken from the server. A queued "new" write is re-predicted against the
 * cell as it now stands, and its `placed` is updated to where it was actually
 * painted, so a later retry mark follows the line the text is shown on. */
function reapplyWrites(
  cell: GridCell,
  unsaved: CellWrite[],
  queued: CellWrite[],
  onRefused: (w: CellWrite) => void = () => {},
): GridCell {
  let next = cell;
  for (const w of unsaved) {
    if (w.line === 0) continue;
    // Never paint an unsaved write over a line someone else now holds
    // (non-blank, different text): a "new" write moves to the next free line;
    // an edit becomes a refusal row (text kept, Add/Discard).
    const occ = (next.lines ?? []).find((l) => l.line === w.placed);
    if (occ && occ.text.trim() !== "" && occ.text !== w.text) {
      if (w.intent === "new") {
        w.placed = predictNewLine(next.lines ?? [], w.line, w.text);
      } else if (occ.athlete_authored && !occ.entered_by_coach) {
        // An edit only loses its line to the ATHLETE (a coach line differing
        // from the coach's own earlier text is just the coach's stack).
        onRefused(w);
        continue;
      }
    }
    next = applyWriteToCell(next, w, true);
  }
  for (const w of queued) {
    if (w.line === 0) continue;
    if (w.intent === "new") w.placed = predictNewLine(next.lines ?? [], w.line, w.text);
    next = applyWriteToCell(next, w);
  }
  return next;
}

function mapCell(
  grid: MesoGrid,
  exerciseSlotId: Id,
  weekId: Id,
  fn: (cell: GridCell) => GridCell,
): MesoGrid {
  const key = String(weekId);
  return {
    ...grid,
    days: grid.days.map((day) => ({
      ...day,
      rows: day.rows.map((row) => {
        if (row.exercise_slot_id !== exerciseSlotId) return row;
        const cell = row.cells[key];
        if (!cell) return row;
        return { ...row, cells: { ...row.cells, [key]: fn(cell) } };
      }),
    })),
  };
}

function findCell(grid: MesoGrid | null, exerciseSlotId: Id, weekId: Id): GridCell | undefined {
  return findRow(grid, exerciseSlotId)?.cells[String(weekId)];
}

export function useGrid(options: UseGridOptions) {
  const { planId, csrf, initialGrid } = options;
  const onActivityRef = useRef(options.onActivity);
  onActivityRef.current = options.onActivity;
  const touch = useCallback(() => onActivityRef.current?.(), []);
  // #709 PR 2 (live sync). `adoptedVRef[cellKey]`: the sync_v of the last write
  // answer adopted for that (slot, week) cell, line writes AND line-0
  // patchCell answers; a fetched grid stamped below it is OLDER than that
  // write and must not revert the cell (#718). `line0InflightRef`/
  // `rowInflightRef`: writes sent but not answered. `appliedVRef`: the stamp
  // of the last fetched grid merged (also the version the poll sends).
  const adoptedVRef = useRef<Map<string, number>>(new Map());
  const line0InflightRef = useRef<Map<string, number>>(new Map());
  const rowInflightRef = useRef<Map<Id, number>>(new Map());
  const appliedVRef = useRef<number | undefined>(
    typeof initialGrid?.sync_v === "number" ? initialGrid.sync_v : undefined,
  );
  const recordAdoptedV = (key: string, v: unknown) => {
    if (typeof v !== "number") return;
    const cur = adoptedVRef.current.get(key);
    if (cur === undefined || v > cur) adoptedVRef.current.set(key, v);
  };
  const bump = <K,>(m: Map<K, number>, k: K, by: 1 | -1) => {
    const n = (m.get(k) ?? 0) + by;
    if (n <= 0) m.delete(k);
    else m.set(k, n);
  };
  const [grid, setGridState] = useState<MesoGrid | null>(initialGrid);
  // The latest grid, updated synchronously with every set — writeCellLine's
  // per-cell queue predicts and re-applies against it between renders.
  const gridRef = useRef<MesoGrid | null>(initialGrid);
  const setGrid = useCallback(
    (update: MesoGrid | null | ((prev: MesoGrid | null) => MesoGrid | null)) => {
      const next = typeof update === "function" ? update(gridRef.current) : update;
      gridRef.current = next;
      setGridState(next);
    },
    [],
  );

  // Last unsaved-change failure, shown by DesignerRoot as a banner. Every
  // optimistic verb sets it (alongside its console.error) so a failed write is
  // never silent.
  const [saveError, setSaveError] = useState<string | null>(null);
  const reportFailure = useCallback((label: string, err: unknown) => {
    console.error(label, err);
    setSaveError(SAVE_ERROR_MESSAGE);
  }, []);
  const dismissSaveError = useCallback(() => setSaveError(null), []);

  // Per-cell UI (notices, refusal drafts, retry marks), keyed by cellUiKey.
  const [cellUi, setCellUi] = useState<Record<string, CellUiState>>({});
  const patchCellUi = useCallback((key: string, patch: (cur: CellUiState) => CellUiState) => {
    setCellUi((prev) => {
      const next = patch(prev[key] ?? {});
      const out = { ...prev };
      if (!next.notice && !(next.refusals && next.refusals.length) && !(next.unsaved && next.unsaved.length)) delete out[key];
      else out[key] = next;
      return out;
    });
  }, []);
  const noticeTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  useEffect(() => {
    const timers = noticeTimersRef.current;
    return () => {
      timers.forEach((t) => clearTimeout(t));
      timers.clear();
    };
  }, []);
  // Per-cell write queue: writes to one (slot, week) are chained so their
  // responses arrive in send order; `queuedRef` holds the not-yet-answered
  // ones (re-applied on top of each adopted server answer), `unsavedRef` the
  // ones that failed to reach the server.
  const chainsRef = useRef<Map<string, Promise<void>>>(new Map());
  const queuedRef = useRef<Map<string, CellWrite[]>>(new Map());
  const unsavedRef = useRef<Map<string, CellWrite[]>>(new Map());

  const refusalIdRef = useRef(0);
  const addRefusal = useCallback(
    (key: string, r: Omit<CellRefusal, "id">) => {
      if (r.text.trim() === "") return; // nothing to keep
      const id = ++refusalIdRef.current;
      patchCellUi(key, (cur) => ({ ...cur, refusals: [...(cur.refusals ?? []), { ...r, id }] }));
    },
    [patchCellUi],
  );

  // After re-applying unsaved writes over server lines: edits whose line was
  // taken become refusal rows (text kept, Add/Discard) and leave `unsavedRef`;
  // the retry marks are re-derived from where each unsaved write is painted.
  const settleUnsaved = (key: string, refused: CellWrite[]) => {
    let list = unsavedRef.current.get(key) ?? [];
    if (refused.length) {
      list = list.filter((w) => !refused.includes(w));
      unsavedRef.current.set(key, list);
      const first = athleteFirstName(gridRef.current) ?? "Your athlete";
      for (const w of refused) {
        addRefusal(key, {
          text: w.text,
          message: `${first} logged on this line — your unsaved text is below it.`,
          canAdd: true,
        });
      }
    }
    patchCellUi(key, (cur) => ({ ...cur, unsaved: list.map((w) => w.placed) }));
  };
  const [history, setHistory] = useState<GridHistory>(initialGrid?.history ?? EMPTY_GRID_HISTORY);

  // One shared in-flight guard across every structural (refetch-driven) verb
  // — mirrors useDeletes' deletingRef / useReorder's reorderingRef, checked
  // synchronously so a double-click can't race two refetches.
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);

  // In-flight cell-autosave POSTs (patchCell/renameExercise are fire-and-
  // forget). fillAcrossWeeks reads the source cell's already-stored DB values
  // server-side, so it must flush these first or it can copy stale data to
  // sibling weeks when a coach edits then immediately fills (Codex P2).
  const pendingWritesRef = useRef<Set<Promise<unknown>>>(new Set());

  const adoptGridHistory = useCallback((data: GridHistoryCarrier) => {
    const h = data?.history;
    if (!h) return;
    setHistory({
      can_undo: h.can_undo,
      can_redo: h.can_redo,
      undo_label: h.undo_label ?? "",
      redo_label: h.redo_label ?? "",
    });
  }, []);

  const flushPendingWrites = useCallback(async () => {
    await Promise.allSettled([...pendingWritesRef.current]);
  }, []);

  // Merge a fetched grid (a structural refetch or a live-sync poll) into local
  // state. Rules, in order:
  //  1. A grid stamped BELOW the last one merged is older than what is on
  //     screen: ignored whole. (A stamp-less grid, an old server, is taken.)
  //  2. Structure (weeks, days, rows, plan, athlete, phases, history) comes
  //     from the fetched grid.
  //  3. Per cell: if a write answer already adopted for that cell is newer
  //     than the fetched stamp, the LOCAL cell stays (#718).
  //  4. Otherwise the fetched cell is taken, except its line-0 text when a
  //     line-0 write is in flight, then queued/unsaved line writes are laid
  //     back over it.
  //     That suppression does NOT advance the applied stamp (see below).
  //  5. A row with an unanswered columns/rename write keeps its local
  //     name/tempo/rest/note.
  // Returns whether anything the coach can see changed.
  const applyRemoteGrid = useCallback(
    (fetchedGrid: MesoGrid): boolean => {
      const v = typeof fetchedGrid.sync_v === "number" ? fetchedGrid.sync_v : undefined;
      if (v !== undefined && appliedVRef.current !== undefined && v < appliedVRef.current) return false;
      const gv = v ?? Number.POSITIVE_INFINITY;
      const prev = gridRef.current;
      const settleLater: Array<[string, CellWrite[]]> = [];
      // True when a fetched line-0 text was dropped for a local in-flight one.
      let suppressed = false;
      // plan/athlete/phases must ride every merge, not just the initial
      // hydration (issue #455 phase A5: the top bar / left rail / block view
      // have no other source).
      const merged: MesoGrid = {
        plan: fetchedGrid.plan,
        athlete: fetchedGrid.athlete,
        phases: fetchedGrid.phases,
        mesocycle: fetchedGrid.mesocycle,
        weeks: fetchedGrid.weeks,
        days: fetchedGrid.days.map((day) => ({
          ...day,
          rows: day.rows.map((fetchedRow) => {
            const localRow = findRow(prev, fetchedRow.exercise_slot_id);
            let rowOut: GridRow = fetchedRow;
            if (localRow && (rowInflightRef.current.get(fetchedRow.exercise_slot_id) ?? 0) > 0) {
              rowOut = {
                ...fetchedRow,
                name: localRow.name,
                exercise_id: localRow.exercise_id,
                tempo: localRow.tempo,
                rest: localRow.rest,
                note: localRow.note,
              };
            }
            let cells = rowOut.cells;
            for (const [weekId, c] of Object.entries(fetchedRow.cells)) {
              const k = cellUiKey(fetchedRow.exercise_slot_id, weekId);
              const local = prev ? findCell(prev, fetchedRow.exercise_slot_id, weekId) : undefined;
              let next: GridCell = c;
              const adopted = adoptedVRef.current.get(k);
              if (local && adopted !== undefined && adopted > gv) {
                next = local;
              } else {
                if (local && (line0InflightRef.current.get(k) ?? 0) > 0) {
                  if (local.text !== next.text) suppressed = true;
                  next = { ...next, text: local.text };
                }
                const unsaved = unsavedRef.current.get(k) ?? [];
                const queued = queuedRef.current.get(k) ?? [];
                if (unsaved.length || queued.length) {
                  const refused: CellWrite[] = [];
                  next = reapplyWrites(next, unsaved, queued, (w) => refused.push(w));
                  if (unsaved.length) settleLater.push([k, refused]);
                }
              }
              if (next !== c) {
                if (cells === rowOut.cells) cells = { ...rowOut.cells };
                cells[weekId] = next;
              }
            }
            return cells === rowOut.cells ? rowOut : { ...rowOut, cells };
          }),
        })),
        history: fetchedGrid.history,
        sync_v: v !== undefined && !suppressed ? v : prev?.sync_v ?? v,
      };
      // Content was held back (a fetched line-0 text lost to an in-flight
      // write): don't claim this stamp, so the next poll refetches and
      // reconciles once nothing is in flight.
      if (v !== undefined && !suppressed) appliedVRef.current = v;
      const changed =
        !prev || stableStringify({ ...prev, sync_v: 0 }) !== stableStringify({ ...merged, sync_v: 0 });
      if (changed) setGrid(merged);
      for (const [k, refused] of settleLater) settleUnsaved(k, refused);
      if (fetchedGrid.history) setHistory(fetchedGrid.history);
      return changed;
    },
    [setGrid],
  );

  const getSyncV = useCallback(() => appliedVRef.current, []);

  const refetchGrid = useCallback(async () => {
    try {
      // The block the coach has open, not the plan's default one (else a
      // refetch and the next changed poll would alternate between two blocks).
      const openBlock = gridRef.current?.mesocycle?.id;
      const q = openBlock != null ? `?mesocycle=${openBlock}` : "";
      const res = await fetch(`/meso/api/plan/${planId}/grid/${q}`);
      if (!res.ok) throw new Error("Request failed: " + res.status);
      const data = (await res.json()) as MesoGrid & { ok?: boolean };
      applyRemoteGrid(data);
    } catch (err) {
      reportFailure("Refetch grid failed", err);
    }
  }, [planId, applyRemoteGrid, reportFailure]);

  const runStructural = useCallback(async (fn: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    touch();
    try {
      // A just-blurred line write must land before the refetch replaces the grid.
      if (pendingWritesRef.current.size) await flushPendingWrites();
      await fn();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [flushPendingWrites, touch]);

  const patchCell = useCallback(
    (cellId: Id, patch: GridCellPatch) => {
      touch();
      // Line 0 is in flight until answered; its answer's stamp guards the cell.
      const cellKey = cellKeyOfPrescription(gridRef.current, cellId);
      if (cellKey) bump(line0InflightRef.current, cellKey, 1);
      setGrid((prev) => (prev ? updateCellInGrid(prev, cellId, patch) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, patch, csrf)
        .then((data) => {
          if (cellKey) recordAdoptedV(cellKey, (data as { sync_v?: unknown } | null)?.sync_v);
          adoptGridHistory(data as GridHistoryCarrier);
        })
        .catch((err) => reportFailure("Cell autosave failed", err))
        .finally(() => {
          if (cellKey) bump(line0InflightRef.current, cellKey, -1);
        });
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory, touch],
  );

  const renameExercise = useCallback(
    // `exerciseId` (#608, a suggestion pick): undefined = key absent = a plain
    // typed rename, where the server unlinks a CHANGED name — mirrored here;
    // a string/null is sent explicitly and sets (or clears) the link.
    (exerciseSlotId: Id, name: string, exerciseId?: string | null) => {
      const row = findRow(grid, exerciseSlotId);
      const cellId = rowIdentityCellId(grid?.weeks ?? [], row);
      if (cellId == null) return;
      const patch: Partial<GridRow> = { name };
      if (exerciseId !== undefined) patch.exercise_id = exerciseId;
      else if (row && row.name !== name) patch.exercise_id = null;
      const body = exerciseId !== undefined ? { name, exercise_id: exerciseId } : { name };
      touch();
      bump(rowInflightRef.current, exerciseSlotId, 1);
      setGrid((prev) => (prev ? updateRowInGrid(prev, exerciseSlotId, patch) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/`, body, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => reportFailure("Rename exercise failed", err))
        .finally(() => bump(rowInflightRef.current, exerciseSlotId, -1));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [grid, planId, csrf, adoptGridHistory, touch],
  );

  const renamePlan = useCallback(
    (title: string) => {
      setGrid((prev) =>
        prev?.plan ? { ...prev, plan: { ...prev.plan, title } } : prev,
      );
      const write = apiPost(`/meso/api/plan/${planId}/title/`, { title }, csrf)
        .then((data) => {
          const reply = data as PlanTitleCarrier;
          if (typeof reply.plan?.title === "string") {
            const returnedTitle = reply.plan.title;
            setGrid((prev) =>
              prev?.plan
                ? { ...prev, plan: { ...prev.plan, title: returnedTitle } }
                : prev,
            );
          }
          adoptGridHistory(reply);
        })
        .catch((err) => reportFailure("Rename program failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory],
  );

  const renameMesocycle = useCallback(
    (mesocycleId: Id, name: string) => {
      setGrid((prev) =>
        prev ? updateMesocycleNameInGrid(prev, mesocycleId, name) : prev,
      );
      const write = apiPost(
        `/meso/api/plan/${planId}/mesocycle/${mesocycleId}/name/`,
        { name },
        csrf,
      )
        .then((data) => {
          const reply = data as MesocycleNameCarrier;
          if (
            reply.mesocycle?.id != null &&
            typeof reply.mesocycle.name === "string"
          ) {
            const returnedId = reply.mesocycle.id;
            const returnedName = reply.mesocycle.name;
            setGrid((prev) =>
              prev
                ? updateMesocycleNameInGrid(prev, returnedId, returnedName)
                : prev,
            );
          }
          adoptGridHistory(reply);
        })
        .catch((err) => reportFailure("Rename block failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory],
  );

  const renameDay = useCallback(
    (sessionSlotId: Id, name: string) => {
      setGrid((prev) =>
        prev ? updateDayNameInGrid(prev, sessionSlotId, name) : prev,
      );
      const write = apiPost(
        `/meso/api/plan/${planId}/day/${sessionSlotId}/name/`,
        { name },
        csrf,
      )
        .then((data) => {
          const reply = data as DayNameCarrier;
          if (
            reply.day?.session_slot_id != null &&
            typeof reply.day.name === "string"
          ) {
            const returnedId = reply.day.session_slot_id;
            const returnedName = reply.day.name;
            setGrid((prev) =>
              prev ? updateDayNameInGrid(prev, returnedId, returnedName) : prev,
            );
          }
          adoptGridHistory(reply);
        })
        .catch((err) => reportFailure("Rename day failed", err));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory],
  );

  // Phase 2a/#709: write one (week × line) sub-line of a row's stack —
  // addressed by (exercise_slot, week, line), not pk, since a sub-line cell
  // may not exist yet (the server upserts via `cell_line_write`).
  //
  // Optimistic (local repaint now), but unlike the other verbs the server's
  // ANSWER matters: it can relocate a "new" write, refuse one onto the
  // athlete's line, or flip a line's kind. So writes to the SAME (slot, week)
  // are chained (responses arrive in send order), every 200/422 carries the
  // cell's whole `grid_cell` which replaces our lines/summary (re-applying
  // the writes still queued behind it), and a failed write keeps its text on
  // screen with a "Not saved — retry" mark instead of vanishing.
  const sendCellWrite = useCallback(
    async (exerciseSlotId: Id, weekId: Id, write: CellWrite) => {
      const key = cellUiKey(exerciseSlotId, weekId);
      const dropQueued = () => {
        const q = queuedRef.current.get(key) ?? [];
        queuedRef.current.set(
          key,
          q.filter((w) => w !== write),
        );
      };
      const markUnsaved = (err: unknown, label: string) => {
        dropQueued();
        const list = (unsavedRef.current.get(key) ?? []).filter((w) => w.placed !== write.placed);
        list.push(write);
        unsavedRef.current.set(key, list);
        patchCellUi(key, (cur) => ({ ...cur, unsaved: list.map((w) => w.placed) }));
        reportFailure(label, err);
      };
      const adopt = (gc: Partial<GridCell> | undefined) => {
        if (!gc || !Array.isArray(gc.lines)) return;
        const unsaved = unsavedRef.current.get(key) ?? [];
        const queued = queuedRef.current.get(key) ?? [];
        const refused: CellWrite[] = [];
        setGrid((prev) =>
          prev
            ? mapCell(prev, exerciseSlotId, weekId, (cell) => {
                // Never the server's line-0 `text` — patchCell owns it.
                let next: GridCell = {
                  ...cell,
                  lines: gc.lines!,
                  athlete_summary: "athlete_summary" in gc ? gc.athlete_summary : cell.athlete_summary,
                  session_started: "session_started" in gc ? gc.session_started : cell.session_started,
                };
                return reapplyWrites(next, unsaved, queued, (w) => refused.push(w));
              })
            : prev,
        );
        settleUnsaved(key, refused);
      };
      const body: Record<string, unknown> = { week_id: weekId, line: write.line, text: write.text };
      if (write.intent) body.intent = write.intent;
      if (write.kind) body.kind = write.kind;
      if (write.token) body.token = write.token;

      let result: { ok: boolean; status: number; data: unknown };
      try {
        result = await apiPostResult(`/meso/api/plan/${planId}/row/${exerciseSlotId}/cell/`, body, csrf);
      } catch (err) {
        markUnsaved(err, "Cell line write failed");
        return;
      }
      const data =
        result.data && typeof result.data === "object" ? (result.data as Record<string, any>) : null;

      if ((result.ok || result.status === 422) && data) recordAdoptedV(key, data.sync_v);

      if (result.ok && data) {
        dropQueued();
        // This line reached the server: any earlier unsaved mark on it is moot.
        const rest = (unsavedRef.current.get(key) ?? []).filter((w) => w.placed !== write.placed);
        unsavedRef.current.set(key, rest);
        adopt(data.grid_cell);
        adoptGridHistory(data as GridHistoryCarrier);
        patchCellUi(key, (cur) => {
          let next: CellUiState = {
            ...cur,
            unsaved: (unsavedRef.current.get(key) ?? rest).map((w) => w.placed),
          };
          if (data.relocated_from != null) {
            const first = athleteFirstName(gridRef.current);
            next = {
              ...next,
              notice: { kind: "moved", message: `Moved below ${first ?? "your athlete"}'s line` },
            };
          }
          return next;
        });
        if (data.relocated_from != null) {
          const timers = noticeTimersRef.current;
          const prevTimer = timers.get(key);
          if (prevTimer) clearTimeout(prevTimer);
          timers.set(
            key,
            setTimeout(() => {
              timers.delete(key);
              patchCellUi(key, (cur) =>
                cur.notice?.kind === "moved" ? { ...cur, notice: undefined } : cur,
              );
            }, MOVED_NOTICE_MS),
          );
        }
        return;
      }

      if (result.status === 422 && data) {
        dropQueued();
        adopt(data.grid_cell);
        const code = typeof data.code === "string" ? data.code : "";
        const serverError = typeof data.error === "string" && data.error ? data.error : "That line couldn't be saved.";
        if (code === "athlete_line") {
          const first =
            (typeof data.athlete_first_name === "string" && data.athlete_first_name) ||
            athleteFirstName(gridRef.current) ||
            "Your athlete";
          addRefusal(key, {
            text: write.text,
            message: `${first} just logged on this line — your text is below it.`,
            canAdd: true,
          });
        } else if (code === "no_free_line") {
          addRefusal(key, { text: write.text, message: serverError, canAdd: false });
        } else {
          const message = write.text.trim() ? `${serverError} (“${write.text}”)` : serverError;
          patchCellUi(key, (cur) => ({ ...cur, notice: { kind: "error", message } }));
        }
        return;
      }

      // 5xx, another non-ok status, or a 200 whose body is unreadable.
      markUnsaved(new Error("Request failed: " + result.status), "Cell line write failed");
    },
    [planId, csrf, adoptGridHistory, patchCellUi, addRefusal, reportFailure, setGrid],
  );

  const enqueueCellWrite = useCallback(
    (exerciseSlotId: Id, weekId: Id, write: CellWrite) => {
      const key = cellUiKey(exerciseSlotId, weekId);
      queuedRef.current.set(key, [...(queuedRef.current.get(key) ?? []), write]);
      // The first write for a cell starts synchronously; later ones wait on it.
      const prev = chainsRef.current.get(key);
      const run = prev
        ? prev.then(() => sendCellWrite(exerciseSlotId, weekId, write))
        : sendCellWrite(exerciseSlotId, weekId, write);
      chainsRef.current.set(key, run);
      pendingWritesRef.current.add(run);
      run.finally(() => {
        pendingWritesRef.current.delete(run);
        if (chainsRef.current.get(key) === run) chainsRef.current.delete(key);
      });
    },
    [sendCellWrite],
  );

  const writeCellLine = useCallback(
    (exerciseSlotId: Id, weekId: Id, line: number, text: string, opts?: CellLineWriteOpts) => {
      const key = cellUiKey(exerciseSlotId, weekId);
      const cellNow = findCell(gridRef.current, exerciseSlotId, weekId);
      const placed =
        line !== 0 && opts?.intent === "new" && cellNow
          ? predictNewLine(cellNow.lines ?? [], line, text)
          : line;
      touch();
      const write: CellWrite = { line, text, placed, ...opts };
      if (opts?.intent === "new" && line !== 0) write.token = makeToken();
      setGrid((prev) =>
        prev ? mapCell(prev, exerciseSlotId, weekId, (cell) => applyWriteToCell(cell, write)) : prev,
      );
      // A fresh write supersedes a retry mark on its line. It never clears a
      // refusal: only that refusal's own Add or Discard does.
      const unsaved = (unsavedRef.current.get(key) ?? []).filter((w) => w.placed !== placed);
      unsavedRef.current.set(key, unsaved);
      patchCellUi(key, (cur) => ({
        ...cur,
        unsaved: unsaved.map((w) => w.placed),
      }));
      enqueueCellWrite(exerciseSlotId, weekId, write);
    },
    [enqueueCellWrite, patchCellUi, setGrid, touch],
  );

  /** Re-send a line whose write never reached the server (same write,
   * no new optimistic repaint — its text is still on screen). */
  const retryCellLine = useCallback(
    (exerciseSlotId: Id, weekId: Id, line: number) => {
      const key = cellUiKey(exerciseSlotId, weekId);
      const list = unsavedRef.current.get(key) ?? [];
      const write = list.find((w) => w.placed === line);
      if (!write) return;
      touch();
      const rest = list.filter((w) => w !== write);
      unsavedRef.current.set(key, rest);
      patchCellUi(key, (cur) => ({ ...cur, unsaved: rest.map((w) => w.placed) }));
      enqueueCellWrite(exerciseSlotId, weekId, write);
    },
    [enqueueCellWrite, patchCellUi, touch],
  );

  const dismissCellNotice = useCallback(
    (exerciseSlotId: Id, weekId: Id) => {
      patchCellUi(cellUiKey(exerciseSlotId, weekId), (cur) => ({ ...cur, notice: undefined }));
    },
    [patchCellUi],
  );

  /** Remove one refusal (its own Add or Discard). */
  const discardRefusal = useCallback(
    (exerciseSlotId: Id, weekId: Id, refusalId: number) => {
      patchCellUi(cellUiKey(exerciseSlotId, weekId), (cur) => ({
        ...cur,
        refusals: (cur.refusals ?? []).filter((r) => r.id !== refusalId),
      }));
    },
    [patchCellUi],
  );

  // Phase 2a (D2): the per-exercise Tempo/Rest/instructions columns — row
  // attributes on the block-shared ExerciseSlot, written through
  // `exercise_slot_patch`. Same optimistic fire-and-forget shape as patchCell.
  const patchRowColumns = useCallback(
    (exerciseSlotId: Id, patch: GridRowPatch) => {
      touch();
      bump(rowInflightRef.current, exerciseSlotId, 1);
      setGrid((prev) => (prev ? updateRowInGrid(prev, exerciseSlotId, patch) : prev));
      const write = apiPost(`/meso/api/plan/${planId}/row/${exerciseSlotId}/`, patch, csrf)
        .then((data) => adoptGridHistory(data as GridHistoryCarrier))
        .catch((err) => reportFailure("Row columns autosave failed", err))
        .finally(() => bump(rowInflightRef.current, exerciseSlotId, -1));
      pendingWritesRef.current.add(write);
      write.finally(() => pendingWritesRef.current.delete(write));
    },
    [planId, csrf, adoptGridHistory, touch],
  );

  const addExercise = useCallback(
    (day: GridDay) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${day.session_id}/exercise/`, null, csrf);
        } catch (err) {
          reportFailure("Add exercise failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const removeExercise = useCallback(
    (exerciseSlotId: Id) =>
      runStructural(async () => {
        const row = findRow(grid, exerciseSlotId);
        const cellId = firstWeekCellId(grid, row);
        if (cellId == null) return;
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/delete/`, null, csrf);
        } catch (err) {
          reportFailure("Remove exercise failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, planId, csrf, runStructural, refetchGrid],
  );

  const addDay = useCallback(
    () =>
      runStructural(async () => {
        const weekId = currentWeekId(grid);
        // No live week in the block we're viewing means there is nothing to hang
        // a day on. Posting anyway would send `{week_id: undefined}` — JSON drops
        // the key, and the server's own fallback would create the day in whatever
        // block DOES have a live week, i.e. not the one on screen. Add a week
        // first (that path is block-scoped).
        if (weekId == null) return;
        try {
          await apiPost(`/meso/api/plan/${planId}/session/`, { week_id: weekId }, csrf);
        } catch (err) {
          reportFailure("Add day failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, planId, csrf, runStructural, refetchGrid],
  );

  const removeDay = useCallback(
    (day: GridDay) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${day.session_id}/delete/`, null, csrf);
        } catch (err) {
          reportFailure("Remove day failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const addWeek = useCallback(
    () =>
      runStructural(async () => {
        // Post the block we're VIEWING. Without it the server falls back to the
        // plan's first block, which is the same block the grid opens on today —
        // but only by coincidence, and it silently diverged before (an empty
        // first block sent the new week to a later one, where this grid would
        // never show it).
        const mesocycleId = grid?.mesocycle?.id;
        try {
          await apiPost(
            `/meso/api/plan/${planId}/week/`,
            mesocycleId != null ? { mesocycle_id: mesocycleId } : null,
            csrf,
          );
        } catch (err) {
          reportFailure("Add week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, planId, csrf, runStructural, refetchGrid],
  );

  const removeWeek = useCallback(
    (weekId: Id) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/week/${weekId}/delete/`, null, csrf);
        } catch (err) {
          reportFailure("Remove week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  // Issue #455 phase A2 (drag reordering): same STRUCTURAL shape as every
  // verb above — the server owns the authoritative order (block-wide P0
  // ExerciseSlot/SessionSlot.order), so these await their POST then
  // refetch the whole grid, sharing busyRef. useTableReorder (the pure
  // drag-event translator) builds `order` from the CURRENT week's live
  // cell/session ids and calls these two verbs — see its own header for the
  // payload contract (mirrors views.py session_reorder/week_reorder_sessions
  // exactly: `order` must be EXACTLY the live id set for the target session/
  // week, in the new order).

  const reorderExercises = useCallback(
    (sessionId: Id, order: number[]) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${sessionId}/reorder/`, { order }, csrf);
        } catch (err) {
          reportFailure("Reorder exercises failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const reorderDays = useCallback(
    (weekId: Id, order: number[]) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/week/${weekId}/reorder/`, { order }, csrf);
        } catch (err) {
          reportFailure("Reorder days failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  // Issue #455 phase A2.5 (menu-based cross-day move): closes the parity gap
  // A2's drag scope deliberately left out (separate <table> containers +
  // sticky columns = high dnd-kit risk — see useTableReorder.ts's header).
  // --- P2 exceptions: skip / fill / add-this-week -------------------------
  // Same STRUCTURAL shape as add/removeExercise|Day|Week above — the grid
  // (not just one cell) can change shape/content in ways only the server
  // knows (fill rewrites whole stacks, add-this-week creates a new
  // slot+cells) so these await their POST then refetch, sharing busyRef.
  // (The one-week swap verb is gone — Phase 2a: a substitution is sub-line
  // text, written through writeCellLine above.)

  const skipCell = useCallback(
    (cellId: number, skipped: boolean) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/skip/`, { skipped }, csrf);
        } catch (err) {
          reportFailure("Skip cell failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const fillAcrossWeeks = useCallback(
    (cellId: number) =>
      runStructural(async () => {
        // Flush any in-flight cell autosave first — fill copies the source
        // cell's ALREADY-STORED DB values server-side, so a just-edited cell
        // must finish committing or the fill can copy stale data (Codex P2).
        await flushPendingWrites();
        try {
          await apiPost(`/meso/api/plan/${planId}/prescription/${cellId}/fill/`, {}, csrf);
        } catch (err) {
          reportFailure("Fill across weeks failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid, flushPendingWrites],
  );

  const addExerciseThisWeek = useCallback(
    (day: GridDay, weekId: number) =>
      runStructural(async () => {
        try {
          await apiPost(`/meso/api/plan/${planId}/session/${day.session_id}/exercise/`, { week_id: weekId }, csrf);
        } catch (err) {
          reportFailure("Add exercise this week failed", err);
          return;
        }
        await refetchGrid();
      }),
    [planId, csrf, runStructural, refetchGrid],
  );

  const undo = useCallback(
    () =>
      runStructural(async () => {
        if (!history.can_undo) return;
        const weekId = currentWeekId(grid);
        try {
          await apiPost(`/meso/api/plan/${planId}/undo/`, { week_id: weekId }, csrf);
        } catch (err) {
          reportFailure("Undo failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, history.can_undo, planId, csrf, runStructural, refetchGrid],
  );

  const redo = useCallback(
    () =>
      runStructural(async () => {
        if (!history.can_redo) return;
        const weekId = currentWeekId(grid);
        try {
          await apiPost(`/meso/api/plan/${planId}/redo/`, { week_id: weekId }, csrf);
        } catch (err) {
          reportFailure("Redo failed", err);
          return;
        }
        await refetchGrid();
      }),
    [grid, history.can_redo, planId, csrf, runStructural, refetchGrid],
  );

  return {
    grid,
    history,
    busy,
    patchCell,
    renamePlan,
    renameMesocycle,
    renameDay,
    renameExercise,
    writeCellLine,
    retryCellLine,
    dismissCellNotice,
    discardRefusal,
    cellUi,
    saveError,
    dismissSaveError,
    patchRowColumns,
    addExercise,
    removeExercise,
    addDay,
    removeDay,
    addWeek,
    removeWeek,
    reorderExercises,
    reorderDays,
    skipCell,
    fillAcrossWeeks,
    addExerciseThisWeek,
    undo,
    redo,
    refetchGrid,
    applyRemoteGrid,
    getSyncV,
  };
}

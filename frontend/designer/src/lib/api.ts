// Server contract + fetch wrapper for the Meso designer (ported from
// createMeso() in app/store_project/static/js/meso.js — see
// frontend/designer/CONTRACT.md and scratchpad phase2-inventory.md "Server
// contract" section). No reactive state here: apiPost takes the CSRF token
// as an argument instead of reading `this.csrf`.

/** Undo/redo availability, adopted from every payload that carries one. */
export interface HistoryState {
  can_undo: boolean;
  can_redo: boolean;
  undo_label: string | null;
  redo_label: string | null;
}

export const EMPTY_HISTORY: HistoryState = {
  can_undo: false,
  can_redo: false,
  undo_label: null,
  redo_label: null,
};

/** One freeform sub-line of a cell's stack (Phase 2a text-first). */
export interface CellLine {
  /** Present on the multi-week grid payload (patch-by-pk); absent on the
   * athlete-facing single-week shape. */
  id?: number;
  line: number;
  text: string;
}

/** One exercise row (a Prescription) as the grid renders/edits it.
 *
 * Phase 2a (spreadsheet parity): the structured sets/reps/load/rpe fields are
 * gone — a cell is one freeform `text` string plus optional sub-`lines`;
 * `tempo`/`rest`/`note` are per-exercise row columns off the slot. */
export interface Exercise {
  id: number | string;
  name: string;
  text: string;
  lines?: CellLine[];
  tempo?: string;
  rest?: string;
  note?: string;
  tag?: string;
  last?: string;
  /** P2 one-week exception: this week's Prescription was skipped (not
   * trained). Serialized by `serialize_prescription`/`serialize_session`
   * (serializers.py) on the single-week path — mirrors `GridCell.skipped`
   * on the P1 table path. Optional/falsy by default so existing fixtures
   * (which never set it) keep rendering the normal editable row. */
  skipped?: boolean;
}

/** One training day (a Session) and its exercises. */
export interface Day {
  id: number | string;
  n: number;
  name: string;
  bias?: string;
  exercises: Exercise[];
}

/** One week strip entry (a mesocycle Week). */
export interface Week {
  id: number | string;
  index?: number;
  label: string;
  current: boolean;
  phase?: string;
  deload?: boolean;
  /** Block-view timeline bars (0-100). */
  vol?: number;
  inten?: number;
}

/** One macrocycle phase chip. */
export interface Phase {
  name: string;
  weeks: string;
  state: "done" | "current" | "next" | "future" | string;
}

/** Individual athlete identity for the left rail (serialize_athlete_identity). */
export interface AthleteIdentity {
  name: string;
  initials: string;
  goal?: string;
  contraindications: { text: string }[];
}

export interface PlanSummary {
  id: number | string;
  title?: string;
  goal?: string;
  status?: string;
  unit?: string;
}

/**
 * The full re-serialize payload every ✓-marked endpoint returns (serialize_plan)
 * and applyPlanData adopts wholesale: plan/athlete/program/weeks/viewing/
 * phases/history all move together (inventory "Server contract").
 */
export interface PlanEnvelope {
  ok?: boolean;
  plan?: PlanSummary;
  athlete?: AthleteIdentity | null;
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewing: number | string | null;
  history?: HistoryState;
}

/** A row-merge (○) reply: carries just the changed slice + a fresh `history`. */
export interface HistoryCarrier {
  history?: HistoryState;
}

/**
 * P1 multi-week table grid (`serialize_mesocycle_grid`, backend
 * `app/store_project/meso/serializers.py`) — the whole block at once: one row
 * per live ExerciseSlot, one column per live Week, keyed by `str(week_id)`.
 * Distinct from `PlanEnvelope`'s single-week `program` (see CONTRACT.md's
 * useGrid section) — this is P1's own data shape, fetched/POSTed independently.
 */
export interface GridWeek {
  id: number;
  index: number;
  label: string;
  phase: string;
  deload: boolean;
  current: boolean;
  delivered_at: string | null;
  /** Block-view timeline bars (0-100) — issue #455 phase A5 (serialize_
   * mesocycle_grid additions; mirrors Week.vol/Week.inten above). Optional
   * (like Week's) to minimize churn in the many existing GridWeek test
   * fixtures across this tree that predate this phase and don't set them —
   * BlockView's barH already treats a missing value as 0 (`w.vol ?? 0`). The
   * real server payload always includes both. */
  vol?: number;
  inten?: number;
}

export interface GridCell {
  prescription_id: number;
  /** The freeform prescription text, verbatim (Phase 2a text-first). */
  text: string;
  skipped: boolean;
  /** The row's freeform sub-line stack for this week (line >= 1), blank
   * lines included so the editor can show a cleared line in place. */
  lines: CellLine[];
}

export interface GridRow {
  exercise_slot_id: number;
  /** The block-shared row identity (a substitution is sub-line text now). */
  name: string;
  exercise_id: number | null;
  order: number;
  tags: unknown[];
  /** Per-exercise columns (Phase 2a, D2): Tempo / Rest / instructions. */
  tempo: string;
  rest: string;
  note: string;
  /** One cell per live week, keyed by `String(week.id)`. */
  cells: Record<string, GridCell>;
}

export interface GridDay {
  session_slot_id: number;
  /** The session pk the P1 table DISPLAYS for this day column — prefers the
   * current week, but FALLS BACK to an earlier live week's session
   * (`_pick_session_id`, serializers.py) when the current week's was
   * independently soft-deleted. Display-only: never use this for a day
   * REORDER post (see `session_ids` below and useTableReorder.ts's header —
   * Codex #455 A2 review finding 2). */
  session_id: number | null;
  /** Per-week session pks for this day, keyed by `String(week.id)` —
   * covers only the live weeks that have a LIVE session for this day (a
   * week missing one has no entry). The source of truth for day-reorder
   * payloads: always read `session_ids[String(currentWeekId)]`, never the
   * (possibly-fallback) `session_id` above. */
  session_ids: Record<string, number>;
  day_number: number;
  name: string;
  bias: string;
  order: number;
  rows: GridRow[];
}

/** `serialize_plan_history`'s shape as ridden by the grid endpoints — labels
 * are always strings (never null), unlike `HistoryState`. */
export interface GridHistory {
  can_undo: boolean;
  can_redo: boolean;
  undo_label: string;
  redo_label: string;
}

export interface MesoGrid {
  /** Issue #455 phase A5: the grid's own plan/athlete/phases — the
   * front-end's ONLY source for these now that the one-week `plan_data`
   * hydration path is retired. Optional (rather than required) to minimize
   * churn in useGrid.test.ts's `Partial<MesoGrid>` fixtures that predate
   * this phase and don't set them. */
  plan?: PlanSummary;
  athlete?: AthleteIdentity | null;
  phases?: Phase[];
  mesocycle: { id: number; plan_id: number; name: string; week_count: number };
  weeks: GridWeek[];
  days: GridDay[];
  history: GridHistory;
}

/**
 * POST JSON to `url` with the CSRF header and same-origin credentials
 * (fetch's default), throwing on a non-ok response. `csrf` is an explicit
 * argument (the Alpine original read `this.csrf`); callers supply it from
 * the `#meso-csrf` hydration value.
 */
export async function apiPost<T = unknown>(
  url: string,
  body: unknown,
  csrf: string,
): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrf,
    },
    body: body == null ? null : JSON.stringify(body),
  });
  if (!res.ok) throw new Error("Request failed: " + res.status);
  return res.json();
}

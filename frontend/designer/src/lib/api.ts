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

/** One member's stored per-athlete diff on a shared (group) row. */
export interface OverrideAdjust {
  id: string;
  name?: string;
  initials?: string;
  label?: string;
  swap: string;
  load_pct: number | null;
  sets: string;
  reps: string;
  note: string;
}

/** One exercise row (a Prescription) as the grid renders/edits it. */
export interface Exercise {
  id: number | string;
  name: string;
  sets: string;
  reps: string;
  load: string;
  load_type?: "abs" | "pct" | string;
  rpe?: string;
  note?: string;
  tag?: string;
  last?: string;
  /** Group mode: this row's badge summary, e.g. "MO -10%", or null. */
  adj?: string | null;
  /** Group mode: every member's stored diff on this row. */
  adjusts?: OverrideAdjust[];
  /** Individual mode, %1RM rows only. */
  one_rm?: string;
  one_rm_source?: string;
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

export interface GroupMember {
  id: string;
  name: string;
  initials: string;
}

/** Group identity for the left rail / top bar (serialize_group_identity). */
export interface GroupIdentity {
  id: number | string;
  name: string;
  focus?: string;
  member_count: number;
  members: GroupMember[];
  flags: string[];
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
 * and applyPlanData adopts wholesale: plan/group/athlete/program/weeks/viewing/
 * phases/history all move together (inventory "Server contract").
 */
export interface PlanEnvelope {
  ok?: boolean;
  plan?: PlanSummary;
  group?: GroupIdentity | null;
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
}

export interface GridCell {
  prescription_id: number;
  sets: string;
  reps: string;
  load: string;
  load_type: "abs" | "pct";
  rpe: string;
  rest: string;
  note: string;
  skipped: boolean;
  swap_name: string;
  swap_exercise_id: number | null;
  /** Resolved one-week swap display name: `swap_name` if free-text, else the
   * swapped catalog exercise's name, else "" (no swap). A catalog-only swap
   * (swap_exercise_id set, swap_name blank) needs this to render a badge —
   * swap_name alone is blank for that case. */
  swap_display: string;
  /** P5 group: this cell's per-athlete adjust badge summary (e.g. "MO -10%"
   * or "2 adjusts"), or absent when no member has an effective adjust here.
   * Only attached for a GROUP plan (serialize_mesocycle_grid) — individual
   * plans never carry it, so `cell.adj` is `undefined` and MesoTable renders
   * no adjust control. Mirrors `Exercise.adj` on the single-week path. */
  adj?: string | null;
  /** P5 group: every member's stored diff on this cell (drives the override
   * editor's member dots + draft). Present alongside `adj`; absent otherwise. */
  adjusts?: OverrideAdjust[];
  /** Issue #455 phase A3: the athlete's persisted %1RM estimate for THIS
   * cell's resolved lift identity (swap-aware — a swapped cell reads its own
   * identity's estimate, not the row's block identity). Individual-plan-only
   * (server attaches uniformly per cell, regardless of `load_type`; absent
   * for a group plan and when the athlete has no stored estimate). */
  one_rm?: string;
  one_rm_source?: string;
}

export interface GridRow {
  exercise_slot_id: number;
  /** The BLOCK identity (not swap-resolved) — a swapped cell shows its own
   * `swap_name` alongside this. */
  name: string;
  exercise_id: number | null;
  order: number;
  tags: unknown[];
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

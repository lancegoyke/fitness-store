// useTableReorder (issue #455 phase A2 — drag reordering in the multi-week
// table) — a pure, stateless drag-event -> verb-call translator. Unlike
// useReorder (WeekGrid's Phase-4 hook), this hook owns NO optimistic state
// and makes NO fetch of its own: `grid` is read fresh at drop time (never
// carried on the event, mirroring useReorder's `program`) and every
// mutation routes through the two injected verbs — DesignerRoot wires them
// to useGrid's own `reorderExercises`/`reorderDays`, which already await
// their POST behind useGrid's shared `busyRef` guard (see useGrid.ts's
// header) — so there is no concurrency guard to duplicate here.
//
// Cross-day row moves are explicitly OUT of scope for A2 (own follow-up;
// the backend's `prescription_move` needs zero changes for it later) —
// enforced here as a second, independent guard even though MesoTable's own
// collision filter (`filterTableDragCandidates`) already keeps a row drag
// from colliding with another day's rows in the first place.
//
// Payload construction mirrors the two endpoints' exact contracts
// (app/store_project/meso/views.py `session_reorder`/`week_reorder_sessions`).
// Programs are date-less and carry no "current week" pointer
// (docs/meso/remove-current-week-plan.md) — both payloads anchor on the
// VIEWED week, `grid.weeks[0]` (the grid's own opening default, same as the
// server's `current_week(plan)` degrade), not a flagged week:
//  - Row order = the viewed week's (`grid.weeks[0]`) live row cells'
//    `prescription_id`s, in the new order — a row with no cell for that week
//    (an add-this-week-only hole) is excluded, mirroring the server's own
//    `session.cells()` query for that week.
//  - Day order = the viewed week's live Session pks, in the new order —
//    read from EACH day's own `GridDay.session_ids[String(currentWeekId)]`,
//    never `GridDay.session_id` (display-only, and `_pick_session_id`
//    (serializers.py:954) can fall back to a LATER live week's session for a
//    slot when the viewed week's was independently soft-deleted; the
//    fallback pk belongs to the WRONG week and would 400 server-side if
//    posted as part of the viewed week's order — Codex #455 A2 review
//    finding 2). Defensive no-op if ANY live day lacks a `session_ids` entry
//    for the viewed week.
//
// IMPORTANT (Codex #455 A2 review finding 1): identity for both verbs comes
// EXCLUSIVELY from the `TableDragData` carried on `active.data.current`/
// `over.data.current` — `active.id`/`over.id` are dnd-kit's own opaque
// sortable ids (in the real MesoTable wiring, ENCODED strings like
// "row-<daySlotId>-<exerciseSlotId>" — see ../lib/tableDragIds), never
// parsed or compared here as if they were the numeric slot ids themselves.
import { arrayMove } from "@dnd-kit/sortable";
import type { GridDay, MesoGrid } from "../lib/api";
import type { Id } from "./useGrid";

/** Carried by every sortable item via dnd-kit's `useSortable({ id, data })`
 * — decision 2 (brief): drag identity is exercise_slot_id/session_slot_id
 * (P0 block-wide identity), never prescription_id. */
export type TableDragData =
  | { type: "row"; daySlotId: Id; exerciseSlotId: Id }
  | { type: "day"; sessionSlotId: Id };

/** Modeled on dnd-kit's real `DragEndEvent` — MesoTable's `onDragEnd` adapts
 * the real event into this shape before calling the hook's handler (mirrors
 * WeekGrid's `ReorderDragEndEvent` / handleDragEnd). */
export interface TableDragEndEvent {
  active: { id: Id; data: { current: TableDragData } };
  over: { id: Id; data: { current: TableDragData } } | null;
}

export interface UseTableReorderOptions {
  grid: MesoGrid | null;
  reorderRow(sessionId: Id, order: number[]): void;
  reorderDay(weekId: Id, order: number[]): void;
}

// The viewed week both reorder payloads anchor on — programs are date-less
// and carry no "current" pointer, so this is simply the grid's first live
// week (its own opening default), same as useGrid.ts's own `currentWeekId`.
function currentWeekId(grid: MesoGrid): Id | undefined {
  return grid.weeks[0]?.id;
}

export function useTableReorder(options: UseTableReorderOptions) {
  const { grid, reorderRow, reorderDay } = options;

  function rowReorder(day: GridDay, activeExerciseSlotId: Id, overExerciseSlotId: Id) {
    if (!grid) return;
    const weekId = currentWeekId(grid);
    if (weekId == null) return;
    const weekKey = String(weekId);
    // The endpoint is the VIEWED week's session — resolved via
    // `session_ids[weekKey]`, never `session_id`, which can silently be a
    // FALLBACK to a different week when the viewed week's session was
    // independently soft-deleted; posting viewed-week cell ids to a
    // fallback session 400s server-side (same guard as dayReorder below).
    const sessionId = day.session_ids[weekKey];
    if (sessionId == null) return;
    // Only rows with a live cell for the VIEWED week can appear in the
    // order array — session_reorder's contract is EXACTLY session.cells()
    // for the viewed week. A row that exists only on some OTHER week (an
    // add-this-week-only hole) is invisible here, same as the server's own
    // query.
    const liveRows = day.rows.filter((r) => r.cells[weekKey]);
    const oldIndex = liveRows.findIndex((r) => r.exercise_slot_id === activeExerciseSlotId);
    const newIndex = liveRows.findIndex((r) => r.exercise_slot_id === overExerciseSlotId);
    if (oldIndex === -1 || newIndex === -1) return;
    const reordered = arrayMove(liveRows, oldIndex, newIndex);
    const order = reordered.map((r) => r.cells[weekKey]?.prescription_id).filter((id): id is number => id != null);
    reorderRow(sessionId, order);
  }

  function dayReorder(activeSessionSlotId: Id, overSessionSlotId: Id) {
    if (!grid) return;
    const weekId = currentWeekId(grid);
    if (weekId == null) return;
    const weekKey = String(weekId);
    // Defensive no-op: every live day must resolve its OWN viewed-week
    // session id via `session_ids[weekKey]` (see this file's header) —
    // `session_id` can silently be a FALLBACK to a different week and must
    // never substitute here, or the posted order 400s server-side.
    if (grid.days.some((d) => d.session_ids[weekKey] == null)) return;
    const oldIndex = grid.days.findIndex((d) => d.session_slot_id === activeSessionSlotId);
    const newIndex = grid.days.findIndex((d) => d.session_slot_id === overSessionSlotId);
    if (oldIndex === -1 || newIndex === -1) return;
    const reordered = arrayMove(grid.days, oldIndex, newIndex);
    const order = reordered.map((d) => d.session_ids[weekKey]).filter((id): id is number => id != null);
    reorderDay(weekId, order);
  }

  function onDragEnd(event: TableDragEndEvent): void {
    if (!grid || !event.over || event.active.id === event.over.id) return;
    const { active, over } = event;
    const activeData = active.data.current;
    const overData = over.data.current;

    if (activeData.type === "row" && overData.type === "row") {
      if (activeData.daySlotId !== overData.daySlotId) return; // cross-day: OUT of A2 scope
      const day = grid.days.find((d) => d.session_slot_id === activeData.daySlotId);
      if (!day) return;
      // Identity is the TableDragData payload, NEVER active.id/over.id (the
      // real MesoTable emits ENCODED string ids there — Codex #455 A2
      // review finding 1).
      rowReorder(day, activeData.exerciseSlotId, overData.exerciseSlotId);
      return;
    }

    if (activeData.type === "day" && overData.type === "day") {
      dayReorder(activeData.sessionSlotId, overData.sessionSlotId);
    }
    // Any other type combination (row-over-day-header etc.) is unreachable
    // through the real UI (separate row/day SortableContexts +
    // filterTableDragCandidates) — a silent no-op here.
  }

  return { onDragEnd };
}

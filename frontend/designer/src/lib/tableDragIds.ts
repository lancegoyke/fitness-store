// Shared dnd-kit id encoding for MesoTable's drag reordering (issue #455
// A2) — the SINGLE SOURCE OF TRUTH for the two sortable id string formats.
// Every site that builds or parses one of these ids goes through here:
// MesoTable.tsx's useSortable({id}) calls, its SortableContext `items`
// arrays, and its collision-filter prefix tests (filterTableDragCandidates)
// — plus useTableReorder.test.ts's hand-built drag events, so the hook's
// specs construct ids the SAME way the real MesoTable does.
//
// useTableReorder.ts itself must NEVER import from here / parse these
// strings — a sortable id is opaque transport for dnd-kit; real identity for
// the hook always comes from the TableDragData payload carried as `data`
// (see that file's header). This module exists only for the places that
// legitimately need the encoded string: dnd-kit's own id prop, and the
// collision filter, which dnd-kit calls with only a raw id (no data) to
// decide.
import type { Id } from "../hooks/useGrid";

export const TABLE_DAY_DRAG_PREFIX = "day-";
export const TABLE_ROW_DRAG_PREFIX = "row-";

export function tableDayDragId(sessionSlotId: Id): string {
  return `${TABLE_DAY_DRAG_PREFIX}${sessionSlotId}`;
}

export function tableRowDragId(daySlotId: Id, exerciseSlotId: Id): string {
  return `${TABLE_ROW_DRAG_PREFIX}${daySlotId}-${exerciseSlotId}`;
}

/** The id prefix shared by every row belonging to one day — lets the
 * collision filter scope a lifted row to its own day's row containers
 * without decoding a full row id. */
export function tableRowDragPrefix(daySlotId: Id): string {
  return `${TABLE_ROW_DRAG_PREFIX}${daySlotId}-`;
}

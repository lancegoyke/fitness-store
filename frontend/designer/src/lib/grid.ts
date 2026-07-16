// Week-grid pure helpers ported from createMeso() (meso.js: numeric,
// loadSuffix, barH, cellOn, cellStyle). `round25`, `onDeliver`/`delivered`,
// `accent`, and `theme` are confirmed dead by the inventory and dropped
// (never template-wired / prototype-only props with no live consumer).
import type { Day, Exercise, MesoGrid, Phase, Week } from "./api";

/** True when `v` is a plain non-negative decimal string (e.g. sets/reps/load). */
export function numeric(v: unknown): boolean {
  const s = String(v == null ? "" : v).trim();
  return s !== "" && /^[0-9.]+$/.test(s);
}

// `loadSuffix` retired in Phase 2a (text-first cells): there's no typed
// `load`/`load_type` pair left to suffix — a "%"" or a unit lives inside the
// freeform prescription text itself.

/** Bar height for the block-view timeline chart, clamped to a 6px floor. */
export function barH(pct: number, track: number): string {
  return Math.max(6, (pct / 100) * track) + "px";
}

// The calendar view's weekday dots were driven by a hardcoded fixture field
// (`sessionDays`, confirmed fixture-only dead state by the inventory) rather
// than the real per-week schedule — no live endpoint returns which weekdays
// a week trains. cellOn/cellStyle themselves ARE still called from the live
// calendar view (designer.html's periodStyle === 'calendar' block), so they
// aren't dead code — only the field they read was. Ported as pure functions
// that take `sessionDays` as an optional parameter (default = the same
// Mon/Wed/Fri fixture the original hardcoded), so the calendar keeps its
// current decorative look until a real per-week schedule exists to feed in.
export const DEFAULT_SESSION_DAYS: readonly number[] = [0, 2, 4];

/** Whether the calendar dot at column `ci` is "on" for week `w`. */
export function cellOn(
  w: Pick<Week, "deload">,
  ci: number,
  sessionDays: readonly number[] = DEFAULT_SESSION_DAYS,
): boolean {
  return sessionDays.indexOf(ci) >= 0 && !(w.deload && ci === 4);
}

/** The calendar cell's inline style string (faithful port of cellStyle). */
export function cellStyle(
  w: Pick<Week, "current" | "deload">,
  ci: number,
  sessionDays: readonly number[] = DEFAULT_SESSION_DAYS,
): string {
  const on = cellOn(w, ci, sessionDays);
  const border = w.current ? "var(--soft-line)" : "var(--line)";
  const bg = on ? (w.current ? "var(--accent)" : "var(--soft)") : "var(--rail)";
  return (
    "height:34px;border-radius:7px;border:1px solid " +
    border +
    ";background:" +
    bg +
    ";display:flex;align-items:center;justify-content:center"
  );
}

// --- Issue #455 phase A5: re-sourcing AthletePreview / TopBar off the grid
// alone (the one-week usePlanData/serialize_plan owner is retired) --------

/**
 * AthletePreview's `program` prop, derived from the grid instead of a
 * separately-hydrated one-week payload. Walks `grid.days`, and for each row
 * picks that row's cell at the resolved week (`weekId` if given, else the
 * grid's own current week, else the FIRST week — the same fallback the
 * server's `current_week(plan)` and `cycleLabelFromGrid` apply, so a block
 * whose pointer isn't set still previews instead of blanking) — a row with
 * no cell for that week is simply omitted, mirroring `Session.cells()` only
 * ever surfacing live cells server-side. A DAY whose resolved week has no
 * live session (`session_ids` omits the week — a per-week session delete)
 * is omitted entirely: the athlete won't see that session, so neither
 * should the preview (the retired `serialize_plan` filtered on the open
 * week's live sessions the same way). The exercise name is just `row.name`
 * now — Phase 2a retired the one-week swap fields, so there's no per-cell
 * display name left to override the block identity.
 */
export function gridToProgram(grid: MesoGrid, weekId?: number | string): Day[] {
  const week =
    weekId != null
      ? grid.weeks.find((w) => w.id === weekId)
      : (grid.weeks.find((w) => w.current) ?? grid.weeks[0]);
  if (!week) return [];
  const weekKey = String(week.id);

  const days: Day[] = [];
  for (const day of grid.days) {
    const sessionId = day.session_ids[weekKey];
    if (sessionId == null) continue;
    const exercises: Exercise[] = [];
    for (const row of day.rows) {
      const cell = row.cells[weekKey];
      if (!cell) continue;
      exercises.push({
        id: cell.prescription_id,
        name: row.name,
        text: cell.text,
        lines: cell.lines,
        tempo: row.tempo,
        rest: row.rest,
        note: row.note,
        tag: typeof row.tags[0] === "string" ? row.tags[0] : undefined,
        skipped: cell.skipped,
        adj: cell.adj ?? null,
        adjusts: cell.adjusts ?? [],
      });
    }
    days.push({
      id: sessionId,
      n: day.day_number,
      name: day.name,
      bias: day.bias,
      exercises,
    });
  }
  return days;
}

/**
 * TopBar's cycle label, derived from the grid's own phases/weeks instead of
 * usePlanData's "viewed week" (which no longer exists — the table shows
 * every week as columns at once, so there's no single week being "viewed").
 * Same formula as the retired `usePlanData.cycleLabel`, just keyed off the
 * grid's CURRENT week instead.
 */
export function cycleLabelFromGrid(phases: Phase[], weeks: Pick<Week, "id" | "label" | "current">[]): string {
  const currentPhase = phases.find((p) => p.state === "current") ?? phases[0] ?? null;
  const week = weeks.find((w) => w.current) ?? weeks[0] ?? null;
  const phase = currentPhase ? currentPhase.name : "";
  const wk = week ? week.label + (weeks.length ? " / " + weeks.length : "") : "";
  return [phase, wk].filter(Boolean).join(" · ");
}

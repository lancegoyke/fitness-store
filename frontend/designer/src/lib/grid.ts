// Week-grid pure helpers ported from createMeso() (meso.js: numeric,
// loadSuffix, barH, cellOn, cellStyle). `round25`, `onDeliver`/`delivered`,
// `accent`, and `theme` are confirmed dead by the inventory and dropped
// (never template-wired / prototype-only props with no live consumer).
import type { Exercise, Week } from "./api";

/** True when `v` is a plain non-negative decimal string (e.g. sets/reps/load). */
export function numeric(v: unknown): boolean {
  const s = String(v == null ? "" : v).trim();
  return s !== "" && /^[0-9.]+$/.test(s);
}

/**
 * The Load cell's suffix: "%" for a %1RM row, `unit` for an absolute (or
 * typeless) numeric load, nothing for a non-numeric load ("BW"). The
 * original read `this.unit`; ported as an explicit argument since this is
 * no longer a component method.
 */
export function loadSuffix(
  ex: Pick<Exercise, "load" | "load_type"> | null | undefined,
  unit: string,
): string {
  if (!numeric(ex && ex.load)) return "";
  return ex?.load_type === "pct" ? "%" : unit;
}

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

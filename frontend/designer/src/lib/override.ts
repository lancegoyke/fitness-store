// Per-athlete override editor (group mode) pure helpers, ported from
// createMeso() (meso.js: overrideDraft, the overrideHasExisting getter, and
// parseOverrideLoadPct). The open/select/save/clear flow (network + which
// member is initially selected) moves to useOverrideEditor.
import type { Exercise } from "./api";

export interface OverrideDraft {
  swap: string;
  load_pct: string;
  sets: string;
  reps: string;
  note: string;
}

/** The member's stored diff on this row as editable strings (blank if unset). */
export function overrideDraft(ex: Exercise, memberId: string): OverrideDraft {
  const found = (ex.adjusts || []).find((a) => a.id === memberId);
  return {
    swap: (found && found.swap) || "",
    load_pct: found && found.load_pct != null ? String(found.load_pct) : "",
    sets: (found && found.sets) || "",
    reps: (found && found.reps) || "",
    note: (found && found.note) || "",
  };
}

/**
 * True when the given member already has a stored adjust on this row (so
 * the editor can offer "Clear"). Ported from the `overrideHasExisting`
 * getter, which read `this.override.ex`/`this.override.memberId` — here
 * both come in as explicit arguments.
 */
export function overrideHasExisting(ex: Exercise, memberId: string): boolean {
  return (ex.adjusts || []).some((a) => a.id === memberId);
}

export type ParsedLoadPct = { ok: true; value: number | null } | { ok: false };

/**
 * Parses the load% field to the endpoint's int | null. Blank → null (clear
 * that part); anything but a whole number in the model's 1-200 band is
 * rejected here so the badge never repaints off a server 400.
 */
export function parseOverrideLoadPct(raw: string): ParsedLoadPct {
  const trimmed = (raw || "").trim();
  if (trimmed === "") return { ok: true, value: null };
  if (!/^[0-9]+$/.test(trimmed)) return { ok: false };
  const n = parseInt(trimmed, 10);
  if (n < 1 || n > 200) return { ok: false };
  return { ok: true, value: n };
}

// Coach 1RM editor validation, ported from createMeso()'s parseOneRm
// (meso.js). The individual-mode %1RM editor's pure input-parsing slice —
// the open/save/network flow moves to useOneRmEditor.
import { numeric } from "./grid";

export type ParsedOneRm = { ok: true; value: string } | { ok: false };

/**
 * Parses the 1RM input to the value the endpoint expects: "" clears (back
 * to the log-derived estimate), a positive number sets, anything else is
 * rejected here so the badge never repaints off a server 400.
 */
export function parseOneRm(raw: string): ParsedOneRm {
  const trimmed = (raw || "").trim();
  if (trimmed === "") return { ok: true, value: "" };
  if (!numeric(trimmed) || parseFloat(trimmed) <= 0) return { ok: false };
  return { ok: true, value: trimmed };
}

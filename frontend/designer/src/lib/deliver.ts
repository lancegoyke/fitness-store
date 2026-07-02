// The top-bar "Deliver" link target, ported from createMeso()'s
// `deliverHref` getter (meso.js). Pinned to the *viewed* week (?week=) so
// "Deliver" sends the week on screen rather than always the live one; falls
// back to the bare deliver URL with no plan (the bare designer redirects to
// a real plan anyway).
export function deliverHref(
  planId: number | string | null | undefined,
  viewedWeekId: number | string | null | undefined,
): string {
  if (planId == null) return "/meso/deliver/";
  const base = `/meso/deliver/${planId}/`;
  return viewedWeekId != null ? `${base}?week=${viewedWeekId}` : base;
}

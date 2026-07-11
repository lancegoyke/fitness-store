// First-run coachmark localStorage helpers, ported from createMeso()
// (meso.js: coachmarkStorageKey, readCoachmark, dismissCoachmark). The
// reactive dismissed-map + `coachmarkVisible` derivation moves to
// useCoachmarks — these are the two storage-facing primitives it calls.

/** The dismissible region notes on the designer (one-week grid / phone
 * preview / multi-week table — the table note added by issue #455 phase A4).
 * "grid" and "phone" stay untouched: the one-week view they annotate is
 * slated for deletion in phase A5, not this one. */
export const COACHMARK_KEYS = ["grid", "phone", "table"] as const;

/**
 * The localStorage key for one region note's dismissal — namespaced under
 * `-designer-` so it never collides with the athlete onboarding coachmarks
 * (meso_onboarding.js uses the `meso-coachmark-` prefix too).
 */
export function storageKey(key: string): string {
  return "meso-coachmark-designer-" + key;
}

/**
 * Whether `key` was previously dismissed. Storage can be absent or throw
 * (Safari private mode) — treated as "nothing dismissed".
 */
export function readDismissed(key: string): boolean {
  try {
    const store = typeof window !== "undefined" && window.localStorage;
    return !!store && store.getItem(storageKey(key)) === "1";
  } catch {
    return false;
  }
}

/**
 * Persists a dismissal best-effort. Never throws — the caller hides the
 * note in-page via reactive state regardless of whether the write lands.
 */
export function dismiss(key: string): void {
  try {
    const store = typeof window !== "undefined" && window.localStorage;
    if (store) store.setItem(storageKey(key), "1");
  } catch {
    /* best-effort — hidden in-page via reactive state regardless */
  }
}

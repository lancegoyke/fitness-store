// Exercise-name suggestions for the row-name combobox (#608). Pure: the
// hydration payload (`#meso-exercise-suggest`) and the grid's own row names go
// in, an ordered, capped suggestion list comes out. No DOM, no React.
//
// Ranking: the coach's own names first (what they already use reads as "their
// vocabulary"), then catalog exercises; within each group a name-PREFIX match
// ranks ahead of a word-START match ("sq" -> "Squat" before "Back Squat").
// A catalog twin of a name the coach already uses is dropped (case-insensitive)
// so the same exercise never shows twice, with the coach's link winning.

export interface CatalogExercise {
  id: string;
  name: string;
}

export interface MineExercise {
  name: string;
  /** The Exercise this name is linked to, or null for a free-text name. */
  exercise_id: string | null;
}

export interface ExerciseSuggestions {
  catalog: CatalogExercise[];
  mine: MineExercise[];
}

export const EMPTY_SUGGESTIONS: ExerciseSuggestions = { catalog: [], mine: [] };

export interface Suggestion {
  name: string;
  source: "mine" | "catalog";
  /** The link a pick sends: a catalog id, the coach's own link, or null. */
  exerciseId: string | null;
}

export const MAX_SUGGESTIONS = 8;

const words = (s: string) => s.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);

/** 0 = whole-name prefix, 1 = every token starts some word, -1 = no match. */
function matchRank(name: string, query: string, tokens: string[]): number {
  const lower = name.toLowerCase();
  if (lower.startsWith(query)) return 0;
  const nameWords = words(name);
  return tokens.every((t) => nameWords.some((w) => w.startsWith(t))) ? 1 : -1;
}

export function suggestExercises(rawQuery: string, source: ExerciseSuggestions): Suggestion[] {
  const query = rawQuery.trim().toLowerCase();
  if (!query) return [];
  const tokens = query.split(/\s+/);

  const seen = new Set<string>();
  const mine: Suggestion[] = [];
  for (const m of source.mine) {
    const key = m.name.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    mine.push({ name: m.name, source: "mine", exerciseId: m.exercise_id });
  }
  const catalog: Suggestion[] = [];
  for (const c of source.catalog) {
    const key = c.name.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    catalog.push({ name: c.name, source: "catalog", exerciseId: c.id });
  }

  // The query already IS a listed name: offer just that entry, so the coach can
  // still pick it to LINK the row to the catalog (a typed name alone doesn't).
  const exact = [...mine, ...catalog].find((s) => s.name.trim().toLowerCase() === query);
  if (exact) return [exact];

  const ranked = (group: Suggestion[]) => {
    const hits = group
      .map((s, i) => ({ s, i, rank: matchRank(s.name, query, tokens) }))
      .filter((h) => h.rank >= 0);
    hits.sort((a, b) => a.rank - b.rank || a.i - b.i);
    return hits.map((h) => h.s);
  };
  return [...ranked(mine), ...ranked(catalog)].slice(0, MAX_SUGGESTIONS);
}

/** Coach's hydrated names plus the names of rows currently in the grid (so a
 * name typed this session shows up before any reload). Hydrated entries win
 * on a case-insensitive clash; blank names are skipped. */
export function mergeMine(hydrated: MineExercise[], gridRows: MineExercise[]): MineExercise[] {
  const seen = new Set<string>();
  const out: MineExercise[] = [];
  for (const m of [...hydrated, ...gridRows]) {
    const key = m.name.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(m);
  }
  return out;
}

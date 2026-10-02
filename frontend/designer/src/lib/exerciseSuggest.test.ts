import { mergeMine, suggestExercises, MAX_SUGGESTIONS } from "./exerciseSuggest";
import type { ExerciseSuggestions } from "./exerciseSuggest";

const src = (over: Partial<ExerciseSuggestions> = {}): ExerciseSuggestions => ({
  catalog: [
    { id: "c1", name: "Back Squat" },
    { id: "c2", name: "Squat Jump" },
    { id: "c3", name: "Bench Press" },
  ],
  mine: [],
  ...over,
});
const names = (r: { name: string }[]) => r.map((s) => s.name);

describe("suggestExercises", () => {
  it("is empty for a blank query", () => {
    expect(suggestExercises("", src())).toEqual([]);
    expect(suggestExercises("   ", src())).toEqual([]);
  });

  it("matches name prefix, case-insensitively", () => {
    expect(names(suggestExercises("BEN", src()))).toEqual(["Bench Press"]);
  });

  it("matches word-start, with prefix matches ranked first", () => {
    expect(names(suggestExercises("sq", src()))).toEqual(["Squat Jump", "Back Squat"]);
  });

  it("requires every query token to start some word", () => {
    expect(names(suggestExercises("sq ba", src()))).toEqual(["Back Squat"]);
    expect(suggestExercises("sq zz", src())).toEqual([]);
  });

  it("lists the coach's own names before the catalog", () => {
    const r = suggestExercises("sq", src({ mine: [{ name: "Box Squat", exercise_id: null }] }));
    expect(r.map((s) => [s.name, s.source])).toEqual([
      ["Box Squat", "mine"],
      ["Squat Jump", "catalog"],
      ["Back Squat", "catalog"],
    ]);
  });

  it("carries the link: catalog id, own id, own null", () => {
    const r = suggestExercises("s", src({ mine: [{ name: "Sled Push", exercise_id: null }, { name: "Snatch", exercise_id: "m1" }] }));
    expect(r.find((s) => s.name === "Sled Push")?.exerciseId).toBeNull();
    expect(r.find((s) => s.name === "Snatch")?.exerciseId).toBe("m1");
    expect(r.find((s) => s.name === "Squat Jump")?.exerciseId).toBe("c2");
  });

  it("drops the catalog twin of a name the coach already uses", () => {
    const r = suggestExercises("back", src({ mine: [{ name: "back squat", exercise_id: "c1" }] }));
    expect(r.map((s) => [s.name, s.source])).toEqual([["back squat", "mine"]]);
  });

  it("offers only the exact entry when the query equals a listed name (so it can be linked)", () => {
    expect(suggestExercises("back squat", src()).map((s) => [s.name, s.exerciseId])).toEqual([["Back Squat", "c1"]]);
    expect(suggestExercises("BACK SQUAT ", src()).map((s) => s.name)).toEqual(["Back Squat"]);
    const mine = src({ mine: [{ name: "Back Squat", exercise_id: null }] });
    expect(suggestExercises("back squat", mine).map((s) => [s.source, s.exerciseId])).toEqual([["mine", null]]);
  });

  it("caps the list", () => {
    const catalog = Array.from({ length: 20 }, (_, i) => ({ id: `i${i}`, name: `Curl ${i}` }));
    expect(suggestExercises("cur", { catalog, mine: [] })).toHaveLength(MAX_SUGGESTIONS);
  });
});

describe("mergeMine", () => {
  it("appends grid names, hydrated wins on clash, blanks skipped", () => {
    const r = mergeMine(
      [{ name: "Squat", exercise_id: "a" }],
      [{ name: "squat", exercise_id: null }, { name: "", exercise_id: null }, { name: "Row", exercise_id: null }],
    );
    expect(r).toEqual([
      { name: "Squat", exercise_id: "a" },
      { name: "Row", exercise_id: null },
    ]);
  });
});

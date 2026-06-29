# Meso — units & RPE/%1RM slice (S2) plan

**Status:** living document · started 2026-06-28
**Decision context:** [`decisions.md`](./decisions.md) S2 — *Units & RPE vs %1RM —
per-athlete/coach setting; needs a home.* The major Meso slices (individual coach +
agent + athlete PWA + groups) are all built and deployed (`mockdata.py` is gone), so
S2 is the next deferred secondary decision picked up.

## What's already done vs. the gap

Half of S2 — **units (kg/lb)** — already shipped incidentally with earlier slices:

- `Unit` (`kg`/`lb`) `TextChoices`, `CoachProfile.default_unit`, and **`Plan.unit`** all
  exist; the unit is threaded through the serializers (`plan.unit`), the presenters
  (results / last-logged labels), the designer JS (`loadSuffix`, `unit`), the seed, and
  the factories.

The genuine gap is the **other half: how intensity is prescribed — %1RM vs. absolute
load.** Today the designer's *Load* cell is free text whose number **always means an
absolute load in the plan's unit** (`loadSuffix` blindly appends `kg`/`lb` to any numeric
load). A coach who programs "75% of 1RM" has no first-class way to say so — the suffix
would wrongly read "75 kg", the athlete can't tell it's a percentage, and nothing downstream
knows the number is a percent.

**RPE is already supported and is orthogonal:** `ExercisePrescription.rpe` is its own
column that can coexist with either an absolute load or a %1RM (a coach may cap a %1RM lift
at RPE 8). So "RPE vs %1RM" is really: give **%1RM** the same first-class footing the
absolute load and the RPE column already have. We do **not** fold RPE into the load type —
it stays its own column.

## Modeling decision — `load_type` on the prescription

A `load_type` field on `ExercisePrescription` records **what the Load number means**:

- `ABSOLUTE` (`"abs"`) — the load is an absolute weight in the plan's unit (kg/lb). The
  current behavior; the **default**, so every existing row keeps reading exactly as today.
- `PERCENT` (`"pct"`) — the load is a **percentage of 1RM**; the cell renders with a `%`
  suffix instead of the unit.

Per-**prescription** (not per-plan): coaches genuinely mix schemes inside one session — a
main lift at 80% 1RM, accessories at an absolute load or by RPE. A per-plan flag couldn't
express that. The `rpe` column is unchanged and orthogonal.

This reuses the existing program tree — no new table, just a typed enum on the row, exactly
the "reuse what exists, add only what's load-bearing" taste of the athlete and groups slices.

## Phasing

Small vertical slices, red→green, each its own PR — the same cadence as the persistence,
agent, athlete, and groups slices.

- **Phase 1 — first-class %1RM in the coach's program + athlete read (this PR).** The
  `load_type` field + migration; the designer's Load cell renders/​toggles `%` vs the unit
  and autosaves the type; the per-athlete override resolution and the group deliver-to-all
  fan-out carry the type through; the athlete sees a `%` target on a %1RM-prescribed lift;
  the coach results screen labels a %1RM target with `%`. The seed gets a demo %1RM row.
- **Phase 2 — %1RM ergonomics + agent awareness (future).** The athlete logger surfacing
  the % target distinctly and capturing the *actual* load lifted against an estimated 1RM;
  the agent's grounding + validation understanding that a `progress` change on a %1RM lift
  moves a percentage (and bounding it sanely); optional estimated-1RM math (% ↔ load). Out
  of scope for Phase 1, which keeps the agent type-agnostic (it already treats `load` as an
  opaque numeric string, so a %1RM number progresses as a number — nothing breaks).

## Phase 1 — build notes

- **Model** (`meso/models.py`): a `LoadType` `TextChoices` (`ABSOLUTE`/`PERCENT`) beside
  `Unit`, and `ExercisePrescription.load_type` (`max_length=3`, default `ABSOLUTE`). The
  default means the migration is data-safe — every existing prescription reads as an absolute
  load, exactly as before. Migration `meso.0011`. `LoggedSet` deliberately gets **no**
  `load_type`: an athlete always logs the *actual* weight lifted (absolute), regardless of how
  the target was prescribed.
- **Serializers** (`meso/serializers.py`): `serialize_prescription` emits `load_type`;
  `resolve_prescription` carries `prescription.load_type` through both the base and the
  override branch (a member's `load_pct` scales a %1RM number to another % — the *type* is
  unchanged), so the resolver remains the single source of a member's effective row.
- **Autosave** (`meso/views.py`): `prescription_patch` accepts `load_type` *separately* from
  the free-text `PATCHABLE_FIELDS` — it is validated against the `LoadType` value whitelist
  (a bad value is a `400`, never persisted) rather than length-checked. New rows
  (`session_add_exercise`) inherit the model default `ABSOLUTE`.
- **Designer** (`templates/meso/designer.html` + `static/js/meso.js`): the Load cell's
  suffix becomes a small **toggle** — tap `kg`/`lb` ⇄ `%` to flip the row's `load_type`;
  `loadSuffix(ex)` returns `%` for a percent row and the unit for an absolute one;
  `persistRow` includes `load_type` so the toggle autosaves; a locally-added row defaults
  `load_type: "abs"`. The group override "shared …" line and the phone-preview target render
  `%` for a percent row too.
- **Athlete read** (`meso/presenters.py`): `_target_label` (the athlete logger's prescribed
  target) appends `%` to a percent load so the athlete sees "75%", not a bare "75";
  `_results_target_label` (the coach results screen) suffixes `%` for a percent target instead
  of the unit. The logged-set labels (`_logged_label` / `_summarize_last_sets`) are unchanged —
  they summarize the athlete's *actual* (absolute) loads.
- **Group fan-out** (`meso/models.py` `sync_delivered_plan`): the materialized per-member
  prescription copies `resolved["load_type"]`, so a member's delivered (athlete-facing) plan
  preserves whether each lift was prescribed as %1RM.
- **Seed** (`management/commands/seed_meso_demo.py`): one demo prescription is prescribed as a
  %1RM (e.g. the main squat at "75" `pct`), idempotent across reseeds, so a fresh DB shows the
  feature.

## Tests (Phase 1)

`meso/tests/test_load_type.py` — model + serializer + resolution + autosave + presenters +
fan-out:

- Model: `load_type` defaults to `ABSOLUTE`; accepts `PERCENT`.
- `serialize_prescription` emits `load_type`; `resolve_prescription` carries it through (base
  and override; a `load_pct` scale keeps the type `PERCENT`).
- `prescription_patch` writes a valid `load_type`, `400`s an invalid one (and persists
  nothing), and stays coach-scoped (`403` for a foreign coach).
- `_target_label` / `_results_target_label` show `%` for a percent load and the unit (or
  nothing) for an absolute one.
- `sync_delivered_plan` materializes a member prescription preserving the shared row's
  `load_type` (a %1RM shared lift → a %1RM member row).
- Seed (`test_seed_demo.py`): the demo plan has a %1RM prescription, not duplicated on reseed.

`frontend/meso.test.js` — the designer JS:

- `loadSuffix(ex)` returns `%` for a `pct` row, the unit for an `abs`/typeless row.
- `toggleLoadType(ex)` flips `abs` ⇄ `pct` and (when live) autosaves via `persistRow`.
- `persistRow` includes `load_type` in the autosave body.
- A locally added row (offline `addExercise`) defaults `load_type: "abs"`.

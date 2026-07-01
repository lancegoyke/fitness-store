# Meso — units & RPE/%1RM slice (S2) plan

**Status:** living document · started 2026-06-28
**Decision context:** [`decisions.md`](../../meso/decisions.md) S2 — *Units & RPE vs %1RM —
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
- **Phase 2 — %1RM ergonomics + agent awareness.** Split into two PR-sized slices, the same
  2a/2b cadence as the groups slice:
  - **Phase 2a — agent %1RM-awareness (this PR, DONE).** The grounding already carries each
    row's `load_type` (Phase 1 wired `serialize_prescription`), so the two real gaps were: the
    **prompt** never explained what `load_type` means, and the **validation backstop** never
    bounded a %1RM progression. Both closed — see below.
  - **Phase 2b — athlete %1RM logging ergonomics (this PR, DONE).** The athlete logger surfaces
    the % target distinctly and offers an **estimated-1RM helper** (% ⇄ load) so a "75%" target
    becomes a bar load. See below.

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

## Phase 2a — agent %1RM-awareness (build notes)

The agent context already carried each prescription's `load_type` (Phase 1 threaded it
through `serialize_prescription`, and `build_context` → `serialize_plan` → `serialize_session`
→ `serialize_prescription`). So no grounding *data* was missing. The two real gaps:

- **Prompt** (`agent/client.py`): the model was never told what `load_type` means. Added a
  `SYSTEM_PROMPT` rule — `abs` is an absolute weight in the plan's unit, `pct` is a percentage
  of 1RM; when progressing a `pct` lift the `new_load` is a percentage (kept sane, ≤ ~100%),
  and never convert one type into the other — and widened the `new_load` tool-field description
  to give both the `'92.5 kg'` and the `'82'` (%1RM) forms.
- **Validation bound** (`agent/validation.py`): the deterministic backstop now bounds a
  `progress` whose **target row** is `LoadType.PERCENT`. The new load must be a **clean percent**
  — a bare number with an optional `%` (`'82'`, `'82.5 %'`) — in `0 < pct ≤ MAX_PERCENT_1RM`
  (120 — above legitimate supramaximal eccentric/walkout work, below a plainly-absolute number
  like "180"). Three things are **rejected** (the candidate is dropped before it reaches the
  review screen, exactly like a contraindicated swap): an out-of-band number (`'180'`), a
  non-numeric value (`'heavy'`), and — importantly — a **unit-suffixed** value (`'100 lb'`),
  which is the model converting the lift to an absolute weight; silently storing that as `100%`
  would corrupt the prescribed intensity, so it is *not* coerced. A valid one is **normalized to
  a bare percent** (`'82.5 %'` → `'82.5'`) so the designer's `%` suffix isn't doubled. The
  absolute path is deliberately left unbounded (no sane ceiling on a kg/lb load). Keyed on the
  *target prescription's* type — the change dict itself carries no type — so it reuses the
  prescription `clean_change` already resolves.

Deliberately **not** in 2a: the agent does not *change* a row's `load_type` (it only progresses
within the existing type), and nothing here touches the athlete logger (→ Phase 2b).

### Tests (Phase 2a)

`meso/tests/test_agent_validation.py`:

- `TestPercentProgressBound` — a %1RM progress accepts a sane percent (`'82'`), strips a `%`
  sign (`'82.5 %'` → `'82.5'`), rejects a unit-suffixed load (`'100 lb'`), an absolute-looking
  load (`'180'`), and a non-numeric value (`'heavy'`), allows legitimate supramaximal (`'105'`),
  and leaves the **absolute** path unbounded (`'180 kg'` still passes).
- `TestPercentAwarePrompt` — the `SYSTEM_PROMPT` mentions `load_type` + `1RM`, and the `new_load`
  tool field mentions the percent form (locks the prompt contract so it can't silently regress).

## Phase 2b — athlete %1RM logging ergonomics (build notes)

A %1RM target ("75%") is an **intensity, not a weight**: Phase 1 made the athlete *see* the
`%` (`_target_label`), but they still had to convert it to a bar load by hand. Phase 2b closes
that with an **estimated-1RM helper** in the logger — %1RM ⇄ load, both directions — without a
model change (a `LoggedSet` still records the *actual*, absolute weight lifted, per Phase 1).

- **Presenter** (`meso/presenters.py`): the data contract the client needs to offer the helper.
  `athlete_session` carries the plan's **`unit`** (kg/lb) into its context, and
  `athlete_log_payload` threads that `unit` (top-level) plus each row's structured **`load`** and
  **`load_type`** into the trimmed payload — so the client knows which rows are %1RM (and the
  percent value) rather than only the pre-rendered `target` string. No new query (`plan.unit` is
  on the already-`select_related`'d plan).
- **Client** (`static/js/meso_athlete.js`): the maths is client-side (the athlete's 1RM estimate
  is per-device convenience, not coach-owned program data). Pure, Vitest-exported helpers —
  `epleyOneRm(load, reps)` (Epley `w × (1 + reps/30)`; a single rep returns the load itself, not
  the formula's slight overshoot; null for any non-numeric cell), `loadForPercent(oneRm, percent)`
  (plate-rounded via `roundToStep`, mirroring the designer's `round25`), and `fmtNum`/`parseNum`.
  Component methods `isPercentLift(ex)`, `suggestedLoad(ex)` (% → "90 kg"), and
  `setImpliedOneRm(row)` (a logged set's implied 1RM, so the athlete can refine the estimate from
  what they actually lifted). The estimate lives in **localStorage** keyed by exercise id
  (`meso-e1rm`), hydrated on `init()` — the same "reuse what exists, defer new tables" taste as the
  offline log queue (S7); a persisted, coach-visible 1RM is a deferred follow-up.
- **Template** (`templates/meso/athlete_session.html`): for a %1RM lift only — a `%1RM` badge, a
  "your 1RM" input (`@input` persists), the suggested bar load (`75% ≈ 90 kg`), and a per-set
  `1RM ≈ …` hint. Absolute lifts are untouched (all gated on `isPercentLift`).

### Tests (Phase 2b)

`meso/tests/test_percent_logging.py` (pytest, the data contract):

- `athlete_session` context carries the plan's `unit`.
- `athlete_log_payload` carries a top-level `unit` and, per exercise, the structured `load` +
  `load_type` (a %1RM row → `pct`/"75", an absolute row → `abs`/"70").

`frontend/meso_athlete.test.js` (Vitest, the maths + persistence):

- `epleyOneRm` — single-rep returns the load, multi-rep uses Epley, null for BW/AMRAP/ranges/0.
- `roundToStep` / `loadForPercent` — plate-rounded suggested load (120 @ 75% → 90).
- `isPercentLift` / `suggestedLoad` (with unit) / `setImpliedOneRm` — only %1RM lifts get a
  suggestion; an absolute lift or a missing 1RM yields "".
- estimated-1RM persistence — round-trips per exercise, drops a cleared estimate, hydrates on init.

Deliberately **not** in 2b: a persisted/coach-visible estimated 1RM (a model + migration), and
auto-deriving the estimate from logged history — both deferred.

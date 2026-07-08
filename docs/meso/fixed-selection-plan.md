# Meso — fixed exercise selection & multi-week table plan

**Status:** IN PROGRESS — building as 6 per-phase PRs (P0…P5), started 2026-07-07 · **pre-launch: no
production data, all demo** (so this is a clean reshape + reseed, not a data migration)
**Companion to:** [`decisions.md`](./decisions.md), [`persistence-plan`](../archive/meso/persistence-plan.md) (the schema this reshapes), [`designer-framework-plan`](../archive/meso/designer-framework-plan.md) (soft-delete + undo op-log the writes reuse)

## Goal

Make **exercise selection fixed for the length of a mesocycle**. Within a block, the coach
picks the exercises once (per day); week to week they only manipulate the *numbers* — sets,
reps, load, RPE, and **rest** (a new field). This unlocks a **concise multi-week table** —
one table per training day, exercises down the side, weeks across the top — replacing
today's week-at-a-time tab strip, which hides the context of the previous and upcoming weeks.

### Requirements (locked with the coach, 2026-07-07)
1. **Enforcement — locked lineup, per-week exceptions allowed.** One shared lineup per
   mesocycle. A coach can deviate a single week in **three ways** (all confirmed): **swap**
   an exercise for that week, **skip** an exercise that week, or **add** an extra exercise
   that week only. The table stays clean and marks the exceptions.
2. **Layout — exercises × weeks, one table per training day.**
3. **No data to migrate.** The app is pre-launch; everything is demo/seed data. "Migrate
   everything" = **reshape the schema and reseed the demo**, not a lossless data migration.
4. **Rest — a first-class per-exercise field** that varies per week like load/RPE.
5. **Delivery — the whole block at once.** The athlete receives and can **see the entire
   block** (all weeks), not one week at a time as Meso does today.
6. **Deloads need no special machinery** — a deload is the same lineup with lower
   volume/intensity (ordinary per-week numbers); the rare "drop a lift on a deload" case is
   just a per-week *skip* exception.

Assumption baked in (say so if wrong): the **day structure** (how many days, each day's
name/bias) is also fixed for the mesocycle — exercises live inside days, so the day skeleton
is shared too. Everything numeric varies per week; only exercise identity + its day/order is
locked.

---

## Architecture — normalized lineup + per-week cells

Because there's no data to preserve, model the thing directly rather than layering onto the
current per-week rows: **identity lives once on the lineup; each week holds only numbers.**

```
Plan
 └──< Mesocycle (the block)
        ├──< SessionSlot   day: day_number, name, bias, order, deleted_at        ← fixed DAYS
        │      └──< ExerciseSlot   exercise→Exercise NULLABLE, name, order, del.  ← fixed EXERCISES = table ROWS
        └──< Week          index, phase, is_deload, is_current, delivered_at, del ← table COLUMNS

Prescription (a CELL = ExerciseSlot × Week)
        numbers:    sets, reps, load, load_type, rpe, rest, note
        exceptions: skipped (bool) · swap_exercise→Exercise NULL / swap_name (this week only)
        unique(exercise_slot, week)

Session (Week × SessionSlot) — thin per-week day instance, anchors logging
        └──< SessionLog(athlete, date, status, notes) ──< LoggedSet(prescription→CELL, set_number, reps, load, rpe)
```

**What changed from today.** Today `Session` and `ExercisePrescription` are **per-week** and
`append_week` deep-copies the whole grid forward, so "the same exercise" is N independent
rows that can drift. Now:
- `ExerciseSlot` owns the exercise **once per block** (the row in the table).
- `SessionSlot` owns the **day** once per block.
- `Week` is just the column (kept ~as-is).
- `Prescription` is the **cell** — pure per-week numbers, no identity of its own; it reads
  its exercise from the slot. That removes all "keep the copies in sync" logic — drift is
  impossible by construction.

### Invariants (the lock, for free)
1. **Identity = the slot.** A cell has no name/exercise; it inherits the slot's. The *only*
   place a cell overrides identity is a deliberate one-week **swap** (`swap_exercise`/`swap_name`).
2. **Order = slot order** (canonical); the table renders rows in `ExerciseSlot.order`.
3. **Presence = cell state.** Every `(slot × week)` has a cell. `skipped=True` = not trained
   that week (an em-dash in the table). An exercise that should exist in only one week is a
   slot whose cells are `skipped` everywhere else — so the grid stays dense and easy to render.

### Coach operations → writes

| Coach action | Write |
|---|---|
| Add exercise to the block | Create `ExerciseSlot` + a cell per week (default/blank numbers) |
| Remove exercise from the block | Soft-delete the `ExerciseSlot` (+ its cells) |
| Rename / change the exercise for the whole block | Edit the `ExerciseSlot` — every week follows automatically (no propagation code) |
| Reorder exercises | Reorder `ExerciseSlot`s |
| Edit a week's numbers | Patch one `Prescription` cell (sets/reps/load/rpe/**rest**/note) |
| **Swap** one week only | Set the cell's `swap_exercise`/`swap_name` — badged in the table |
| **Skip** one week only | Set the cell `skipped=True` → em-dash cell |
| **Add** one week only | New `ExerciseSlot`, cells `skipped` except the target week |
| Add / remove a day | `SessionSlot` create / soft-delete (cascades the per-week `Session`s) |
| Add a week | New `Week` + a cell per live `ExerciseSlot`, numbers copied from the latest week |
| **Fill across weeks** (convenience) | Copy one cell's numbers to the same slot in other weeks |

---

## The multi-week table (UI contract)

Today the designer is a React island fed by `serialize_plan`, whose `program` key holds
**exactly one week's grid** (`serializers.py:840`); the `WeekStrip` tab switcher re-fetches
the envelope pinned to another week. The table needs all weeks at once, so add a
mesocycle-scoped serializer:

```
serialize_mesocycle_grid(mesocycle) →
{
  mesocycle: { id, name, week_count },
  weeks: [ { id, index, label, phase, deload, current, delivered_at } ],   // columns
  days: [
    { slot_id, day_number, name, bias, order,
      rows: [                                                              // = ExerciseSlots
        { slot_id, name, exercise_id, order,
          cells: {                                                         // keyed by week id
            "<week_id>": { prescription_id, sets, reps, load, load_type,
                           rpe, rest, note, skipped, swap_name } } } ] } ]
}
```

**Front-end:** a new `MesoTable` component (one `<table>` per `day`, rows = `rows`, columns =
`weeks`) replaces `WeekStrip` + `WeekGrid` + `DayCard`. Deload columns get a header marker
(▽); skipped cells render an em-dash; swap cells get a badge. Cell edit is an autosave to the
cell-patch endpoint; identity/structure edits hit the slot endpoints below.

---

## Reseed (not a migration)

There is **no production data** — the app is pre-launch and everything is demo/seed. So the
"rollout" is just:
1. Reshape the models (new `SessionSlot`/`ExerciseSlot`/`Prescription`; retire the per-week
   `ExercisePrescription`; keep `Week`; thin `Session`). Because there are no real rows to
   preserve, the Django migration can drop/recreate freely.
2. Rewrite the demo seeder (`seed_meso_demo`) and Factory Boy factories to build blocks in
   the new shape (a lineup + weeks of numbers, with a couple of exceptions to exercise the UI).
3. Re-run the seed in dev / the sandbox.

No lossless-migration logic, no drift reconciliation — that entire section of the original
plan is gone.

---

## Delivery — whole block at once

Meso delivers **one week at a time** today: `plan_deliver` stamps a single `Week.delivered_at`
+ snapshot, and the athlete home shows `latest_delivered_week` (`serializers.py:552`). The
coach wants to release the **whole block** and let the athlete see every week. Reuse the
existing per-week gate rather than inventing a block-level one:

- **Deliver block** (replaces "deliver week"): stamp `delivered_at = now` on **every live
  week** in the mesocycle and write a `WeekDelivery` snapshot per week. Re-delivering
  re-stamps; adding a week later + re-delivering releases it.
- **Athlete home (`/meso/me/`)** shows the whole delivered block as the same read-only
  multi-week table, not just one week. Generalize `latest_delivered_week` → "the current
  block's delivered weeks."
- **`is_current`** now marks *which week the athlete is on* — what the athlete home opens to
  and where "today's session" comes from. (Auto-advancing it as weeks complete is a later
  refinement, not this spec.)
- **Notification** copy: "your new block is ready (N weeks)" instead of "week N delivered."
- **Logging** is per-week as today — the athlete opens a day and logs against that week's cells.

---

## Ripple by subsystem

- **Serializers / reads:** every `prescription.name`/`.exercise` read site now reads from the
  slot (or the cell's swap). Broad but mechanical; no data at stake.
- **Agent (own phase):** structural proposals (`add`, `swap`) act on `ExerciseSlot` / cell
  swaps (block- or week-scoped); numeric proposals (`progress`, `volume`, `deload`) patch
  cells. Update `validation.clean_change` (drop the hard `current_week` scope for structural
  kinds) and `apply.py`.
- **Group (own phase):** rewrite `sync_delivered_plan` to materialize per-member plans from
  slots + cells + overrides; `PrescriptionOverride` points at a member's cell.
- **Rest field:** add `rest` to the cell, `serialize_session`, `serialize_week_snapshot`, the
  cell editor, the athlete session view + logger, and `LoggedSet` display.

---

## Phasing

- **P0 — Schema + seed.** New `SessionSlot`/`ExerciseSlot`/`Prescription` (cell) + thin
  `Session`; retire per-week `ExercisePrescription`; `rest` on the cell. Rewrite
  `seed_meso_demo` + factories. Drop-and-recreate migration (safe — no data).
- **P1 — Table UI.** `serialize_mesocycle_grid` + `MesoTable`; slot CRUD + cell-patch write
  endpoints; `rest` in the cell editor.
- **P2 — Exceptions.** swap / skip / add-this-week; fill-across-weeks.
- **P3 — Whole-block delivery + athlete view.** Block-level deliver; athlete home shows the
  whole block; `is_current` = the week the athlete is on; notification copy.
- **P4 — Agent rescope.** Slot-level structural changes, cell-level numeric changes.
- **P5 — Group.** Slot/cell-based `sync_delivered_plan`; group table view.

### P2 → P3 carry-over (delivery ⨯ exceptions)

P2 delivered the coach **edit** UX for exceptions and made them coherent across the *designer*
surfaces (multi-week table, the transitional "This week" grid, and the athlete **preview**). The
**delivery** subsystem was deliberately left untouched — it is P3's job to rewrite delivery around
the slots+cells+exceptions model. Concrete items P3 must reconcile (surfaced by the P2 Codex review):

- **Deliver-screen diff is not exception-aware.** `diff_week_snapshots` / `_PRESCRIPTION_DIFF_FIELDS`
  omit `skipped`, so skipping/unskipping a *delivered* row is not reported as a change (swaps already
  read as a `name` change). Adding `skipped` to the generic diff is not enough on its own: (a) the
  deliver screen renders diff fields through a `default`-filtered template, so a boolean `False→True`
  shows as `— → True` — a `skipped` diff needs off/on (or no/yes) rendering; and (b) an **add-this-week**
  row creates real `skipped=True` placeholder cells in every non-target week, and the pk-based "added"
  detection in `_diff_exercises` would surface those placeholders as phantom *added* exercises on
  redelivery of a non-target week even though the athlete has no new work. Do delivery-diff
  exception-awareness holistically in P3, not piecemeal.
- **`append_week` starts new-week cells clean** (documented decision in `Mesocycle.append_week`):
  `skipped`/`swap_*` are never carried forward. This means an "add-this-week-only" row becomes
  trainable in any week added *afterward*. That is consistent with the "a new week is a clean draft"
  model and is visible/correctable in the table, but if P3 wants one-week-only rows to stay absent
  from future weeks it needs an explicit product decision (the P0 model collapses "one-week-only row"
  and "normal row skipped elsewhere" into the same `skipped` representation).

---

## Resolved with the coach (2026-07-07)

- **One-week exceptions:** all three — swap, skip, add.
- **Delivery:** whole block at once; athlete sees every week.
- **Deloads:** same lineup, lower numbers; skip-exception covers the rare drop.
- **Data:** none (pre-launch, all demo) → reshape + reseed, no data migration.

## Implementation notes (2026-07-07, from the codebase recon)

Two spec assumptions did not hold against the real code, plus the delivery decision:

- **No "existing designer flag" to dark-launch behind.** `designer_flags` only drives the chat
  composer states; there is no dual-UI toggle. Since the app is pre-launch/demo-only, we do a **clean
  cutover** (matching the "reshape + reseed, drop-and-recreate" spirit) rather than build coexistence.
- **The undo/redo op-log (`history.py`) must be redesigned.** It snapshots weeks+sessions+prescriptions
  flat (identity + numbers together); splitting identity (slots) from numbers (cells) means
  `serialize_plan_snapshot`/`restore_plan_snapshot` learn the new models. (Not in the "Ripple" section.)
- **The designer front-end is a real React 19 + TypeScript island** at `frontend/designer/` (Vite build
  → `static/js/dist/designer.js`, Vitest tests, `@dnd-kit`, authoritative `CONTRACT.md`). P0 keeps the
  serializer JSON shape identical, so the island needs **no changes in P0**; P1's `MesoTable` reshapes it.
- **Shipping as 6 per-phase PRs** (P0…P5), each red/green-developed, Codex-reviewed, merged on green CI,
  and deployed, in sequence. `Prescription` (the cell) exposes resolving `name`/`exercise`/`tags`
  properties so most read sites survive the cutover unchanged.

## Open questions / risks

1. **`is_current` advancing over time** — kept as a manual marker of the week the athlete is
   on. Auto-advancing as weeks complete is a natural follow-up, out of scope here.
2. **Editing a delivered block** — the whole block is visible to the athlete, so later edits
   (e.g. tuning week 4) show live. Matches today's behavior but is more visible; confirm
   coaches are comfortable editing "ahead" of the athlete.
3. **Scope of the P0 reshape** — retiring `ExercisePrescription` touches serializers, the
   agent, group sync, and the seeder in one release. Pre-launch this is fine, but it's the
   biggest single phase; worth landing behind the existing designer flag until the table UI
   (P1) is ready.

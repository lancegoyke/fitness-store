# Meso — spreadsheet parity by simplification

**Status:** 3b built 2026-07-17 (template library UI) · Phase 3 built 2026-07-17 (import + validate) · Phase 2 COMPLETE (2e UI cleanup built 2026-07-16) · 2d built 2026-07-16 · 2c built 2026-07-16 · 2b built 2026-07-16 · 2a built 2026-07-16 · started 2026-07-16 · next: Later-phase extensions (tracking → PRs → agent)
**Owner:** Lance
**North star:** make writing a program in Meso as fast and frictionless as writing it
in a Google Sheet — keyboard-driven, freeform, one grid — then extend to tracking,
personal records, and the agent.

This is a **design + sequencing** doc. It grounds every decision in Lance's real
program-automation workflow (a library of ~57 single-sheet Google Sheets templates
that get copied per-client and hand-edited) and in a close read of 10+ real
templates + one filled client workbook. It supersedes the earlier Q2 "template
library deferred" punt in [`decisions.md`](decisions.md).

---

## 0. TL;DR

The single most important decision: **a program cell stops being seven structured
fields and becomes one freeform text string, parsed only when something needs
structure.** Everything else follows from that and from "simplify aggressively —
no users yet."

- **The cell is text.** `4 x 6, RPE 9` / `4 x 6, RPE 9, 225` / `3 x 12-15` / `20-60m`
  / `AMRAP` — whatever you type. A parser derives `{sets, reps, rpe, load, …}` on
  demand (for the agent and PR tracking), never as the source of truth.
- **One grid, weeks across the top.** All weeks of a block visible side-by-side as
  columns; prescription and execution live in the same grid, editable by coach and
  athlete.
- **Keyboard-first.** Arrow keys / Tab / Enter to move and fill, add rows and weeks,
  and copy-forward — no mouse required.
- **Live edits; "deliver" = a nudge.** Edits are always live to the athlete;
  delivering just sends a "check for updates" notification, not a per-edit push.
- **Aggressive removals** (each needs Lance's OK): the `MesoGroup` subsystem →
  a batch-deliver command; the structured cell fields → text + parser; the
  deliver-gate/diff machinery → live edits + a notify marker.
- **Keep:** undo/redo (version control), billing (untouched), delivered snapshots
  (repurposed as history/retention), the agent (deferred — the *eventual* main
  feature, built on the parse layer once the scaffolding exists).

---

## 1. Ground truth — how Lance actually programs

Evidence: the template library list (57 sheets), a close read of 101/102/103,
321, 402, 402G, 501, 601, and one filled client workbook (68 tabs). Raw grids and
verbatim cells saved under `scratchpad/templates/` (fixtures for the importer).

### The grid
- A program block = **7 `Day` sections**, weeks as **4 side-by-side columns**.
- Per exercise: a prescription cell per week (`3 x 12`), plus — in newer templates —
  a per-week **RPE sub-row** (`RPE 8 | RPE 9 | RPE 6-7 | RPE 10`), a per-row **Tempo**
  column (`201/212/EXP/ISO`), and a per-row **Rest** column (`PRN/2-3m/60-90s`).
- Column letters drift between template generations → **parse by header label,
  never by column position.**

### The cell format (design the model around these)
Real verbatim cells: `3 x 12`, `4 x 12`, `3 x 8 each`, `4 x 6 each`, `3 x 12-15`,
`3 x 15e`, `3 x 45s`, `3 x 1m`, `3 x 5 breaths`, `20-60m`, `15'`, `Up to 8 x 8s`,
`1x`, `3 x ?`, `AMRAP`, and whole circuits packed into one cell
(`A) EDT 1. RDL x 6 2. Bench press x 6`). Takeaways:
- Free text with heterogeneous units (`each`, `e`, `s`, `m`, `'`, `breaths`,
  `rounds`, `bpm`), ranges (`12-15`), placeholders (`3 x ?`), and hedges (`Up to`).
- **Load is usually absent** in the *blank* templates — progression rides on
  reps/sets + RPE, with intent in coach cues. Lance *does* seed loads inline for
  specific clients (`4 x 6, RPE 9, 225`). The text cell absorbs both.
- **Supersets are a text prefix** on the exercise name (`A)`, `C1)`, inner `1.`) —
  not a separate field.

### The proof that structured fields are the friction
Template **102 still contains a hidden legacy sheet** with dedicated
`Exercise | Notes | Sets | Reps | Rest | Load | Volume` columns and a Volume
formula — and the visible, delivered sheet **abandoned it** for freeform text cells
+ Tempo + an RPE sub-row. Lance already ran the experiment; freeform won. Build the
freeform cell.

### Families, variants, and macrocycles
- **Leading digit = a program family run as a curriculum sequence** (all `2xx`
  before `4xx`, etc.), not a focus tag — though families do cluster by emphasis
  in practice (strength: 2xx/4xx/7xx; conditioning/metabolic: 6xx; mixed: 3xx/5xx).
- **`101 → 102 → 103` is one macrocycle** ("Base 1/2/3", same 7-day slot structure,
  same weekly wave, exercises rotating within fixed movement slots) → **a family
  imports as one multi-block plan, not N plans.**
- **`G` = "Gym" (equipment-rich variant)**, *not* group: `402` vs `402G` share
  day/week structure and rep waves, differing only in exercise selection.
- Non-program members of the library exist: intake forms and single-week **testing
  batteries** (`H000/3G000/5G000`). Some templates bundle metadata + warm-up +
  program in one spreadsheet (101 = 5 tabs); bundling is inconsistent — only the
  7-day program grid is universal.

### The heterogeneous grid
The same table holds non-exercise rows: full-width banners, `Date:` rows,
`Rest 5 minutes` separators, `END OF WEEK` footers, aerobic time entries, named
benchmarks, substitution instructions, even URLs. **A "row" cannot be forced to be
a clean exercise.**

### The delivery model
Edits are **live the instant they're made** (it's a shared spreadsheet). For
in-person clients Lance never "delivers" — he introduces the program on the next
call. For async clients, "deliver" is a **one-time** "it's ready" ping; subsequent
edits stay live and are not re-delivered. Group classes = **one program → a list of
clients + minor per-client tweaks**, everyone synced on the same fixed 4-week block;
miss a week, just miss it.

### RPE-first, prescription-as-scaffolding
Lance's system is **RPE-first**. He usually sets weights; athletes can too;
occasionally he pre-seeds a starting weight. He thinks of the prescription as
"temporarily filling out scaffolding for the athlete's executed workout" — so the
line between prescribed and performed is intentionally soft.

---

## 2. The core model change — text-first cells, one grid

### 2.1 The cell
Today `Prescription` carries ~10 structured fields (`sets`, `reps`, `load`,
`load_type`, `rpe`, `rest`, `note`, `skipped`, `swap_exercise`, `swap_name`).

**Proposed:** a cell is `{ exercise_slot, week, line, text, skipped }` — one freeform
`text` field replaces the structured prescription fields; `line` orders a short
**vertical stack within an exercise** (line 0 = the sets/reps prescription, lines 1+
= optional sub-rows such as the per-week RPE row, reached by arrow-down — see §2.3).
So a per-week "cell" is really a top-down stack the coach fills line by line.
Structure is *derived*:

```
parse_prescription(text) -> { sets?, reps?, reps_range?, rpe?, load?, unit?,
                              duration?, amrap?, raw } | None
```

- Tolerant/best-effort; returns partial structure; never blocks entry on parse
  failure. Test corpus = the verbatim cells in §1 + the saved fixtures.
- Used by: the agent, PR/tracking, any analytics. **Not** persisted as truth
  (parse lazily / cache if needed).

### 2.2 The exercise row
- `ExerciseSlot.name` stays freeform text and **carries the superset prefix inline**
  (`C1) Slider hamstring curls`) — no separate grouping field.
- Add optional per-row **`tempo`** and **`rest`** text columns (they're per-exercise,
  not per-week — see open decision D2 on whether to keep them as columns or fold in).
- Keep the **optional** catalog FK (`exercise`, nullable) for later name→catalog
  matching; never required.
- Keep a per-exercise **instructions/cues note** (the merged Coach-Comment column —
  e.g. `Max fatigue / keep shoulders packed`) that each exercise starts with — **this
  is where the how-to lives and must be retained.** Other/athlete comments are
  **flexible** (Lance uses them little): an inline column or a sub-line, TBD at build.
- Rows need not be exercises: a row is text; "is this an exercise" is inferred by the
  parser, not enforced by the schema (accommodates separators, aerobic, benchmarks,
  notes).

### 2.3 Weeks and the grid
- Weeks stay `Week` rows under a `Mesocycle`, **rendered as side-by-side columns** —
  change the designer from today's one-week-at-a-time view to **all weeks visible**
  (look backward and forward at once).
- RPE: lives in a **separate per-week sub-row directly beneath** the prescription
  cell (reached by arrow-down), matching the templates' `RPE 8 | RPE 9 | …` row and
  Lance's actual habit. Generalized: an exercise row owns an ordered stack of freeform
  per-week **lines** (line 0 = sets/reps, then optional RPE / cue / note lines) —
  nothing is hardcoded to "RPE"; each line × week is one freeform cell. (Resolves D3.)

### 2.4 Prescription vs execution
Lance wants **one grid** that both coach and athlete edit; the prescription is
scaffolding the athlete completes. Proposed for now: **one shared text cell** —
coach seeds it, athlete overwrites/completes it, the final text is what happened.
Version history (undo/redo + delivered snapshots) preserves the evolution.

The prescribed-vs-performed *split* (needed for a real PR engine) is **deferred to
the tracking phase**, where we either (a) parse "performed" from snapshot history, or
(b) add a light `performed` text layer per cell shown stacked in the one visual cell.
Flagged as open decision D4 so we don't over-build it now.

### 2.5 What this ripples into
- `load_type` (abs/pct), numeric field validation, the `%1RM` helper
  (`AthleteOneRm`), and `swap_exercise`/`swap_name` all become **moot or optional** —
  swaps are just editing the freeform name/cell; %1RM isn't used by the templates
  (see D5).
- `PrescriptionOverride` (per-athlete group overrides) goes away with `MesoGroup`
  (§3.1).

### 2.6 Polymorphic cells & real-world annotations
Grounded in a fully-filled client program (tab `415 - 0626`). Layout: per-exercise
**merged** columns `Exercise (B) | Tempo (C) | Coach Comment (H) | Athlete Comment (I)
| Rest (J)`; **unmerged** week columns `Week 1-4 (D-G)`. The prescription sits on the
exercise's **line 0** across the week columns; execution and annotations are **added
as sub-lines beneath, per week, independently** — never overwriting the prescription.

Findings that shape the model:
- **A (week × line) cell is polymorphic freeform.** The same cell may hold a
  prescription (`3 x 1-2 each`), logged execution (`30lbs x 2 each`, `15lbs x 8`), an
  in-cell **substitution** — the movement actually performed that week
  (`DB pullover`, `R SL L glute max`) — a **skip** (`skip`), or a **note**
  (`paired with lat hang or plank to downward dog`). No stored type; parse on demand.
- **Deviations never overwrite the prescription.** Line 0 stays across all four weeks
  even when a week was skipped (`D118='skip'`) or swapped (`D124='DB pullover'`); the
  deviation lives in a sub-line. This is the arbitrary-sub-lines model in practice.
- **Exercise row identity is stable.** The merged `B` name (`C) Inverted push up`,
  `C) Turkish get up`) never changes even when a week's sub-cell names a different
  movement or says `skip`. Per-week deviation ≠ mutating the row.
- **Week cells are independent per (line, week).** Different weeks fill different
  sub-lines (wk1 skipped; wk2 swapped + 3 logged sets; wk3 two substitute movements;
  wk4 four logged sets). The model is a per-exercise **sub-grid of `line × week`
  freeform cells**, each independently filled or empty.
- **Consequence — three more fields die.** Meso's structured `swap_exercise` /
  `swap_name` / `skipped` all collapse into freeform text (a swap = typing the
  substitute name in the week's sub-cell; a skip = typing `skip`). The parser recovers
  "this week was a swap / skipped" only when the agent or PR engine needs it.
- Per-exercise attributes = merged columns: **Tempo** (D2 column), **Rest** (D2
  column), and a **per-exercise instructions/cues note** (the Coach-Comment column,
  `Max fatigue / keep shoulders packed`) that each exercise starts with — **retain it;
  it's where the how-to lives.** Other/athlete comments are **flexible** (Lance uses
  them little): an inline column or a sub-line, settle at build.

---

## 3. Simplifications & removals (each needs Lance's OK)

Per Lance: propose every removal explicitly. No users exist yet, so this is safe.

### 3.1 Remove the `MesoGroup` subsystem → batch deliver  ·  **needs OK (D1)**
Today `MesoGroup` = author one shared plan → attach athletes → layer per-athlete
overrides → deliver fans out live-linked personalized copies. Cost of removal: lose
"edit the one shared source and propagate to all mid-block" — which Lance's workflow
doesn't use (fixed 4-week blocks, individual tweaks).
- **Remove:** `MesoGroup`, `GroupMembership`, `PrescriptionOverride`, the group
  designer mode, and the materialization fan-out (`deliver_block` /
  `sync_delivered_plan`).
- **Replace with:** a **batch-deliver** command — pick a program/template → pick a
  list of clients → each gets an **independent, live-editable copy**. Optionally a
  lightweight saved **client list** so delivering to a time-slot class is one click.
- **Defer:** if "change this for the whole class at once" is ever needed, add a
  targeted "apply this edit to these clients" action — far cheaper than the whole
  group system.

### 3.2 Structured cell fields → text + parser  ·  **needs OK (already leaning yes)**
Covered in §2. This is the headline change and the biggest migration.

### 3.3 Deliver-gate + diff UI → live edits + a notify marker  ·  **needs OK (D6)**
- Edits are always live (no "hidden until delivered").
- "Deliver" becomes a one-time **notify** ("check for updates"), not a per-edit push.
- **Keep** `WeekDelivery` snapshots — repurposed as **history/retention** and the
  notify marker (they also feed PRs later). The changes-since-last-delivery *diff UI*
  can stay as an optional "what changed" view but is no longer a gate.

### 3.4 Template library (the deferred Q2) → `is_template` plans
A template = a `Plan` with `is_template=True` and no athlete, **editable in the same
grid** (no second editor). "New from template" / batch-deliver = a deep copy.

### 3.5 Keeps (explicitly not touched)
Undo/redo (version control), **billing (untouched)**, the athlete surface/PWA/logging
(simplified to the one-grid), and the **agent** — deferred; it becomes the main
feature *after* the scaffolding and consumes the parse layer.

---

## 4. Where other coaches differ (light generalization)

Freeform text cells **generalize well** — any notation works (%1RM, velocity,
tempo-heavy, DUP, conjugate, EMOM/AMRAP). The risks and mitigations:
- **Coaches who want structure** (auto-calculated volume, %1RM off a 1RM, progress
  charts, load prescriptions): the **parse layer + optional catalog link** is the
  escape hatch — structure is *derivable* without forcing it on entry. If demand is
  real, a per-coach "show structured columns" view can render parsed fields.
- **Dimensions coaches vary on:** RPE vs %1RM vs velocity; block vs DUP vs conjugate;
  in-person vs fully-remote; group vs 1:1; metric vs imperial; how much they log
  actuals. The text+parse model absorbs nearly all of these without bespoke schema.
- **Keep the door open, don't build it now:** ship the freeform grid; add structured
  render/analytics only if a real coach asks. This is a "small amount of ideation,"
  not a roadmap.

---

## 5. Import (validate, don't bulk-load)

- **Reuse `seed_meso_demo.build_block(dict→tree)`** as the target hook.
- Parser: classify tabs (program vs metadata vs warm-up), find the 7-day grid, read
  `Exercise / Tempo / Week 1..N / Rest` **by header label**, map each cell's text
  **verbatim** into the freeform cell, name (with prefix) into `ExerciseSlot.name`,
  tempo/rest into row columns, RPE sub-row folded into the week cell text.
- Handle: non-grid rows (banners/`Date:`/separators/footers/aerobic/benchmarks) as
  freeform rows; merged circuit cells as one row's text; multi-line notes; unit
  suffixes; placeholders (`3 x ?`); the drifting columns.
- **101/102/103 → one multi-block plan** (3 `Mesocycle`s); 321/402/402G/601/501 →
  single-block plans.
- **Scope:** validate on **3–5 templates** (101/102/103 + 402 + 601), find UX gaps,
  iterate. **No bulk import** of all 57 yet. Fixtures already saved under
  `scratchpad/templates/` become the parser's test cases.

---

## 6. Sequenced phases (matches Lance's ordering)

1. **Phase 1 — Design (this doc).** Data-structure adaptation + light generalization.
   Deliverable: the model change, the removals list, and the open decisions below.
2. **Phase 2 — Build the simplifications.**
   - 2a **Text-first cell** — model + migration (structured fields → `text` +
     `parse_prescription`); keep undo/redo. **✅ Built 2026-07-16** (branch
     `meso/2a-text-first-cell`): `Prescription` = `(exercise_slot, week, line,
     text, skipped)` — line 0 = the prescription, lines 1+ = freeform sub-rows;
     `swap_*`/`load_type`/per-week `rest`/`note` retired (swap/skip/notes are
     text per §2.6, `skipped` kept per §2.1); `ExerciseSlot` gained the
     per-exercise `tempo`/`rest`/`note` columns (D2); new `parsing.py`
     (`parse_prescription` + `compose_prescription_text`, corpus-tested);
     migrations `0038`/`0039` compose existing cells into Lance-notation text,
     hoist rest, convert `WeekDelivery` payloads, and **wipe the undo/redo
     stacks** (old snapshots capture retired columns); undo/redo itself now
     upserts cells by pk so sub-line edits round-trip. New endpoints:
     `cell_line_write` (sub-line upsert by slot/week/line) and
     `exercise_slot_patch` (tempo/rest/note); `prescription_swap` and the
     designer's %1RM editor + `load_type` toggle removed. The table renders
     one text input per week cell + the sub-line stack + Tempo/Notes/Rest row
     columns. Group overrides resolve as text interim (volume recomposed,
     swap/note/load% as extra sub-lines) until 2c removes them.
   - 2b **All-weeks grid + keyboard flow** — weeks side-by-side; arrows/Tab/Enter
     navigation; Enter-adds-row, add-week, `Ctrl-C`/`Ctrl-V` duplicate-forward
     (this also *is* the ad-hoc log mode — starting a session = add/duplicate a
     week/workout). **✅ Built 2026-07-16** (branch `meso/2b-grid-keyboard-flow`;
     frontend-only — weeks-side-by-side and add-week-with-carry-forward already
     existed from the A-series/2a): `useTableNav`'s cell identity gained a
     **line** — each week cell's sub-line stack (existing lines + the trailing
     ghost) is a run of vertical stops, so **ArrowDown from a prescription steps
     into its stack** (D3's "RPE row reached by arrow-down" made literal — the
     ghost is a stop, so minting the RPE line never needs the mouse); the
     per-row **Tempo/Notes/Rest columns joined the horizontal axis** (name →
     tempo → weeks… → notes → rest, the sheet's order); **Tab/Shift+Tab** walk
     that axis unconditionally, wrapping between rows (arrows still defer to
     the caret at text boundaries); **Enter = commit + move down one stop**,
     and at a day's LAST stop it **appends an exercise row** to that day
     (Enter never crosses a day boundary; a fully-blank row never appends —
     the blank check reads the DOM so an uncommitted draft counts as content;
     after the refetch, focus lands on the new row at the column Enter came
     from); horizontal moves from a sub-line **clamp to the target cell's
     nearest stop** (a shorter stack lands on its ghost — the merged-cell
     feel). **`Ctrl-C`/`Ctrl-V` = cell-stack copy/paste** on the prescription
     input: copy with nothing selected copies the whole stack newline-joined
     (a real selection stays native); pasting multi-line text replaces the
     stack (line 0 + sub-lines, longer old lines blanked); single-line paste
     stays native. No backend changes — appends reuse `session_add_exercise`,
     paste reuses `prescription_patch`/`cell_line_write`.
   - 2c **Remove `MesoGroup`** → batch-deliver + client list. **✅ Built
     2026-07-16** (branch `meso/2c-remove-mesogroup`): the whole group
     subsystem is gone — `MesoGroup`/`GroupMembership`/`PrescriptionOverride`,
     `Plan.group`/`source_group` + the XOR/singleton constraints,
     `deliver_block`/`sync_delivered_plan`, the override endpoint + designer
     override editor, the group designer mode (the island is single-mode now),
     the group agent (`Kind.ADJUST`, `Trigger.GROUP`, `_group_context`,
     member framing), the roster Groups card, the demo group segment, and the
     "groups" tour step (migration `0040`; shared group plans deleted by a
     data step, a member's materialized copy simply survives as an ordinary
     individual plan). **Replaced by batch-deliver:** `Plan.duplicate_for`
     deep-copies the live tree (whole line stacks, tempo/rest/note, tags,
     `is_current` mirrored, `delivered_at` reset) and the deliver screen
     offers the coach's other clients as checkboxes —
     `plan_batch_deliver` fans out one independent, live-editable ACTIVE copy
     per pick, stamped + snapshotted + notified exactly like an individual
     deliver. The saved client list (one-click class deliver) is deferred
     until real use demands it.
   - 2d **Deliver → live + notify** — repurpose snapshots. **✅ Built
     2026-07-16** (branch `meso/2d-live-plus-notify`): the delivery
     visibility gate is gone — the athlete sees **every live week** of every
     non-archived plan the moment the coach types it (home, chips, block
     grid, session logger, focus override, and logging all drop their
     `delivered_at` filters); the athlete home anchors on the `is_current`
     pointer (falling back to the earliest live week; `awaiting` = a plan
     with no weeks at all), and adherence re-bases from "latest delivered
     week" to the current week (`link_current_week` — newest plan by
     `modified`, then the flagged week). **Deliver stays as the one-time
     nudge:** it still stamps `delivered_at` (now purely a notify marker),
     snapshots every live week (`WeekDelivery` = history/retention, feeding
     the deliver screen's now-optional what-changed diff), and sends the one
     block-level email + push; the dead per-week notify chain
     (`_notify_athlete_delivered`, `send_week_delivered_email`,
     `notify_week_delivered` + templates) was removed. Copy reframed
     coach-side ("your edits are already live — delivering sends a heads-up")
     and athlete-side ("your program, live as your coach writes it"). No
     migration — `delivered_at`/`WeekDelivery` keep their data.
   - 2e **UI cleanup** — strip the chrome the above obsoletes. **✅ Built
     2026-07-16** (branch `meso/2e-ui-cleanup`; CSS-only — 2a–2d already
     removed the components/endpoints, this sweeps their orphaned styles):
     a mechanical sweep (every class defined in the designer stylesheets +
     `meso.css`, checked against all TSX/HTML/JS/PY usage incl.
     template-literal construction) found ~75 dead classes. Deleted:
     `designer-modal.css` wholesale (the 2c override-editor modal — modal
     chrome, field grid, member picker, save/clear/cancel buttons); the
     group chrome (topbar `meso-group-avatar*`, rail
     `meso-rail-avatar--group`/`meso-rail-group-glyph`/`--tight`,
     `meso-participant-*`, chat's `meso-change-member` chip, grid's
     `meso-adjust-*` badges); the structured-cell chrome (2a —
     `meso-onerm-*` ×7, `meso-load-toggle`, `meso-num-input`, `meso-note`
     + its `meso.css` focus twin, `meso-table-cell-setsreps/load`, the
     swap badge/editor/input family); the retired one-week-designer chrome
     (A5 — `meso-week-view*`, `meso-canvas-autosaved*`); and prototype
     leftovers (`meso-chip-soon`, `meso-flag-badge/dot`,
     `meso-grid--2`, `meso-inline-block`). Stale
     comments referencing the dead classes updated in place; dist rebuilt
     (designer.css 22.3 kB).
3. **Phase 3 — Import + validate.** Importer over 3–5 templates → surface UX
   limitations → iterate. **✅ Built 2026-07-17** (branch
   `meso/3-import-validate`): **template plans are first-class** (§3.4) —
   `Plan.is_template` + `Plan.owner` (the coach's library; a check constraint
   keeps templates relationship-less), `__str__`/`.coach` no longer crash on a
   relationship-less plan (the 2c Codex finding), and
   `is_editable_by`/`editable_by` grant the template's owner, so the owner
   edits a template **in the same designer grid** (identity chip shows the
   template's title + "Template"; deliver bounces back to the designer, the
   deliver/agent/1RM endpoints refuse cleanly — batch-deliver FROM a template
   works at the endpoint level, no UI yet). **The importer:**
   `meso/sheet_import.py` (openpyxl, new dependency) parses a Drive-exported
   template workbook into the exact `build_block` spec — visible-program-tab
   selection (102's hidden legacy tab skipped), per-Day header rows resolved
   by **label** (columns drift), exercise blocks by the B-column **merge
   extents** (name/tempo/coach-comment/rest merged down the block; blank
   set-detail log rows skipped), the newer templates' RPE row folded as
   per-week **sub-lines**, verbatim cells (float tempos coerced `201.0`→
   `201`), 601's `Rest 5 minutes` separators as cell-less freeform rows and
   its EDT/circuit packed cells as one row each; banners/`Date:`/`END OF
   WEEK` chrome skipped + reported, unknown structure never raises.
   `manage.py meso_import_template <xlsx>... --owner <email> [--title]`
   builds ONE template plan, one `Mesocycle` per file in order (the
   101→102→103 family = one 3-block plan), idempotent by
   (owner, title) re-run. Validated over all five raw fixtures
   (`docs/meso/fixtures/templates/{101,102,103,402,601}.xlsx`, now all
   committed): 7 days each; 22/22/22/19/13 exercises; 96/100/100/72/40
   cells; every skip accounted for (banner + date rows + footer).
   ~~Deferred: a template-library UI and a "new from template" button — the
   designer URL + batch-deliver endpoint are the only doors for now.~~
   **3b — template library UI ✅ Built 2026-07-17** (branch
   `meso/3b-template-library-ui`): `/meso/templates/` (`TemplateLibraryView`,
   login-gated, "Templates" link on the roster) lists the coach's owned
   templates alphabetically — each opens in the designer and, when the coach
   has active clients, offers **"Start for client"** (`template_use`, POST-only:
   deep-copies via `duplicate_for` into a live ACTIVE *undelivered* working
   plan — no snapshot, no nudge; batch-deliver stays the notify door — then
   lands in the copy's designer) and **batch deliver** (the 2c endpoint;
   a template source now redirects back to the library). Billing (D6):
   suspended relationships are excluded as copy targets at both endpoints,
   and `can_edit_plan` exempts templates from the coarse coach-wide freeze —
   templates hold no seat; billing bites at the copy targets (both Codex
   review findings).
4. **Later — Extensions.** Tracking (already `LoggedSet`), data retention (snapshots),
   **personal records** (parse layer + prescribed-vs-performed), then the **agent**
   as the main feature, grounded on the parse layer + the FAQ heuristics.

---

## 7. Open decisions (for Lance's OK)

**Status 2026-07-16:** D1, D2, D4, D5, D6 **confirmed** (leans accepted). D3 **resolved**
as a separate sub-row (below).

- **D1 — Remove `MesoGroup`? → CONFIRMED yes; built 2026-07-16 (2c).**
  Batch-deliver shipped; the optional saved client list is deferred.
- **D2 — Tempo & Rest → CONFIRMED columns** (per-exercise, not per-week).
- **D3 — RPE → RESOLVED: separate sub-row.** Lance types RPE in the cell **directly
  beneath** the sets/reps cell (a per-week sub-row reached by arrow-down), not inline.
  Model: exercise rows own an ordered stack of freeform per-week lines (§2.1, §2.3);
  the RPE row is just line 1, and further cue/note lines are allowed — nothing is
  special-cased. **Arbitrary sub-lines CONFIRMED (2026-07-16):** any number of freeform
  lines may sit beneath an exercise, not locked to RPE.
- **D4 — Prescription vs performed → CONFIRMED defer** the split to the PR phase.
- **D5 — `%1RM` / `AthleteOneRm` → CONFIRMED defer.**
- **D6 — Deliver → CONFIRMED** live-edits + one-time notify; keep `WeekDelivery`
  snapshots for history/retention (not as a gate).

---

## Appendix — the template library

57 single-sheet Google Sheets templates on Lance's **personal** Google account
(`lancegoyke@gmail.com` — the Drive connector must be on the personal account).
Naming: `<family digit><seq>` optionally `- <MMYY>`; `G` suffix = Gym (equipment-rich);
`H000/3G000/5G000` = testing batteries; `203v2` supersedes `203`; `602.5` = half-step
insert. Full name→spreadsheet_id→sheet_id list preserved privately in the session memory dir
(`spreadsheet-parity-template-ids.md`), kept out of the repo.

Exploration artifacts now live in [`fixtures/`](fixtures/) — see its `README.md` for
provenance, computability, and git handling. Contents: template grids (`.md`), the raw
template inputs we have (`102.xlsx`, `baseline-H000.xlsx`, `101.pdf`), the parsing
scripts, and an **anonymized** annotated-program sample
(`templates/annotated-program-sample.xlsx`, derived from a real client's tab
`415 - 0626` with identity + dates scrubbed; the raw client workbook was deleted, never
committed). `lance-program.xlsx` + `sheet-dump.json` persist locally but are gitignored.
Key source workbooks (re-fetchable via the connector): the filled history "Lance Goyke
program" (`1FLOdWQJn403nP42lWE-xWPQpO8d8LGRhfE5UjnOtaSg`) and the annotation-study client
(`1hd_MIzXGuKxbOSVOUb0FHGk0wKwtTurVy6bUkQPlLgI`, tab `415 - 0626`, gid `402688260`).
**Still missing for a full Phase-3 import set:** raw `.xlsx` for `101/103/402/601`
(currently `.md`/`.pdf` only) — re-export via the connector when building the importer.

### Column layout of a real program grid (reference for the importer)
`A = Day label · B = Exercise (merged down the block) · C = Tempo · D–G = Week 1–4
(unmerged, one cell per week) · H = Coach Comment / instructions (merged) · I = Athlete
Comment (merged) · J = Rest (merged)`. Header row repeats per Day section; column
letters drift between template generations — **parse by header label.**

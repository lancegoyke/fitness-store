# Meso — parse-at-commit (typed performed data → structured `LoggedSet`)

**Status:** planned 2026-07-18 · **not started** · design decided this session ·
**prerequisite:** the current-week removal
([`remove-current-week-plan.md`](remove-current-week-plan.md)) lands first (both
edit `athlete_cell_write`) · migration adds `LoggedSet.source_line`
**Owner:** Lance
**North star:** the athlete logs by **typing into grid cells** — one freeform
textbox per cell, like a spreadsheet. On blur we parse the text into a structured
`LoggedSet` so personal records (and later the agent) work off typed text, with
**zero friction**: a parse that fails never blocks entry, it just leaves the text
and shows a small warning.

This is a **design + sequencing** doc. The core (parser + silent structured store)
is slice **5a**; the settle/notification layer is slice **5b**. Both are grounded
in a code read of `parsing.py`, `models.py` (`LoggedSet` 2263), `views.py`
(`athlete_cell_write` ~1488), `personal_records.py`, `one_rm.py`, and
`presenters.py`.

---

## 0. TL;DR

- **One parser, load-first.** New `parse_performed(text)` classifies a cell into
  **set** (`225 x 5` → a `LoggedSet`), **skip**, **swap**, or **note** — all
  successful, no warning. It must be **load-first**: today's `parse_prescription`
  reads `225 x 5` as *225 sets* (prescription grammar); performed, it's *225 load
  × 5 reps*.
- **Silent parallel.** The freeform text stays the athlete's source of truth; the
  `LoggedSet` is a derivative linked back by a new `LoggedSet.source_line` FK. No
  double-display, no clobbering the structured logger.
- **Records go live.** Because there is no "I'm done" button (see
  [`remove-current-week-plan.md`](remove-current-week-plan.md) and §7), the
  derive-on-read reads that feed the personal-records panel + the PR toast count
  **pending** sets and re-derive on every edit (self-healing).
- **PR celebration = optimistic + confirmed.** 5a fires an **optimistic** 🎉 on
  blur when a set beats the current live best; 5b fires the **confirmed** one after
  a **24 h quiet-period settle** (a `django-q` sweep) and writes the persisted
  record.
- **Warning = fat-finger only.** The cell colors a warning **only** when the text
  *looks like a set attempt* (`225 x`) but won't resolve. Prose / skip / swap never
  warn. Derived on read — no stored flag.
- **Deferred (unchanged):** where athlete/"other" comments eventually live (inline
  column vs sub-line) — `parse_performed` tolerance means a note just yields no set,
  so this slice does **not** force that decision.

---

## 1. The two entry paths today (the thing we're collapsing)

Right now an athlete can put performed data in **two** places:

1. **The structured session logger** (`athlete_log_session`, `views.py:1312`) — a
   form that writes `LoggedSet` rows (`session_log`, `prescription`, `set_number`,
   `reps`/`load`/`rpe`) and is the **only** thing that marks a `SessionLog`
   **DONE**.
2. **The freeform grid cell / sub-line** (`athlete_cell_write`, `views.py:1488`) —
   made editable in 4a; saves the raw text as a `Prescription` sub-line
   (`athlete_authored=True`) and writes **no** structure.

The spreadsheet-parity model makes the **grid cell canonical**: the athlete types,
we parse. Parse-at-commit is what lets path 2 produce the structured record that
path 1 used to, so path 1 can eventually be retired (5b).

`LoggedSet` today (`models.py:2263`, verified): `session_log` (FK CASCADE),
`prescription` (FK **SET_NULL**, nullable), `set_number` (default 1), `reps` /
`load` / `rpe` (`CharField(32)`, blank). **No unit column** — the parser captures a
unit token but drops it on write (parity with the structured logger, which also
stores plain strings). `SessionLog.Status.DONE` exists (used by the DONE-only
scans in `personal_records.py:95` and `one_rm.py:73`).

---

## 2. Design decisions (all resolved 2026-07-18)

- **D-A — hook point: `athlete_cell_write`, on blur.** The cell already saves on
  blur; parse-at-commit adds the parse+upsert right after `cell.save(...)` (~line
  1550), inside the existing `transaction.atomic()`. No new endpoint.
- **D-B — silent parallel (not replace).** The freeform text is truth; the
  `LoggedSet` is a machine-readable derivative. Requires a persisted link back to
  the source cell → the new `LoggedSet.source_line` FK (§4). *(Lance's "leave it as
  a textbox" confirmed this over replace/absorb.)*
- **D-C — a new `parse_performed()` is required** (not a flag on
  `parse_prescription`). The `N x M` inversion is the common barbell case, not an
  edge case; sharing the segmenter would fight the prescription tuning. Reuse the
  token regexes only (§3).
- **D-D — athlete/"other" comments placement stays deferred.** A non-performed
  sub-line yields no set and remains pure annotation text; no schema decision is
  forced now.
- **Evolved product decisions (this session):**
  - **Textbox + warning** — one cell textbox; parse on blur; keep-as-text + a small
    color warning on a *failed set attempt* only (§8).
  - **Live, re-derived records** — the panel/toast reads count pending sets and
    re-derive on edit (§7).
  - **Optimistic + confirmed** PR celebration; **24 h** quiet-period settle window
    (§7, §9-5b).

---

## 3. `parse_performed(text)` — the parser (slice 5a)

New total function in `parsing.py` (never raises — parity with
`parse_prescription`). Returns `None` for empty/whitespace, else a dict tagged with
a classification:

| Input | Classified | Result |
|---|---|---|
| `225 x 5`, `135x5`, `225 x 5, RPE 8` | **set** | `{reps, load, rpe?}` → a `LoggedSet` |
| `30lbs x 8 each`, `102.5kg x 3`, `85% x 5`, `bw x 12` | **set** | load keeps its suffix string; unit token dropped on write |
| `5 @ 225` | **set** | `reps=5, load=225` |
| `225` (bare) | **set (partial)** | pin the decision: `load=225`, no reps → no e1RM |
| `skip`, `-`, `—` | **skip** | no set |
| `DB pullover`, `R SL L glute max` | **swap** | no set |
| `felt tight`, `paired with lat hang` | **note** | no set |
| `225 x`, `2255x5` | **unresolved-set** | no set + **warn** (§8) |
| `30s`, `20-60m` | **duration** | no set (not a lift) |
| `` / whitespace | — | `None` |

**Load-first is the whole point.** `_SETS_X` (`parsing.py:34`,
`^(?:up\s+to\s+)?(\d+)\s*[x×]\s*(.+)$`) claims a leading integer-then-`x` as
**sets**; `parse_performed` must try the **load × reps** split first so `225 x 5` →
`load=225, reps=5`. Reuse the existing `_LOAD` (bare / `%` / `lbs`/`kg` / `bw`),
`_REPS`, `_RPE`, `_DURATION` token regexes verbatim; add the `@` form and a
non-numeric-leading **swap** branch. **One set per line** (first recognized);
multi-set-per-line (`225x5, 230x3`) is out of scope — document it.

---

## 4. The model change — `LoggedSet.source_line` (slice 5a)

```python
source_line = models.ForeignKey(
    "Prescription", on_delete=models.SET_NULL,
    null=True, blank=True, related_name="parsed_sets",
)
```

- Points at the **sub-line cell** (`line ≥ 1`, `athlete_authored`) whose text
  parsed into this set. **NULL = structured-logger origin.**
- One field, triple duty: **discriminator** (the structured-logger delete skips
  parsed sets), **de-dup link** (presenters suppress one channel), **idempotency
  key** (`(session_log, source_line)`).
- `LoggedSet.prescription` continues to point at the exercise's **line-0** cell, so
  `key_str` identity, coach `session_results`, and e1RM/PR all keep working (coach
  results only fetch line-0 cells).

**Migration:** additive nullable FK. Number it the next free slot — if the
current-week removal (`0043`) has landed, this is `0044`; otherwise `0043`. **Pure
append, no renumber, no backfill** (existing rows default NULL = structured
origin). *Verify the latest migration on disk before numbering.*

---

## 5. The write hook — `athlete_cell_write` (slice 5a)

After `cell.save(...)` (~`views.py:1550`), inside the existing
`transaction.atomic()`:

1. **Fetch-or-create the athlete's `SessionLog`** for `(session, request.user)`
   (mirror `athlete_log_session`'s newest-first lookup). New parsed sets are
   **PENDING** — a blur is a draft, not a completion. Never downgrade an existing
   DONE log.
2. `parsed = parse_performed(cell.text)`.
3. **Delete-then-recreate, scoped to `(session_log=log, source_line=cell)`** —
   idempotent, no unique constraint needed:
   ```python
   log.sets.filter(source_line=cell).delete()
   if parsed and parsed["kind"] == "set" and (parsed.get("reps") or parsed.get("load")):
       LoggedSet.objects.create(
           session_log=log, prescription=line_zero[exercise_id],
           source_line=cell, set_number=1,
           reps=str(parsed.get("reps", "")), load=str(parsed.get("load", "")),
           rpe=str(parsed.get("rpe", "")),
       )
   ```
   Blank / unparseable / skip / swap / note → the delete runs, nothing recreated
   (mirrors "blank clears the cell").
4. **Idempotency key `(session_log, source_line)`** — re-blur replaces, never
   appends.
5. **Tolerance guard:** the `cell.save` above already committed the raw text;
   wrap the parse+upsert defensively so any unexpected error logs and continues —
   the athlete's text is never lost and the response never 4xx/5xx on a parse
   problem. `parse_performed` being total makes this belt-and-suspenders.
6. **Structured-logger delete scoping** — in `athlete_log_session`, scope its
   `LoggedSet` delete to `source_line__isnull=True` so a structured "Save progress"
   never wipes the freeform-parsed sets (and vice-versa).

Keep the explicit `transaction.atomic()` — `ATOMIC_REQUESTS` is inert on this
project, so the block is load-bearing. No row locking is added.

---

## 6. No double-display (slice 5a)

The only surface that renders both channels for one row is the athlete session
(`presenters.py`). Hydrate the structured `set_rows` (`_set_rows`, ~`presenters.py:1263`)
from `source_line__isnull=True` sets **only** — so a parsed set shows once, as its
sub-line text, never also as a phantom structured input row. `_sub_lines` unchanged.
Coach `session_results` (~862, groups by line-0 `prescription_id`) picks parsed
sets up automatically once they count (§7) — no change.

---

## 7. Records go live + the PR celebration (5a optimistic, 5b confirmed)

There is no "done" button anymore (the concept dissolves into the 24 h settle).
So the **live**, re-derivable reads must count **pending** sets:

- **Relax the DONE-only filter** in `personal_records.py` — `_performed_sets`
  (the query at `personal_records.py:93-95`) and the `new_records_in` DONE gate
  (`:179`). These feed the 4d panel and the PR toast; counting pending makes bests
  **live and self-healing** (a corrected cell just re-derives on next read).
- **Leave the persisted path DONE-only.** `one_rm.py`'s `derive_one_rm_values`
  (`:73`) / `refresh_one_rms` write the persisted `AthleteOneRm` — that is the
  **confirmed/settled** record and stays DONE-only, finalized by the 5b settle.

**Optimistic toast (5a).** On blur, if the just-parsed set beats the athlete's
current **live** best, `athlete_cell_write` returns `new_records` (reuse 4c's
`new_records_in` / `serialize_new_record`, which today only fire from
`athlete_log_session`). The athlete UI shows the same optimistic 🎉. It can
occasionally be a false alarm (a later-corrected set) — that's the accepted trade
for in-the-moment feedback.

**Confirmed celebration (5b).** A `django-q` sweep finds PENDING `SessionLog`s with
no edit for **24 h**, transitions them to **DONE** (the settle — time-based, not a
button), which promotes them into the persisted path (`refresh_one_rms` →
`AthleteOneRm`, and later a `PersonalRecord` snapshot) and fires the **confirmed**
PR notification (push/email). Editing a settled session **re-derives the live
panel** but does **not** re-fire the confirmed notification; whether an edit
re-opens the settle timer is a 5b build detail.

---

## 8. The warning UX (slice 5a)

Lance: *"one textbox; on blur try to parse into structure; if it can't, leave it as
a textbox and show a small warning (maybe a color change)."* The refinement that
keeps it from being noise: **most sub-lines legitimately aren't sets** (skip / swap
/ note). So:

- The `set` / `skip` / `swap` / `note` classifications are **all successful — no
  warning**.
- The warning (a cell color change) fires **only** on the `unresolved-set`
  classification: the text *looks like a logging attempt* (has the digit / `x` /
  `@` shape) but can't resolve into a valid set (`225 x`, `2255x5`).
- Implement as a **derive-on-read `warn` flag** from the same `parse_performed`
  classification (no new column) + a cell CSS class. The athlete's text is never
  altered or rejected.

---

## 9. Phase split

### 5a — parser + silent store + live records + warning (**first PR**)
`parse_performed` + corpus; `LoggedSet.source_line` + migration; the
`athlete_cell_write` upsert (silent parallel, pending); structured-logger delete
scoping; no-double-display; the derive-on-read `warn` flag + cell coloring; relax
the live DONE-only reads; the optimistic toast. **Independently valuable and
low-risk** — records surface off typed text, no scheduler involved.

### 5b — settle + confirmed celebration + logger retirement (**follow-up PR**)
The 24 h `django-q` quiet-period sweep (PENDING → DONE → confirmed notification +
persisted record snapshot); optional persisted `PersonalRecord` table; retire the
structured logger `athlete_log_session` now that the grid textbox is the single
input path. Needs the scheduler and its own product review — do not build in 5a.

---

## 10. Tests (5a; red/green, pinned-corpus style)

- **`parse_performed` corpus** — every row in §3, plus an explicit inversion
  assertion: `parse_prescription("225 x 5")` → `sets=225` vs `parse_performed` →
  `load=225`.
- **Warn classification** — `225 x` and `2255x5` warn; `skip` / `DB pullover` /
  `felt tight` / `225 x 5` do **not**.
- **Upsert / idempotency** — one `LoggedSet` per `(session_log, source_line)`;
  re-blur replaces (not append); blank deletes; unparseable writes no set but keeps
  text (200, not 500); two sub-lines → two rows with distinct `source_line`.
- **Collision** — freeform commit, then structured "Save progress" → the parsed
  set survives (`source_line` not null), structured sets recreated; no dup / raise.
- **No double-display** — `athlete_session` `set_rows` excludes `source_line` sets;
  `sub_lines` shows the text once.
- **Live records** — a PENDING parsed set counts in `personal_records()` and fires
  the optimistic toast; editing/correcting the cell re-derives the best. Keep the
  persisted-path (`AthleteOneRm`) DONE-only tests.
- **Update 4b/4c/4d tests** that pinned DONE-only behavior for the *live* reads
  (`test_personal_records.py`, `test_pr_surface.py`, `test_pr_records_panel.py`).

Run the **full Django suite AND the designer vitest** (the island has Python
source-scraping tests in CI's `build` job): `uv run pytest app/store_project/meso/`
and `npm test` in `frontend/designer/`. The 2 `admin_honeypot` failures are
pre-existing on main.

---

## 11. Risks & gotchas

- **Never block entry** — `cell.save` commits raw text *before* parsing;
  `parse_performed` never raises; the upsert is defensively wrapped. A parse
  failure = no `LoggedSet` + a `warn` flag, never a 4xx/5xx, never lost text. This
  is the spreadsheet-parity non-negotiable.
- **Relaxing DONE-only changes pinned behavior** in 4b/4c/4d for the *live* reads —
  update those tests; do **not** touch the persisted `AthleteOneRm` DONE-only path.
- **4a undo isolation stays intact** — the parsed `LoggedSet` is a separate row
  keyed off the athlete-authored sub-line; coach undo/redo operates on cell
  snapshots + `PlanAction`, never on `LoggedSet`, so it can't revert a parsed set.
  If a sub-line is hard-deleted, `source_line` `SET_NULL`s and the set survives
  with `prescription` still line-0 (existing orphan semantics).
- **Migration numbering** — append after the latest on disk (0043 if the removal
  landed, else 0043 for this); pure append, never renumber (MEMORY gotcha).
- **`ATOMIC_REQUESTS` is inert** — keep the explicit `transaction.atomic()`; no
  `select_for_update` is added here so the SQLite-vs-Postgres caveat doesn't apply.
- **Don't run a JS/TS formatter** on the designer island / athlete JS — match hand
  style.

---

## 12. Sequencing vs the current-week removal

Both slices edit `athlete_cell_write` (the removal deletes an `is_current`
auto-advance line there; 5a adds the parse+upsert). **Land the removal first** —
5a then builds on a cell-write path that no longer carries the pointer. They are
independent in their cores (one removes a pointer, one adds a parse pipeline) and
touch only at that one view — a clean rebase, not a redesign, whichever ships
first. §7's "default program = most-recently-logged" (in the removal doc) depends
on 5a writing a `SessionLog`/`LoggedSet` on every entry, so verify that query
against 5a's write path once both exist.

---

## 13. Branch / commit shape & Definition of Done

**Branch (5a):** `meso/5a-parse-at-commit` (repo convention `meso/<slice>-<name>`).

**Commits** (each green on its own):
1. `test(meso): 5a — pin parse_performed corpus (red)`
2. `feat(meso): 5a — parse_performed (load-first performed parser)`
3. `feat(meso): 5a — LoggedSet.source_line (migration)`
4. `feat(meso): 5a — parse-at-commit upsert + structured-delete scoping`
5. `feat(meso): 5a — no-double-display, live records, optimistic toast, warn flag`

**Process:** red/green, run the Codex review loop before the PR, run the Django
suite **and** the designer vitest before pushing. **Open a PR for human review —
do not merge** (main is unprotected; merging deploys). Note that 5b (settle +
confirmed notification + logger retirement) follows.

**Definition of Done (5a):**

- [ ] Current-week removal merged first (or the shared `athlete_cell_write` conflict
      coordinated).
- [ ] `parse_performed` lands with the full pinned corpus green; the `N×M`
      inversion vs `parse_prescription` asserted.
- [ ] `LoggedSet.source_line` migration applies; additive nullable FK.
- [ ] A freeform sub-line commit writes an idempotent silent `LoggedSet`
      (prescription=line-0, source_line=sub-line), tolerant of unparseable text.
- [ ] Structured logger + freeform channels never clobber each other (collision
      test green).
- [ ] No double-display (rendering test green).
- [ ] Live reads count pending + re-derive on edit; persisted `AthleteOneRm` path
      unchanged (still DONE-only).
- [ ] Optimistic PR toast fires from `athlete_cell_write`; warn flag colors only a
      failed set attempt.
- [ ] Full Django suite green (minus the 2 pre-existing `admin_honeypot`) **and**
      `frontend/designer` vitest green; Codex loop clean.

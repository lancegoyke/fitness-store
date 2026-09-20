# Meso — consolidating the shared-cell logging model

**Status:** review written 2026-09-20 · **read-only — nothing built, nothing
decided** · grounded in a code read at `3f22a5c` (the head of `main` after
[#576]) plus the eleven issues and five fix PRs of 2026-09-19/20 · companion
issue: "Meso logging: consolidate the shared-cell model"
**Owner:** Lance
**Prerequisite reading:** [`parse-at-commit-plan.md`](parse-at-commit-plan.md)
(5a §4–§7, 5b), [`decisions.md`](decisions.md) (the 2026-09-19/20 entries, and
"First-party usage events")

> **What this is.** Eleven bugs were found and mostly fixed in one subsystem
> over one weekend. Every fix was sound, pinned red-then-green, and reviewed.
> But each fix's review turned up two or three more, and two of the newest
> cannot be fixed where they were found. This doc says why, and what the
> options cost. It proposes no code and opens no build.

> **What this is not.** It does not touch the designer or any coach surface
> beyond naming what they do to logged rows. It proposes no rewrite of
> `MesoTable`, the React island, or the agent.

---

## 0. TL;DR

- **The model stores one fact twice.** 5a's decision D-B chose "silent
  parallel, not replace" ([`parse-at-commit-plan.md`](parse-at-commit-plan.md)
  §2): the athlete's freeform text stays the source of truth *and* a derived
  `LoggedSet` is written beside it. Six different actors can move one without
  the other, and the only thing that reconciles them is **value equality of
  re-parsed text** (`parsing.performed_text_shows`, `parsing.py:624`).
- **So identity is reconstructed, never carried.** Whether a line "is showing"
  a set is answered by re-parsing the line's text and comparing three strings
  (`models._line_shows`, `models.py:2345`). That answer is time-varying, and
  every one of the eleven bugs is two places evaluating it at two different
  moments and disagreeing.
- **The typed line's key is wrong by one join.** `_upsert_parsed_set` keys a
  line's set on `(session_log, source_line)` (`views.py:2555`), but a cell is
  already globally unique on `(exercise_slot, week, line)`
  (`models.py:2011`, `unique_cell_slot_week_line`) and the code already
  asserts "a sub-line renders one line of text, so it stands in for ONE
  performance" (`models.py:2366`). The extra `session_log` in the key is what
  a coach's cross-day move breaks — and #572/#574 are exactly that break.
- **`LoggedSet.prescription` carries nothing `exercise_slot` wouldn't.** Every
  consumer reads only `.exercise_id` / `.name`, both of which are properties
  delegating to `exercise_slot` (`models.py:2026-2047`); `prescription_id` is
  used only as a grouping key. The pointer's only distinctive property is that
  a coach undo can hard-delete its target — which is #577.
- **Six live answers to "which log counts", and four reads that ignore soft
  delete.** #568 unified four of them on `-created_at, -pk`; `session_results`
  (`presenters.py:1155`) was not among them. #575 names two reads that skip
  `deleted_at`; there are at least six.
- **Recommendation:** stop adding pointers and predicates; remove them, in
  four stages. Do the one stage that needs no decision from Lance first
  (re-anchor `LoggedSet` to `ExerciseSlot`). The highest-leverage stage —
  narrowing the typed line's key to the cell — is blocked on one product
  question, so ask it now (§7).

---

## 1. The model as it is today

### 1.1 The grid: slots, cells, lines

The P0 fixed-lineup cutover (migration `0037`) split the old per-week
`ExercisePrescription` row into two, and the logging model still carries the
seam. The chain, as `models.py:1230-1250` states it:

```
Plan → Mesocycle → SessionSlot (fixed day)  ─┐
                 → Week ────────────────────┼→ Session (week × day instance)
                 → SessionSlot → ExerciseSlot (fixed row) → Prescription (cell = row × week)
```

- **`ExerciseSlot`** (`models.py:1716`) is the *durable* identity of an
  exercise row. It is block-wide: one slot is shared by every week of the
  mesocycle. It owns `name` and the `exercise` FK. It is **soft**-deleted.
- **`Prescription`** (`models.py:1947`) is a **cell**: one `ExerciseSlot` ×
  one `Week` × one `line` of freeform `text`. `line` 0 is the coach's
  prescription; lines 1+ are the sub-line stack the athlete types into. It is
  unique on `(exercise_slot, week, line)` (`models.py:2011`). It has
  **no `deleted_at`** — deliberately (`models.py:1967`: "a cell lives iff its
  slot *and* its week are both live") — so the only way a cell goes away is a
  **hard** delete, and `restore_plan_snapshot`'s stray-cell purge is the thing
  that does it (`history.py:333-339`).
- **`Session`** (`models.py:1840`) is the (week × day) join row. Logging
  anchors here: `SessionLog.session`.

So a cell's coordinates are `(exercise_slot, week, line)`. Two of those three
are stable under everything a coach does except a delete; the third,
`exercise_slot`, is what a cross-day move re-points (`views.py:5081`).

### 1.2 The two entry paths

| | typed line | structured logger |
|---|---|---|
| endpoint | `athlete_cell_write`, `views.py:2108` | `athlete_log_session`, `views.py:1474` |
| trigger | a sub-line `@blur` | "Save progress" / "Log session" |
| payload | `{exercise_id, line, text}` (+ `owner`) | `{status, sets:[{prescription, set_number, reps, load, rpe, id \| client_id}]}` |
| writes | one `LoggedSet` per line, via `_upsert_parsed_set` (`views.py:2415`) | wholesale delete + `bulk_create` (`views.py:1738`, `1962`) |
| `source_line` | set to the cell | `NULL` |
| sets `DONE` | never | yes |
| carries row identity | **no** — the coordinate `(exercise_id, line)` is all there is | **yes** since #567 — `id` or `client_id` per row |

5a shipped these as two channels on purpose (D-B, "silent parallel, not
replace"), and 5b's second half — retiring the structured logger — is
explicitly waiting on Lance having trained on both
([`decisions.md`](decisions.md), the 5b entry).

### 1.3 `LoggedSet` and its four pointers

`LoggedSet` (`models.py:2579`) has no `deleted_at`, no `UniqueConstraint`, no
`unique_together`, and no partial index. Its `Meta` declares only
`ordering = ["set_number"]`. Compare its siblings, which all have real
constraints: `Prescription` (`unique_cell_slot_week_line`), `Session`
(`unique_session_week_slot`), `Week` (`unique_week_index`). Every idempotency
guarantee in this subsystem is application code.

Its four pointers, and what each is for:

| pointer | `on_delete` | DB FK? | what it means | lifetime hazard |
|---|---|---|---|---|
| `session_log` | `CASCADE` | yes | which log this set belongs to | `SessionLog` has no uniqueness on `(session, athlete)`; the split-log race is documented at `views.py:1538-1543` and prevented only by the `Session` row lock |
| `prescription` | `SET_NULL` | yes | the line-0 cell → the lift's identity | the target is **hard**-deletable by a coach undo → **#577** |
| `source_line` | `SET_NULL` | yes | the sub-line cell whose text parsed into this set; `NULL` = structured origin (`models.py:2600-2609`) | same hard-delete hazard; `history.py:337` spares it |
| `reclaimed_line` | `SET_NULL` | **no** (`db_constraint=False`) | a hint (#541): the sub-line a structured copy *used to* answer to | no referential integrity at all; `history.py:339` spares it |

Two observations worth stating plainly.

**First — `prescription` is redundant.** A census of every read
(`one_rm.py:98`, `personal_records.py:131-132`, `serializers.py:536,639`,
`presenters.py:1162`, `settle.py:200`) shows it is only ever used for
`.exercise_id` / `.name` — both `Prescription` *properties* that delegate
straight to `exercise_slot` (`models.py:2026-2047`) — or as an opaque grouping
key. The `(exercise_slot, week)` pair it stands for is already recoverable from
`session_log.session.week` plus the slot. The one thing `prescription` adds
over `exercise_slot` is that it points at a row a coach can destroy.

**Second — `reclaimed_line` exists to survive the loss of `source_line`.** It
was added by #541 because "Log session" replaces a reclaimed parsed row with a
source-less structured copy, and the copy needed to remember what it used to
be. That is a pointer whose whole job is to carry an identity the model
dropped a moment earlier.

### 1.4 "Hidden", "reclaimed", "displayed" — three words for one predicate

None of these is stored. All three are computed, in Python, by re-parsing text:

```python
def _line_shows(line, logged_set):  # models.py:2345
    """Does ``line``'s current text render exactly this performance?"""
    return parsing.performed_text_shows(
        line.text, reps=logged_set.reps, load=logged_set.load, rpe=logged_set.rpe
    )
```

`performed_text_shows` (`parsing.py:624`) re-runs `parse_performed` on the
cell's text and compares the three result strings to the three stored strings
through `same_logged_set` → `same_logged_value` (`parsing.py:744`, `659`),
which is casefold-equal **or** float-equal **or** duration-equal. That
function's own docstring records the cost of not having had it:

> "Letting each site spell the comparison itself is what produced three
> separate bugs: the same set displayed in both channels, a restored line
> creating a twin row instead of reusing its own, and a reformatted load
> re-firing a PR toast already celebrated."

On top of that sits a one-row-per-line ranking, `line_displays`
(`models.py:2363`): a row pointing at the line via `source_line` outranks a
source-less copy answering through `reclaimed_line`; between copies, the lower
pk wins. `parsed_set_is_hidden` (`models.py:2388`) asks it for one row;
`hidden_parsed_set_pks` (`models.py:2446`) asks it for a whole log.

**The load-bearing part is that this is not only a display rule.**
`parsed_set_is_hidden`'s docstring says so:

> "Hidden means suppressed from every structured surface — `athlete_session`'s
> `set_rows`, `serialize_session_log`, **and therefore also the structured
> logger's replace-delete, which must never touch a row it cannot see.**
> **Define the rule ONCE.** Visibility and that delete have to agree exactly,
> and every time they were expressed separately they drifted."

So "what is on screen" authorises "what a save may destroy". Change the
display rule and you have changed the write rule. §4.6 returns to this.

### 1.5 The state machine a sub-line goes through

A cell at `(exercise_slot, week, line ≥ 1)` and the `LoggedSet` that may back
it. Solid arrows are athlete actions, dashed are coach actions.

```
                    ┌──────────────┐
                    │    EMPTY     │  no text, no row
                    └──────┬───────┘
      athlete types set-shaped text, blurs │  ▲ athlete blanks the line
                           ▼               │  (mine → delete, nothing created)
                    ┌──────────────┐───────┘
          ┌─────────│    PARSED    │  cell.athlete_authored=True
          │         │              │  LoggedSet: source_line=cell, prescription=line0
          │         └──────┬───────┘  line_displays(cell) == this row  →  HIDDEN from set_rows
          │                ╎
          │  coach rewrites the line (cell_line_write, views.py:5202)
          │  → athlete_authored=False, text is now the coach's
          │  → LoggedSet untouched (undo must never write athlete data)
          │                ╎
          │                ▼
          │         ┌──────────────┐
          │         │  RECLAIMED   │  row still source_line=cell,
          │         │              │  but _line_shows() is now False
          │         └──────┬───────┘  →  VISIBLE again as a structured Set row
          │                │
          │   athlete taps "Log session" — the logger reposts the row it can see
          │                ▼
          │         ┌──────────────┐
          │         │    COPIED    │  source_line=NULL, reclaimed_line=cell   (#541)
          │         │              │  a *new* pk: the save deleted and bulk_created
          │         └──┬────────┬──┘
          │            │        ╎
          │  athlete retypes    ╎  coach undoes the rewrite (the text comes back,
          │  the old text       ╎  LoggedSet still untouched)
          │            ▼        ▼
          │     ┌──────────┐  ┌──────────────────┐
          └────►│  PARSED  │  │ DISPLAYED-VIA-   │  read side only: parsed_set_is_hidden
                │ (re-linked│  │ LINK      (#561) │  follows reclaimed_line so the page
                │  same pk) │  │                  │  shows the set once
                └──────────┘  └──────────────────┘
```

Four more states are orthogonal to that walk — a row can be in any of them at
the same time:

| state | how it is reached | what it means |
|---|---|---|
| **SKIPPED** | coach skips line 0 (`prescription_skip`, `views.py:5157`) | `_upsert_parsed_set` bails before touching anything (`views.py:2494`); the row is read-only, deliberately, so a skip can't un-perform work |
| **ELSEWHERE** | coach moves the exercise (`prescription_move`, `views.py:5010`) | the cell travels with the `ExerciseSlot`; the row stays on the old day's log. `sub_line_warn_reason` answers `"elsewhere"` (#572 part 2) |
| **ORPHANED** | coach undo hard-deletes the line-0 cell (`history.py:333`) | `prescription` is `SET_NULL`'d; the row stops counting in 1RM and PRs, which all filter `prescription__isnull=False`. **#577** |
| **STRANDED** | coach soft-deletes the day/week, **or** the row sits on an older split log | invisible on every athlete surface (`_athlete_session_or_404` 404s the session, `views.py:1330`), still counted by 1RM and PRs. **#575** |

### 1.6 Where soft delete stops

`deleted_at` exists on exactly four models: `Week` (`models.py:1807`),
`SessionSlot` (`1687`), `ExerciseSlot` (`1754`), `Session` (`1862`). Each
cascades by hand in its own `soft_delete()` (`models.py:1699`, `1771`,
`1824`) — never via Django's cascade.

It reaches **no athlete data**. `SessionLog` and `LoggedSet` have no
`deleted_at` and no `soft_delete`, and `SessionSlot.soft_delete`
(`models.py:1709-1712`) stamps every week's `Session` without touching a log.
`session_delete`'s own docstring states the intent: "Any `SessionLog`/
`LoggedSet` the athlete already logged are untouched — preserving them is the
point" (`views.py:4542`).

Preserving the *rows* is right. The problem is that nothing downstream knows
they are stranded:

| read | filters `deleted_at`? | filters "newest log"? |
|---|---|---|
| `one_rm.derive_one_rm_values` (`one_rm.py:87`) | no | no |
| `personal_records._live_logged_sets` (`personal_records.py:111`) | no | no |
| `serializers.last_logged_labels` (`serializers.py:624`) | no | no |
| `serializers.serialize_recent_logs` (`serializers.py:525`) | no | no |
| `adherence.link_last_trained` / `link_session_count` / `recent_logs` (`adherence.py:56, 94, 124`) | no (only `Plan.ARCHIVED`) | n/a |
| `presenters.athlete_session` (`presenters.py:1676`) | session is pre-scoped by the view | yes, `-created_at, -pk` |

#575 names the first two. The gap is wider than the issue says.

And it is currently **load-bearing**, which is what makes it hard. `views.py`
carries an explicit instruction not to fix it:

> "a row stranded on a split/older log, or on a soft-deleted day, still counts
> toward the athlete's live 1RM and PRs … A reviewer proposed adding
> `session_log__session__deleted_at__isnull=True` here; that would be WRONG
> for exactly this reason, so don't." (`views.py:3204-3208`)

The reason is #572 part 2: `"elsewhere"` suppresses a duplicate-minting repost
*because* the far row still counts. Remove the second fact and the suppression
becomes wrong. **#575 and #572 have to be decided together.**

### 1.7 Where the offline outbox cuts across

There is no IndexedDB and no Background Sync. The outbox is one `localStorage`
key, `meso-log-queue` (`static/js/meso_athlete.js:670`), holding two record
shapes:

```js
{ id, owner, url: logUrl,  body: { status, sets: [...] } }                 // a session log
{ id, owner, kind: "cell", url: cellUrl, body: { exercise_id, line, text } } // a typed line
```

The service worker never touches writes — `sw.js:82` returns early for any
non-GET, with a comment saying the page owns writes because that is more
reliable on iOS. Flushes are event-driven only: `online`, page `init()`, the
start of every `save()`, and a cell blur's own chained flush. There is no
timer and no backoff.

Three properties of this matter to the model:

1. **A queued typed line carries no identity.** Its body is
   `{exercise_id, line, text}`. A replay is byte-indistinguishable from a
   fresh typing, so the server cannot tell "this landed already" from "the
   athlete did it again" — which is #574 route 1, and why the issue says both
   obvious cures are wrong.
2. **The replay bypasses the client-side gate.** `_postCell`'s `fromQueue`
   branch never consults `_lineNeedsSending`, deliberately, because #527's
   guarantee is that a queued write drains. So #572 part 2's `"elsewhere"`
   suppression — which lives in `_lineNeedsSending` — cannot cover it.
3. **The two paths are scoped differently.** A cell write carries `owner` and
   gets a 409 from `_sent_by_another_account` (`views.py:2384`); `flushLog`
   posts `item.body` verbatim with no `owner` merged in, and
   `athlete_log_session` has no such check at all. The client's
   `isWrongAccount()` treats both the same defensively, but only one end
   implements it.

---

## 2. The invariants the code is trying to hold

Stated plainly, with where each is enforced and whether it holds.

**I1 — One performance is one `LoggedSet`.**
Asserted at `models.py:2366` ("a sub-line … stands in for ONE performance")
and at `models.py:2605` (the `(session_log, source_line)` idempotency key).
Enforced by: `_upsert_parsed_set`'s delete-then-recreate (`views.py:2578`),
the structured logger's twin absorb (`views.py:1816-1866`), `_client_held`
(`views.py:2891`), and the collision renumbering (`views.py:1890`). **Not
enforced by the database at all.** Broken by #541, #567, #572 part 2, #574.

**I2 — What the athlete sees is what counts.**
Enforced by `parsed_set_is_hidden` / `line_displays` and the tint
(`sub_line_warn_reason`, `models.py:2465`). Broken in both directions: by #561
(shown twice, one row) and by #575/#577 (counted but invisible, or visible but
uncounted).

**I3 — A coach's edit never changes athlete data.**
Enforced by `history.py:20-22` (snapshots exclude `SessionLog`/`LoggedSet`/
`AthleteOneRm`), by `Prescription.athlete_authored` (`models.py:2004`) keeping
athlete cells out of the snapshot, by `cell_line_write`'s own comment
(`views.py:5284`), by `prescription_move`'s (`views.py:5025`), by
`prescription_skip`'s (`views.py:5185`), and by the stray-cell purge's three
exclusions (`history.py:337-339`). **Holds for the rows; fails for what the
rows mean** — a coach rewrite changes the text a row is hidden by, a move
changes which day backs it, and an undo can NULL its lift (#577).

**I4 — A write the client believes landed did land.**
Enforced by the write-ahead outbox (#527/#529), by the 503-not-200 poisoned
answer (#571, `views.py:2308`), by the retryable/refusal split in
`flushLog`/`flushCell`, and by the `set_rollback(True)` refusal (#570,
`views.py:1939`). This is the one invariant that a fix actually closed as a
class — see §3.

**I5 — A coach's plan edit must not destroy earned work.**
Enforced by `prescription_skip` declining to delete (`views.py:5185-5195`), by
`_upsert_parsed_set`'s skip bail (`views.py:2494`), by the replace-delete
sparing rows it cannot account for, and by the purge exclusions. Broken by
#577.

**I6 — Every surface answers "which log counts" the same way.**
Asserted by #568, which put `-created_at, -pk` on four reads
(`presenters.py:1676`, `views.py:1566`, `views.py:3176`, `settle.py:113/177`).
**Not held:** `session_results` (`presenters.py:1155`) orders `-date,
-created_at`, and five other reads span every log rather than picking one
(§1.6). See §9.

---

## 3. The eleven bugs, mapped

| # | broke | fix PR | merge | closed a class, or an instance? |
|---|---|---|---|---|
| **527** a typed set logged offline is lost, page says saved | I4 | [#529] | `3bfd4da` | **class** — every line write is now written ahead before the request, owner-scoped, flushed on `online`/`init`/`save` |
| **541** restoring a reclaimed line after "Log session" duplicates the set | I1 | [#563] | `93ea404` | **instance** — added `reclaimed_line` (migration `0048`) so the restore lookup can find the source-less copy. A fourth pointer, added to rescue an identity the model had just dropped |
| **561** a coach undo after "Log session" shows the reclaimed set twice | I2 | [#569] | `ec072bb` | **instance**, read-side only. Added `line_displays`' ranking. No migration. The entry itself records what it does not cover: a copy made before #541 has no link |
| **567** "Log session" matched a stale payload by set number | I1 | [#573] | `1e4fa15` | **class, on one path.** Row identity (`id` / `client_id`) now rides in the structured payload. Does not reach the typed line, and `client_id` is **never stored** (`views.py:1465`) — it only round-trips |
| **568** the warn fallback query wasn't scoped to a log | I6 | [#573] | `1e4fa15` | **instance + a decision.** Scoped the query to the cell's own day, and decided the move case ("a set on the old day reads as unlogged"). That decision created #572 |
| **570** the collision renumbering had no ceiling | I1 | [#576] | `3f22a5c` | **class** — three disagreeing constants unified on `MAX_LOGGED_SET_NUMBER` (`models.py:2576`), the walk bounded, a refusal made honest. The review found a **fourth** unbounded writer the first version had missed |
| **571** a swallowed DB failure returned 200 on a rolled-back write | I4 | [#576] | `3f22a5c` | **class, for this call site.** `needs_rollback` captured inside the block; 503 instead of a silent 200; the dead `ATOMIC_REQUESTS` line deleted rather than moved, precisely so the class isn't re-armed everywhere |
| **572 pt 2** a tinted line reposts itself into a second row | I1 | [#576] | `3f22a5c` | **instance.** `warn` became a typed `warn_reason`; the client suppresses only `"elsewhere"`. The PR's own text says the gate cannot cover the queued or forced post → **#574** |
| **572 pt 1** a cross-day move tints every logged week of the block | I2 | — | — | **open.** A product decision, not a bug fix |
| **574** a queued or forced re-post still mints a second row | I1 | — | — | **open.** Both local cures rejected in the issue: refusing server-side would also refuse a genuine re-performance; suppressing the replay breaks #527 |
| **575** stranded rows still feed 1RM and PRs | I2 | — | — | **open,** and blocked: the behaviour is load-bearing for #572 part 2 (`views.py:3204-3208`) |
| **577** the undo purge NULLs `LoggedSet.prescription` | I3, I5 | — | in flight | **open.** The purge spares two of three pointers. A one-line `.exclude(logged_sets__isnull=False)` closes the instance; the third pointer being destroyable at all is the class |

Read down the "closed a class" column. Four entries say class — #527, #570,
#571, and half of #567. **All four are about the write *mechanism*:** queueing,
bounding, transactions, payload shape. Every entry about the *model* — what a
row is, which line owns it, what a coach edit means for it — closed one
instance and added a pointer, a predicate, or a special case.

That is the shape of the problem. The mechanism is converging. The model is
not.

---

## 4. Why the rate isn't falling

[#573]'s own body already sketched six causes ("Why this keeps happening").
This section confirms or rejects each of the candidates against the code at
`3f22a5c`, and adds two.

### 4.1 Identity is inferred rather than carried — **confirmed, with a refinement**

Not "not carried at all": #567 *did* carry it, on one path. The refinement is
that identity is carried in three different strengths and stored in none.

- **Structured path:** each posted row carries `id` or `client_id`. But
  `views.py:1465` states it plainly — "A client-minted `client_id` (#567) is
  never stored — it only round-trips." So the identity survives one request,
  not a replay from a new page load.
- **Typed path:** the payload is `{exercise_id, line, text}` — no identity at
  all. Its key *is* a coordinate, and that coordinate resolves server-side to
  a `Prescription` pk that a coach can move (`prescription_move`) or hard-delete
  (`history.py:333`).
- **The line↔set relation on both paths:** inferred, by re-parsing text and
  comparing values (`_line_shows`, `models.py:2345`).

The structured path's positional fallback is still live and documented as
byte-identical to the pre-#567 behaviour for any untagged payload — an
installed PWA on a stale cache, or a tab open since before the deploy.

**Verdict: confirmed.** It is the deepest cause, and it is why the codebase
now has a `same_logged_value` that treats `BW`/`bw`, `8`/`8.0` and `60s`/`1m`
as the same thing (`parsing.py:659`) — a heuristic standing where a key
belongs.

### 4.2 One row is reachable through three pointers with different lifetimes — **confirmed, and it is four**

`session_log` (CASCADE, required), `prescription` (SET_NULL, real FK),
`source_line` (SET_NULL, real FK), `reclaimed_line` (SET_NULL, *no* DB
constraint). Four targets, four lifetimes, and the code has to enumerate them
by hand wherever they matter.

The clearest cost is `restore_plan_snapshot`'s purge, which must list every
pointer that makes a cell worth sparing:

```python
).exclude(pk__in=cell_pks).exclude(athlete_authored=True).exclude(
    parsed_sets__isnull=False
).exclude(reclaimed_sets__isnull=False).delete()      # history.py:337-339
```

That chain grew by one exclusion in #541 and is missing one today (#577). It
is a hand-maintained list of "the ways athlete data can point at a cell",
which is exactly the sort of list that drifts.

**Verdict: confirmed.** And §1.3 shows one of the four is removable outright.

### 4.3 Reads and writes each re-derive "which log counts" separately — **confirmed, and it is broader than reads vs writes**

Six distinct rules are live:

| rule | sites |
|---|---|
| newest by `-created_at, -pk` | `presenters.py:1676`, `views.py:1566`, `views.py:3176`, `settle.py:113`, `settle.py:177` |
| newest DONE by `-date, -created_at` | `presenters.py:1155` (coach results) |
| **all** logs, DONE only | `one_rm.py:88`, `serializers.py:626` |
| **all** logs, any status | `personal_records.py:111`, `serializers.py:525` |
| all logs **except this session** | `presenters.py:1762`, `views.py:3209` (`elsewhere_sets`) |
| any log with ≥1 set | `presenters.py:2344`, `presenters.py:2401` (analytics cohorts) |

The question is only meaningful because `SessionLog` has **no uniqueness on
`(session, athlete)`**. The race that creates a second one is documented at
`views.py:1538-1543` and is prevented only by the `Session` row lock — which
means it was reachable before that lock, and #575 confirms such rows exist.

**Verdict: confirmed.** The `elsewhere_sets` asymmetry is the sharp end:
[`decisions.md`](decisions.md) explains that it deliberately reads *any* log,
unlike `backing_sets`, because the other reads count stranded rows. One rule
is now written in terms of another rule's bug.

### 4.4 Soft delete doesn't reach athlete data — **confirmed, and it is deliberate today**

Confirmed in §1.6. The refinement is that this is no longer a simple omission:
since #572 part 2 shipped on 2026-09-20, `"elsewhere"` depends on stranded rows
counting, and `views.py:3204-3208` instructs the next reader not to add the
filter. A correct-looking one-line fix to #575 would re-open #572.

**Verdict: confirmed, and entangled.**

### 4.5 The offline replay bypasses the rules the live path applies — **confirmed**

`_postCell`'s `fromQueue` branch never consults `_lineNeedsSending`, by design
(#527's drain guarantee). The queued body carries no identity. So every gate
the live path grew in #572 part 2 is invisible to the replay, which is #574
route 1 verbatim — and route 2 (a trailing-space edit) shows the gate is
bypassable from the live path too.

**Verdict: confirmed.** Note the direction: the replay isn't *wrong* to drain.
The rule it bypasses is a client-side patch for a server-side ambiguity.

### 4.6 A display predicate is load-bearing for write correctness — **added**

`parsed_set_is_hidden`'s docstring makes the fusion explicit: hidden means
"suppressed from every structured surface … **and therefore also the
structured logger's replace-delete**", and "Define the rule ONCE. Visibility
and that delete have to agree exactly."

Defining it once was the right call given the two channels. The consequence is
that **any change to what the page shows is a change to what a save may
destroy.** #572 part 2 is that consequence in one sentence: #568 changed a
*tint*'s scope, and the result was a duplicated `LoggedSet`. Three write sites
had to move with the display rule in #561 alone (the replace-delete, the twin
absorb, the collision renumbering).

### 4.7 The same fact is stored twice, and reconciled by a heuristic — **added; this is the root**

5a chose D-B, "silent parallel (not replace)": the freeform text is truth, the
`LoggedSet` is a derivative ([`parse-at-commit-plan.md`](parse-at-commit-plan.md)
§2). That is two writable copies of one fact. Six actors can move one without
the other:

| actor | moves the text | moves the row |
|---|---|---|
| athlete types into the line | yes | yes (`_upsert_parsed_set`) |
| athlete uses the structured logger | no | yes (wholesale replace) |
| coach rewrites the line (reclaim) | yes | **no** (I3) |
| coach undoes | yes | **no** (I3) — but can NULL a pointer (#577) |
| coach moves the exercise | the cell changes day | **no** (I3) |
| coach skips the row | no | **no** (deliberately, `views.py:5185`) |

The only thing that re-couples them is value equality of re-parsed text. Four
of the six rows in that table are a coach action that moves one copy and, by
design, not the other — and I3 says that design is correct. So the divergence
is not a bug to be fixed; it is a property of the model.

**Everything in §4.1–§4.6 is downstream of this.** Inferred identity is what
you use when you have two copies and no key. Extra pointers are what you add
when a copy loses its partner. Re-derived scopes are what you get when every
consumer must decide for itself which copy is real.

### 4.8 What the numbers look like

Not proof of anything, but the shape is consistent:

- `athlete_log_session`, `athlete_cell_write` and `_upsert_parsed_set` are
  **1,266 lines** with **714 comment lines** (56%), excluding docstrings. Most
  of those comments are a paragraph recording one bug and why the obvious fix
  was wrong.
- **Seven test files named after individual bugs**, all created 2026-09-19/20,
  totalling **2,991 lines**: `test_reclaim_restore_after_log.py`,
  `test_undo_after_log_session.py`, `test_log_row_identity.py`,
  `test_set_number_ceiling.py`, `test_warn_read_transaction_placement.py`,
  `test_warn_reason_after_move.py`, `test_poisoned_transaction_cell_write.py`.
- `PWA_CACHE_VERSION` went **v4 → v7** in two days (#529, #573, #576) — three
  forced cache drops, each because the response or payload shape changed.
- Of the eleven, **four are still open**, and the two newest (#574, #575) both
  say in their own bodies that the local fix is wrong.

---

## 5. Options

Five, ordered by how much they change. They compose: B is a subset of C, and D
subsumes most of both.

### Option A — Keep patching

Fix #577 (in flight), then #574, then #575, then decide #572 part 1.

- **Fixes:** each instance, one at a time.
- **Costs:** nothing up front. No migration.
- **Risks:** #574 and #575 both state that the local fix is wrong — #574's
  server-side cure would refuse a genuine re-performance (real data loss);
  #575's one-line filter would re-open #572 part 2. So "patching" here means
  first answering the same product question the options below force. The
  measured rate (§4.8) is the other risk.
- **Migration/backfill:** none.
- **The honest case for it:** every fix so far has been sound, pinned
  red-then-green and reviewed; the subsystem is now heavily tested; and the
  bug that would hurt most (a lost write, I4) is the one class that *has*
  converged. If Meso's athlete population is still small enough that a
  duplicated set is a support conversation rather than a data problem,
  spending the time on the two open product decisions and nothing else is
  defensible.

### Option B — Store the identity that is already being carried

Persist `client_id` on `LoggedSet` (unique per `session_log`); mint one on
both paths; keep it in the outbox entry so a replay names the same row.

- **Fixes:** #574 (both routes — a replay names a row rather than describing
  it). Lets `_client_held`'s stale-id rule and the positional fallback retire
  once old caches age out.
- **Costs:** an additive nullable column plus a partial unique index; a
  `PWA_CACHE_VERSION` bump; one release of dual-path handling for untagged
  payloads (the pattern #567 already used).
- **Risks:** low. The main one is that it adds a **fifth** identifier to a
  model that already has four pointers and `set_number`.
- **Migration/backfill:** additive; no backfill (existing rows NULL).
- **Caveat that shrinks this option:** for the *typed* path, a per-row
  `client_id` would be redundant. A sub-line cell is already unique on
  `(exercise_slot, week, line)` and already stands for exactly one
  performance. The cell **is** the identity. That observation is Option C.

### Option C — Re-anchor, and narrow the key

Two changes, independently shippable.

**C1 — anchor to durable identity.** Replace `LoggedSet.prescription` with
`LoggedSet.exercise_slot`. Backfill `exercise_slot_id =
prescription.exercise_slot_id`; keep `prescription` shadowed for one release,
then drop it.

- **Fixes:** #577 as a *class* — the identity pointer no longer targets a
  hard-deletable row, so an undo cannot silently stop a set counting. Removes
  the `prescription__isnull=False` filters in `one_rm.py:90`,
  `personal_records.py:114`/`238` and `settle.py:200`. Shrinks
  `history.py`'s hand-maintained exclusion list.
- **Costs:** one migration, one backfill, and touching every read that spells
  `ls.prescription.exercise_id` / `.name` — all of which already go through
  `exercise_slot` one hop further (`models.py:2026-2047`), so the diff is
  mechanical.
- **Risks:** rows already NULLed by #577 have no `prescription` to backfill
  from. Some are recoverable via `source_line.exercise_slot_id` /
  `reclaimed_line`; the rest should be **reported, not guessed**.
- **Migration/backfill:** yes, both. Additive then subtractive, two releases.

**C2 — narrow the typed line's key from `(session_log, source_line)` to
`source_line`.** Add a partial unique index where `source_line IS NOT NULL`;
turn `_upsert_parsed_set`'s delete-then-recreate into a real upsert on that
key.

- **Fixes:** #574 (both routes) and #572 part 2's residue *without* the
  client-side gate — a replay or a forced post finds the existing row wherever
  it lives, including on another day's log. Makes `elsewhere_sets` the
  ordinary lookup instead of a special case, which in turn makes #572 part 1 a
  pure display question.
- **Costs:** a dedupe backfill, and it **forces** the #572 part 1 decision:
  when a cell's day changes, does its row move to the new day's log, or stay?
- **Risks:** the constraint is violated by exactly the data that is buggy
  today, so the backfill needs a "which row wins" policy. And a DB
  `IntegrityError` raised inside `_upsert_parsed_set`'s swallow-everything
  savepoint would create nothing and report success — so the upsert must
  become a real `update_or_create` on the key, not a create that happens to
  collide.
- **Migration/backfill:** yes, both, and the backfill is the hard part.

### Option D — Retire the structured logger (5b's deferred half)

[`parse-at-commit-plan.md`](parse-at-commit-plan.md) §9 already schedules
this; [`decisions.md`](decisions.md)'s 5b entry says it waits on Lance having
trained on both paths.

- **Fixes:** `source_line` is never NULL, so `reclaimed_line` has no reason to
  exist; `line_displays`' ranking collapses to "the row whose `source_line` is
  this cell"; `_client_held`, `_names_live_row`, the twin absorb, the
  collision renumbering and the `set_number` numbering-space collision all go
  away. That is most of the 1,266 lines in §4.8, and the majority of the
  eleven bugs' surface area.
- **Costs:** a product decision; a migration for existing source-less rows
  (convert to sub-lines, or keep as read-only history); and whatever the typed
  path cannot currently express — note §3 of the parse plan pins
  **one set per line**, multi-set-per-line explicitly out of scope.
- **Risks:** irreversible in practice once athletes stop seeing the grid. If
  the structured grid turns out to be the better logging UX, this is the wrong
  direction and C is wasted only partially.
- **Migration/backfill:** yes.

### Option E — Derive on read, store nothing

Delete the parsed `LoggedSet` entirely; compute `(reps, load, rpe)` from the
cell's text at read time in the four consumers.

- **Fixes:** the whole hidden/reclaimed/displayed machinery, because there is
  only one copy of the fact.
- **Why it is rejected here:** it destroys earned work. The moment a coach
  reclaims a line, the athlete's performance would vanish with the text — which
  is precisely what `reclaimed_line` was added to prevent (#541). Preserving it
  means materialising a copy at reclaim time, which is Option C wearing a
  different hat. Listed so it is not re-proposed.

---

## 6. Recommendation

**Do C1 now. Ask the two questions in §7 now. Do C2 as soon as the move
question is answered. Treat D as the destination and B as unnecessary once C2
lands.**

The reasoning:

1. **C1 is the only step that needs nothing from Lance.** It is one additive
   migration, one mechanical backfill, a diff that is mostly `.prescription.`
   → `.exercise_slot.`, and it closes #577's *class* rather than its instance.
   It also removes the need for the `prescription__isnull=False` filter that
   makes #577 silent in the first place. There is no product question hiding
   in it.
2. **C2 is the highest-leverage change, and it is blocked on one question.**
   Narrowing the key to the cell is a small schema change with a large blast
   radius: it closes #574 by construction, retires #572 part 2's client gate,
   and reduces #572 part 1 to "what should the tint say", which is a display
   choice rather than a data-loss risk. It cannot be designed without knowing
   whether a moved exercise carries the athlete's rows.
3. **B collapses into C2.** A new `client_id` column would give the typed path
   an identity it already has. Adding a fifth identifier to a model with four
   pointers is the move that got us here.
4. **D is the destination, but it is a product call, not an engineering one.**
   If Lance decides the typed line wins, most of this subsystem deletes itself
   and C2's key becomes the model's only key. If both paths stay, C1+C2 are
   worth doing anyway, and D's cost only grows.

**What I would do first, concretely:** C1, on its own branch, red-then-green,
with the backfill written as a data migration that *reports* unrecoverable
NULL-prescription rows rather than guessing at them — and with a production
count taken first, since #577 asks whether any already exist.

**A staged path for the recommendation**

| stage | change | needs a decision? | migration | closes |
|---|---|---|---|---|
| 0 | `.exclude(logged_sets__isnull=False)` in the purge chain | no | no | #577 (instance) — in flight |
| 1 | **C1** — `LoggedSet.exercise_slot` replaces `prescription` | no | additive + backfill, then drop | #577 (class); removes one of four pointers |
| 2 | **C2** — key the typed line on the cell; real upsert | **yes** (§7 Q2) | partial unique index + dedupe backfill | #574; #572 part 2's residue; makes #572 pt 1 cosmetic |
| 3 | one selector for "the athlete's logged sets", used by all six consumers; decide soft-delete and stranded-log policy once | **yes** (§7 Q3) | no | #575 (and the four reads it doesn't name); I6 |
| 4 | **D** — retire the structured logger; drop `reclaimed_line`, the ranking, the absorb, the renumbering | **yes** (§7 Q1) | yes | most of what is left |

Stages 1 and 3 are independent of each other. Stage 2 should not start before
Q2 is answered, and stage 4 should not start before Q1 is.

---

## 7. Questions only Lance can answer

**Q1 — Do both logging paths stay?**
5b deferred retiring `athlete_log_session` until you had trained on both. Has
that answered itself? If the typed line wins, stage 4 deletes more code than
every fix this weekend added. If both stay, `reclaimed_line`, the
one-row-per-line ranking and the `set_number` numbering-space collision are
permanent and should be designed for rather than patched around.

**Q2 — When a coach moves an exercise to another day, do the athlete's logged
sets follow it?** (#572 part 1, and the upstream fix for #574.)
Three answers, all defensible:
- **They follow.** The set is about the exercise, and the exercise moved. This
  contradicts I3 as literally stated (a coach edit writes athlete data) — but
  the write is a re-parent, not a change to what was performed.
- **They stay, and the tint learns to say so.** The work happened on Monday;
  Monday keeps it. The line on the new day says "logged Monday" rather than
  "not logged".
- **They stay, and the tint is scoped to `(exercise_slot, week)` instead of to
  the day** — which makes the question disappear, because the cell's identity
  never changed.

Whichever you pick decides stage 2's `session_log` semantics and closes #574.

**Q3 — Should a set on a day you deleted still count toward the athlete's 1RM
and PRs?** (#575, which cannot be decided without this.)
Today it does, and #572 part 2 now depends on it doing so. The sub-question:
is a soft-deleted day "this never happened" or "I removed it from the plan,
the athlete still trained it"?

---

## 8. Deliberately out of scope

- The designer, `MesoTable`, the React island, keyboard nav, drag reorder.
- The agent, beyond noting that `serialize_recent_logs` (`serializers.py:525`)
  is one of the reads with its own answer to "which log counts".
- Billing, analytics, push, the tour.
- `#562` (lock-order deadlock between athlete line save and plan undo) and
  `#559` (cascade delete ordering) — same files, different problem
  (concurrency, not identity).
- `#524` (a coach's sub-line renders under "what you did") and `#535` (offline
  queue follow-ups) — real, open, adjacent; neither is a model problem.

---

## 9. One new finding, filed separately

**`session_results` is the one "newest log for this (session, athlete)" read
that #568 did not bring onto the shared rule.** It orders `-date,
-created_at` (`presenters.py:1155`), with no `-pk`, while `athlete_session`,
`athlete_log_session`, `_cell_warn_reason_or_blank` and the settle sweep all
order `-created_at, -pk`. `SessionLog.date` is athlete-supplied
(`views.py:1593-1596` accepts an explicit date), so where two logs exist for
one `(session, athlete)` — the split-log state #575 says is real — the coach's
results screen and the athlete's own page can read different logs.

Reachable only on split logs, and not reproduced. Filed as its own issue and
linked from the consolidation issue; it belongs in stage 3, not on its own.

**Also worth noting, without a separate issue:** #575 names two reads that
skip `deleted_at`. There are at least six (§1.6) — `serializers.py:525` and
`:624`, and all three `adherence.py` queries, have the same gap, and the
adherence one means a logged session on a deleted week still drives the
roster's recency. #575's scope should be widened rather than duplicated.

---

## Appendix — evidence index

| claim | where |
|---|---|
| a cell is `(exercise_slot, week, line)`, unique | `models.py:2011` |
| `Prescription` has no `deleted_at`, deliberately | `models.py:1967` |
| `LoggedSet` has no unique constraint of any kind | `models.py:2643-2646` |
| `prescription` is only ever read for exercise identity | `one_rm.py:98`, `personal_records.py:131`, `serializers.py:536,639`, `presenters.py:1162`, `settle.py:200` |
| `.name`/`.exercise_id` delegate to `exercise_slot` | `models.py:2026-2047` |
| `reclaimed_line` has no DB constraint | `models.py:2633-2641` |
| identity is value-equality of re-parsed text | `models.py:2345`, `parsing.py:624`, `659`, `744` |
| hiding authorises the replace-delete | `models.py:2388-2404` ("Define the rule ONCE", `:2395`) |
| the purge's hand-maintained pointer list | `history.py:333-339` |
| undo must never touch athlete data | `history.py:20-22` |
| the split-log race, and the lock that prevents it | `views.py:1538-1543` |
| `client_id` is never stored | `views.py:1465` |
| don't add the `deleted_at` filter (and why) | `views.py:3204-3208` |
| the move decision | `models.py:2531-2540`, [`decisions.md`](decisions.md) #568 entry |
| the outbox is `localStorage`, two record shapes | `static/js/meso_athlete.js:122-127`, `:670` |
| the SW never intercepts writes | `templates/meso/sw.js:82` |
| the replay skips the client-side gate | `_postCell`'s `fromQueue` branch |
| `MAX_LOGGED_SET_NUMBER` now shared | `models.py:2576` |
| CI skips docs-only PRs | `.github/workflows/django.yml:15-17, 35-37` |

[#529]: https://github.com/lancegoyke/fitness-store/pull/529
[#563]: https://github.com/lancegoyke/fitness-store/pull/563
[#569]: https://github.com/lancegoyke/fitness-store/pull/569
[#573]: https://github.com/lancegoyke/fitness-store/pull/573
[#576]: https://github.com/lancegoyke/fitness-store/pull/576

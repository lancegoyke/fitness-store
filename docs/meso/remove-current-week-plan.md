# Meso — remove the current-week (`is_current`) concept

**Status:** planned 2026-07-18 · **IMPLEMENTED 2026-07-18** (branch
`meso/remove-current-week`) · both product decisions RESOLVED 2026-07-18 (§4a
cadence-based compliance; §4b persist the viewed block on the agent batch) ·
migrations `0043` (RemoveField) + `0044` (agent-batch block FK)
**Owner:** Lance
**Supersedes:** the "Meso designer cleanup" MEMORY guardrail ("never delete
`is_current`/Make-current — it's the load-bearing athlete live-week pointer").
That guardrail predates this decision and no longer binds — call it out in the PR
body so a future reviewer isn't tripped by the stale memory.

Meso programs are **date-less**. A program is a grid of blocks/weeks with no
calendar anchor, so "which week is it right now?" is not derivable — the app has
been carrying a hand-maintained per-plan boolean (`Week.is_current`) that the
athlete's logging auto-advances and the coach can override with a "Make current"
button. That pointer is the babysat state the spreadsheet-parity initiative keeps
trying to delete: it must be advanced, mirrored on duplicate, guarded on delete,
snapshotted for undo, and it drives UI that *tells the athlete which week they are
on* — which is exactly what Lance has decided the app should stop doing. **The
coach communicates the schedule out-of-band** ("Day 1 = Mondays"); the app just
shows the program and lets the athlete navigate the grid freely, defaulting to
the program they last engaged with. This doc removes the concept end-to-end.

This is a **design + sequencing** doc. Two of the changes are genuine redesigns
that need Lance's sign-off before implementation (§4); the rest is a large but
mechanical removal grounded in a full surface audit.

---

## 0. TL;DR

- **Drop `Week.is_current`** (field + migration `0043` `RemoveField`). No DB
  constraint or index to drop — the single-flag invariant was procedural only.
- **`current_week()` collapses to its existing fallback** — explicit `week` wins,
  else earliest live week `(mesocycle.order, index)`. ~9 coach-side callers keep
  working unchanged; consider a later rename to `default_week`/`opening_week`
  (deferred, mechanical ripple through ~6 files).
- **Delete the machinery:** `Week.advance_current_week`, both auto-advance call
  sites, the coach `week_set_current` endpoint + URL + its undoable `PlanAction`,
  the delete-guard that blocks removing the current week, and every `current`
  flag on chips/grid/deliver payloads (backend + the designer React island).
- **Athlete home stops telling the athlete their position** — no "This week"
  heading, no "Week N" label, no current-chip fill, no "Start next week" nudge.
  Card opens to a derived last-engaged position (no new storage), rendered as
  neutral scroll-restore, not a "you are here" claim.
- **Two REDESIGNS (decided 2026-07-18):** (4a) the coach compliance meter → a
  **cadence** signal (recency "last trained N days ago" + "N sessions in 14d"), not
  a per-week %; (4b) the agent grounds on the *block the coach is viewing*, passed
  from the UI and **persisted on the batch** — a plain earliest-live fallback would
  silently scope the agent to block 1.

Removal touches: `models.py`, `serializers.py`, `presenters.py`, `views.py`,
`urls.py`, `adherence.py`, `history.py`, `admin.py`, `sheet_import.py`,
`factories.py`, `agent/service.py`, `agent/validation.py`, `agent/apply.py`,
`management/commands/seed_meso_demo.py`, `templates/meso/athlete_home.html`,
`templates/meso/athlete_profile.html`, new `migrations/0043_*.py`; frontend
`hooks/useGrid.ts`, `hooks/useTableReorder.ts`, `hooks/useAgentChat.ts`,
`lib/api.ts`, `lib/grid.ts`, `DesignerRoot.tsx`, `components/MesoTable.tsx`, plus
type defs and vitest fixtures.

---

## 1. What `is_current` does today

The pointer is `Week.is_current` (`models.py:1661`) — a per-week boolean, at most
one true per plan, **not** enforced by any DB constraint (Week's only constraint
is `unique_week_index` on `(mesocycle, index)`). Every reader therefore takes
"first flagged in plan order, else a fallback." It moves two ways: the athlete's
own logging **auto-advances it forward** (`Week.advance_current_week`,
`models.py:1703`, fired from `athlete_log_session` `views.py:1413` and
`athlete_cell_write` `views.py:1552`), and the coach's manual **"Make current"**
(`week_set_current`, `views.py:2888`, moves it either direction).

The shared resolver **`current_week(plan, week=None)`** (`serializers.py:621`) is
the whole default surface: explicit `week` → else the flagged live week → else the
earliest live week. It is imported by `views.py`, `presenters.py`,
`agent/apply.py`, and `agent/validation.py`.

Five surfaces *consume* the pointer, and they differ in kind — which is why the
removal isn't uniform:

1. **Which week the athlete home opens to**, plus everything it labels — heading,
   "Week N", current-chip fill, current-column grid highlight, "Start next week"
   nudge (`presenters.athlete_home` `presenters.py:1063`;
   `templates/meso/athlete_home.html`). *Communicates a position.*
2. **Which block the designer/deliver/grid open to**
   (`_default_grid_mesocycle` `views.py:2804`; `deliver_plan` `views.py:3610`;
   `api_mesocycle_grid` `views.py:2820`). *Pure default — falls through to
   earliest live week already.*
3. **Which block the agent programs** (`serialize_agent_block` `serializers.py:983`
   → grounding; `clean_change` `validation.py:221` → validation; `_apply_deload`
   `apply.py:177` → apply). *Derives a scope, three times, across time-separated
   requests.*
4. **The coach compliance meter + athlete-profile "training now"**
   (`adherence.link_current_week` `adherence.py:23`, `link_compliance`
   `adherence.py:64`; `presenters.profile_program` `presenters.py:212`). *Derives
   a measurement.*
5. **The coach "Make current" control** and the auto-advance that fights it
   (`week_set_current`; `advance_current_week`). *The babysitting itself.*

Sites that only *default* (2) collapse silently to earliest-live. Sites that
*communicate* (1, 5) get deleted. Sites that *derive* (3, 4) need real
replacement logic — those are the redesigns.

---

## 2. The removal — per-area disposition

Legend: **DELETE** (gone, nothing replaces it) · **FALLBACK** (relies on
`current_week()` degrading to earliest-live, no new logic) · **REDESIGN** (needs
replacement logic; the two blocking ones are called out in §4).

Latest migration on disk **and prod** is `0042_prescription_athlete_athored`
(verified: `migrations/` ends at 0042). Removal appends **`0043`** — a pure
append, **do not renumber**.

### 2.1 Model / field / migration

| Site | Disposition |
|---|---|
| `models.py:1661` `is_current = BooleanField` | **DELETE** field + add migration `0043` `RemoveField` |
| `models.py:1703-1789` `Week.advance_current_week` (whole method) | **DELETE** — auto-advance concept is gone |
| `models.py:1295` `Plan` scaffold/materialize `is_current=True` kwarg | **DELETE** |
| `models.py:1316-1319` `Plan.duplicate_for` docstring "mirrors `is_current`" | **DELETE** the sentence |
| `models.py:1378` `Plan.duplicate_for` `is_current=week.is_current` mirror | **DELETE** the line |
| `models.py:1449-1452, 1476` `Mesocycle.append_week` `is_current=False` + docstring naming `week_set_current` | **DELETE** kwarg; rewrite docstring to "the new week is live and visible at once" |

### 2.2 Resolver `current_week()` — `serializers.py:621-641`

**Keep the function; delete only the `is_current` scan (lines 638-640).** It
degrades to "explicit `week` → else earliest live week." Every downstream caller
keeps working with no signature change — this is the linchpin that keeps the
removal mechanical. Rewrite the docstring (it currently asserts "the week the
athlete is on"). Optional follow-up: rename to `default_week`/`opening_week`
(ripples through ~6 files) — **defer** to keep this PR mechanical.

### 2.3 Athlete home — `presenters.py` + `templates/meso/athlete_home.html`

| Site | Disposition |
|---|---|
| `presenters.py:1145` anchor `next((w … w.is_current), plan_weeks[0])` | **REDESIGN** — see §5 (derive last-engaged position; render neutral, no "you are here") |
| `presenters.py:1204, 1222` `_week_chip_groups` `"current": w.is_current` | **DELETE** the `current` key; keep `focused` (the viewed column) |
| `athlete_home.html:23` `<h1>This week</h1>` | **REDESIGN — FLAG** neutral heading (program title / "Your training") |
| `athlete_home.html:130` `{{ plan.block }} · Week {{ plan.focus_index }}` | **REDESIGN — FLAG** drop the "Week N" position claim |
| `athlete_home.html:143-146, 160, 223, 232` current-chip fill + current-column grid highlight (`w.current`/`c.current`) | **DELETE** the `current`-conditioned styling; keep `focused`; every chip becomes a plain `?week=` link |
| `athlete_home.html:195-197` "Start next week" nudge (`plan.focus_done and plan.next_week`) | **DELETE — FLAG** the clearest "app tells you where you are" affordance |
| `views.py:1246-1256` `AthleteHomeView` + `?week=` | **KEEP** — `?week=` becomes the *only* week selector (was a display-only override); update docstring |
| `views.py:1240` comment "`is_current` only ever advances via the…" | **DELETE** the comment |

### 2.4 Auto-advance

| Site | Disposition |
|---|---|
| `views.py:1413-1414` `athlete_log_session` → `if advance_current_week(): _touch_plan(...)` | **DELETE** the whole `if` block (see §7 for the intentional `_touch_plan` loss) |
| `views.py:1552` `athlete_cell_write` → `advance_current_week()` | **DELETE** the one line; the **unconditional** `_touch_plan(plan)` on the next line stays |
| `models.py:1703-1789` method | **DELETE** (§2.1) |

### 2.5 Coach "Make current"

| Site | Disposition |
|---|---|
| `views.py:2888-2933` `week_set_current` view (incl. `record_plan_action("Made Week N current")` and the bulk-clear-others) | **DELETE** whole view — one fewer undoable action kind |
| `urls.py:216-220` `api_week_set_current` route | **DELETE** |

### 2.6 Deliver — `views.py:3610-3644, 3695, 3721`; presenter `628-675`

| Site | Disposition |
|---|---|
| `views.py:3631` bare-button `target_week = current_week(plan)` | **FALLBACK — FLAG (minor)** bare "Deliver" now targets the **first** block; designer normally posts an explicit `week_id` so this rarely fires |
| `views.py:3695` `if current_week(plan) is None` batch guard | **FALLBACK** still a valid "plan has no week" guard |
| `views.py:3721` `target_week = current_week(copy)` | **FALLBACK** delivers copy's first block |
| `presenters.py:628-631, 669` deliver `live`/`live_id` + per-week `"is_current": w.pk == live_id` | **DELETE** the current-star (see §4-adjacent open item below) |

Deliver-screen highlight is a smaller open question, not a blocker: drop the star
entirely, or repoint it at the *viewed/target* week. Recommend **drop** — the
deliver screen no longer asserts a "current" week. `test_deliver.py` /
`test_batch_deliver.py` assert on `live_chip["is_current"]` — expect churn.

### 2.7 Designer / grid defaults

| Site | Disposition |
|---|---|
| `views.py:239-260` `MesoDesignerView` → `_default_grid_mesocycle` | **FALLBACK** opens on earliest-live-week's block |
| `views.py:2804-2815` `_default_grid_mesocycle` | **REDESIGN (small)** — redefine without `current_week`; see §4b (it is shared with the agent fallback) |
| `views.py:2820-2841` `api_mesocycle_grid` default | **FALLBACK** `?mesocycle=` still explicit |
| `serializers.py:825, 916` `serialize_mesocycle_grid` `current_week_id` + `_pick_session_id` | **FALLBACK** set `current_week_id = weeks[0].pk if weeks else None`; `_pick_session_id` already falls through to earliest live week — rename its param `anchor_week_id`, update docstring (`781-790`) |
| `serializers.py:193` `serialize_week` `"current": week.is_current` | **DELETE** field → forces frontend change (§2.9) |
| `serializers.py:970` `serialize_mesocycle_grid` week dict `"current"` | **DELETE** field (same coupling) |
| `views.py:2618, 2864` `week_view` / `session_add` / `week_add` `current_week(plan)` fallback | **FALLBACK** both already prefer body `week_id`; default becomes earliest live week |

### 2.8 Delete-guard — `views.py:2938-2989`

| Site | Disposition |
|---|---|
| `views.py:2970-2977` "refuse to delete the current week" guard | **DELETE — FLAG** removes a user-facing 400 ("Make another week current before removing") |
| `views.py:2978-2985` `live_week_count <= 1` guard | **KEEP** the last-live-week guard stays |
| `views.py:2948-2957` docstring rule | **DELETE** the current-week clause |

Behavior change: any live week is now deletable, subject only to the last-week
rule.

### 2.9 Undo/redo — `history.py`

| Site | Disposition |
|---|---|
| `history.py:99` capture `"is_current": w.is_current` | **DELETE** key |
| `history.py:229` restore `week.is_current = row["is_current"]` | **DELETE** line (column is gone; no `.get()` guard needed) |
| `history.py:163` docstring parenthetical | **DELETE** the `is_current` mention |

### 2.10 Frontend designer island — `frontend/designer/src/`

| Site | Disposition |
|---|---|
| `lib/api.ts:68, 133` `current: boolean` on `Week` type(s); `_pick_session_id` doc `173-182` | **DELETE** field, scrub doc |
| `hooks/useGrid.ts:93-95, 343, 397-401, 552` `setCurrentWeek` POST + `currentWeekId()` = `find(w=>w.current) ?? weeks[0]` | **DELETE** the POST; repoint `currentWeekId` to a **viewed/selected-week** state or plain `weeks[0]` (it anchors reorder + cell-write payloads `343,510,526` so it must resolve to a real live week); scrub the verbs comment `line 19` |
| `hooks/useTableReorder.ts:19-35, 65` `grid.weeks.find(w=>w.current)` | **REDESIGN/FALLBACK** same viewed-week source as above |
| `lib/grid.ts:44-50, 67, 83, 123-127` `deriveSelectedWeek` / `cycleLabelFromGrid` / cell-styling fall back on `w.current` | **REDESIGN/FALLBACK** repoint to `weeks[0]`; remove the `current`-based border/bg branch (`49-50`) |
| `DesignerRoot.tsx:181-182, 192, 237` `gridCurrentWeekId = find(w=>w.current)?.id ?? weeks[0]?.id`; `onSetCurrentWeek` wiring | **FALLBACK/DELETE** collapse to `weeks[0]?.id`; delete the wiring |
| `components/MesoTable.tsx:104, 997, 1077, 1164, 1176` `onSetCurrentWeek` prop plumbing | **DELETE** |
| `components/MesoTable.tsx:1170-1237` `WeekManagerStrip` "Make current" pill + `make-current-${id}` testid + `current` badge/arm-confirm | **DELETE** the make-current control; **keep** add/remove-week + "+ Add week" |
| `components/MesoTable.tsx:1135-1144, 1184-1197` `week.current` highlight (`aria-current`, `--current` classes) | **DELETE** or **REDESIGN** — repurpose to highlight the *viewed* week if selected-week state is introduced; else remove |
| CSS `.meso-table-week-col--current`, `.meso-week-pill--current`, `.meso-week-pill-make-current`, `.meso-week-pill-badge` | dead — remove or leave (cosmetic; grep the stylesheet) |
| `frontend/meso_tour.test.js` / `static/js/meso_tour.js` `isCurrentPage` | **LEAVE** — unrelated page-URL matching, no `is_current` logic (confirmed) |

### 2.11 Admin / seed / import / factories

| Site | Disposition |
|---|---|
| `admin.py:215` `list_display` `"is_current"` | **DELETE** |
| `admin.py:218` `list_filter ("is_deload", "is_current")` | → `("is_deload",)` |
| `sheet_import.py:285-286` `"is_current": index == 1` + comment | **DELETE** key |
| `factories.py:106` `WeekFactory` `is_current = False` | **DELETE** attr (biggest test fan-out — §6) |
| `seed_meso_demo.py:312, 328, 718, 727, 764, 773, 1150, 1493, 1512` + docstrings `22, 706, 1089` | **DELETE** the `current_index`/`is_current` computations, fixture keys, and the `next((w … w.is_current))` delivery-stamp lookup — **small decision, see §4c-minor** |
| `migrations/0002_...:351` original `AddField` | **LEAVE** — historical migration, never edit past migrations |

---

## 3. The three "derive-a-scope" sites (not silent defaults)

These are called out separately because a naive earliest-live fallback is a real
regression at each. Two are the blocking product decisions in §4; the third
(agent apply) is mechanical once §4b's persisted-block change lands.

- **Agent grounding + validation** (`serialize_agent_block`, `clean_change`) — §4b.
- **Agent apply `_apply_deload`** (`apply.py:177`) — mechanical: replace the
  `current_week(change.batch.plan)` no-session fallback with the batch's persisted
  block's first live week (§4b), then drop the `current_week` import from
  `apply.py`.
- **Coach compliance / profile "training now"** (`adherence.py`,
  `profile_program`) — §4a.

---

## 4. The two redesigns (decided 2026-07-18)

> Both are now resolved (Lance, 2026-07-18). They change *what a coach sees and what
> the agent does* and could not be defaulted safely — everything else in this doc is
> mechanical. Decisions are recorded inline below.

### 4a — DECIDED: cadence-based compliance (recency + 14-day count)

**Decision (Lance, 2026-07-18):** the coach's roster signal is **cadence**, not
prescription-adherence — *"cadence is more important to know because it can alert
how much effort they're putting in."* Implement **Option A (recency) as the roster
pill + Option B (14-day session count) as a secondary profile chip** (details
below). Program-completion % (Option C) was considered and dropped — it measures
progress through a program, not effort/engagement. No per-week %, no `is_current`.

**The problem.** Three coach-facing surfaces derive a *measurement* from the
pointer, all rooted in `adherence.link_current_week` (`adherence.py:23`), which
orders live, non-archived weeks by `-plan.modified, -is_current, mesocycle.order,
index` and takes the first. Remove `is_current` and the meter's denominator
disappears:

1. **Roster compliance meter** (`views.py:375` → `roster.html:124-128`):
   `link_compliance` (`adherence.py:64`) = `round(done_sessions /
   total_sessions * 100)` **of the current week only** → a 0–100 `meso-meter`.
2. **Athlete-profile "Current block · Wk N"** (`presenters.profile_program`
   `presenters.py:212` → `athlete_profile.html:90-118`) — the same asserted-current
   label Lance wants gone, plus a second meter off the same source, plus the
   macrocycle rail highlight anchored on the current week's mesocycle.
3. **Roster activity feed** (`recent_logs` `adherence.py:90`) — **already fully
   date-less**, orders DONE logs by `created_at`, no `is_current`. Survives
   untouched. State this explicitly so it isn't swept into the rework.

A plain earliest-live fallback pins the meter to **Week 1 forever** — actively
misleading (a lapsed athlete keeps a high early-week %). The enabling fact:
`SessionLog.created_at` (server `auto_now_add`, always present, monotonic) is a
real clock even though the *program* is date-less. Recency never needed
`is_current` or program dates — it needs the log-write clock. (The codebase
already prefers `created_at` over the athlete-entered nullable `SessionLog.date`.)

**Options** (all inherit two caveats: `created_at` is log-*write* time so
re-saving an old workout reads as fresh; any recency signal penalizes athletes who
train but don't log — a coaching-culture tradeoff, not just code):

- **A — Recency: "last trained N days ago."** Newest DONE `SessionLog.created_at`
  per link, diffed against `now`. Roster: tone-coded pill in the meter's slot
  (≤3d green / 4–9d amber / ≥10d red); `None` → "No sessions yet" (mirrors today's
  hidden-meter). Profile: "Last trained …" where "Current block · Wk N" is today;
  macrocycle rail highlights the block of that most-recent log (factual history,
  not "you are here"). *Most direct read of "keeping up"; cheapest; degrades
  gracefully.* Loses volume sense; no rest-day concept.
- **B — Rolling volume: "X sessions in last 14d."** Count distinct DONE sessions
  where `created_at >= now - 14d`. **Can't be a 0–100 meter** — a date-less
  program exposes no prescribed weekly frequency to divide by (the very thing whose
  removal is the point). An absolute count, a visual departure from the bar.
- **C — Program-completion %.** Distinct DONE sessions ÷ all live sessions in the
  newest plan. **Preserves the `meso-meter` UI** as a real 0–100. But it's
  *monotonic* — answers "how far along," not "keeping up"; a lapsed athlete keeps a
  high %; the denominator churns as the coach adds/removes blocks.

**Implementation (per the decision):** **A (recency) as the roster signal, with B's
14-day count as a secondary chip on the profile.**
"Keeping up" is a cadence question and the log clock is the only honest date-less
cadence signal. C would mislead as the roster meter; B can't be a meter alone.
Concretely: roster shows a tone-coded "last trained" pill (A); profile replaces
"Current block · Wk N" + the meter with "Last trained {recency} · {N} sessions in
14d" (A+B); rail highlights the most-recent DONE log's block. Re-base the
`has_program` gate (`presenters.py:234`, today `link_current_week` **and**
`link_compliance` both non-None) onto **"plan has any live week"** — computable
without `is_current`. `roster_activity` needs no change.

**Still to settle at build (non-blocking):** the exact recency threshold bands
(≤3d / 4–9d / ≥10d) and whether penalizing athletes who train-but-don't-log is
acceptable (a coaching-culture tradeoff).

### 4b — DECIDED: persist the viewed block on the agent batch

**Decision (Lance, 2026-07-18):** adopt the persist-on-batch approach below — the UI
sends the open block's `mesocycle_id`, stored on `AgentProposalBatch` (`SET_NULL`),
consumed by grounding + validation + apply. It's the only option that survives the
background-job + later-apply time gap.

**The problem.** The agent learns "which block am I programming?" from
`current_week(plan).mesocycle`, re-derived **three times across time-separated
requests** — grounding and validation run in a **background job**, apply runs on a
**later coach request** (minutes later). Nothing records the block the coach had
open; the code trusts `is_current` still points at it. Remove the pointer and all
three fall back to earliest-live → **the agent silently programs block 1 even
while the coach works block 2.** A real regression, not a cosmetic one:

- `serialize_agent_block(plan)` (`serializers.py:983`) grounds Claude on
  `current_week(plan).mesocycle` and tags each snapshot `is_current`.
- `clean_change(raw, plan)` (`validation.py:221-247`) re-derives the same block and
  drops any returned target outside it as "out of contract" — valid edits to the
  viewed block would be silently dropped.
- `_apply_deload` (`apply.py:177`) re-derives independently as its no-session
  fallback.

**The fix (recommended — this is the intended design):** capture the viewed block
**at request time** and **persist it on the batch**, because grounding/validation
(background) and apply (later) can't re-read a live pointer.

- **UI** — the island already knows the open block (`gridData.mesocycle.id`,
  `DesignerRoot.tsx:122`; grid state `useGrid.ts:219`). Change
  `useAgentChat.ts:78` to send `{ instruction, mesocycle_id }`.
- **Model** — add `AgentProposalBatch.mesocycle` FK (`models.py:2089`),
  **`on_delete=SET_NULL, null=True, blank=True`** — the batch is also the
  usage/cost ledger, so a deleted block must not delete billing history; `null`
  also covers legacy rows and the API fallback. New migration (sequence after
  `0043`, or fold into the agent-scope commit — see §9).
- **View** — `agent_propose` (`views.py:3895`, after `_coach_plan_or_forbidden`
  which already ownership-scopes the plan): parse `mesocycle_id`, then
  `get_object_or_404(Mesocycle, pk=…, plan=plan)` — the `plan=plan` filter **is**
  the security check (a foreign block 404s); else `_default_grid_mesocycle(plan)`;
  if `None`, 400 "no block to program yet." Pass `mesocycle=` into
  `create_drafting_batch`. Mirror in `_reserve_plan_draft` (`views.py:701`, the
  Draft-with-AI path) with the scaffolded plan's single block.
- **Fallback helper** — redefine `_default_grid_mesocycle` (`views.py:2804`, shared
  with the grid default §2.7) as
  `plan.mesocycles.filter(deleted_at__isnull=True).order_by("order").first()` — the
  plan's first block by order, no `current_week`.
- **Grounding** — `run_proposal_job` reads `batch.mesocycle`
  (`select_related`); `build_context(plan, mesocycle)` passes it through;
  `serialize_agent_block(plan, mesocycle)` accepts it directly (drop the
  `current_week` derivation and the per-week `snap["week"]["is_current"]` line,
  `serializers.py:1001`; keep the `mesocycle is None` empty guard and the week
  `index`).
- **Validation** — `clean_change(raw, plan, *, mesocycle, forbidden=None)`; delete
  the re-derivation (`validation.py:228-229`); use the passed `mesocycle` in both
  `_resolve` scope filters. `_persist_result` (`service.py:167`) threads
  `batch.mesocycle`. Update the `evals.check_result` / eval-harness caller
  (`agent/evals.py`) to pass the case's block.
- **Apply** — `_apply_deload`: `week = change.session.week if change.session_id
  else (batch.mesocycle's first live week or None)`; drop the `current_week`
  import.

**Contract check (verify in code first):** `test_agent_grounding.py:41` asserts
`"is_current" in w["week"]`, and the agent **system prompt** may describe the
field — grep `agent/` prompt strings for `is_current`/"current week" and scrub, or
the model gets a dangling contract.

Edge cases: no blocks → `_default_grid_mesocycle` returns `None` → `agent_propose`
400s (empty-guard stays as defense-in-depth); block with no materialized weeks →
`weeks: []`, validation matches nothing, add/deload no-op; mesocycle soft-deleted
mid-run → FK intact, `deleted_at` scope filters drop stale targets; hard-deleted →
`SET_NULL` → `_apply_deload` degrades to no-op; coach switches blocks mid-draft →
irrelevant, the block is snapshotted at request time.

**This change must land before or with the global removal** — otherwise the three
agent sites silently fall back to earliest-live.

**Settled 2026-07-18:** persist-on-batch (vs re-passing at apply, which can't work
across the time gap); `SET_NULL` for ledger safety; 400-on-no-block.

---

## 5. Athlete home + defaults after removal

**Default program (the "last-opened" view Lance wants).** `Plan.modified` is the
wrong signal — it's bumped by the *coach's* edits/deliver (`_touch_plan`,
`views.py:2355`), not athlete activity, so a coach touching plan B would yank the
athlete's default to B. True "last-opened" would need a per-user-per-plan
write-on-read timestamp (new field + a write on every home GET). **Skip that.**
Derive the default as **the plan holding the athlete's most-recent
`SessionLog`/`LoggedSet`** (`SessionLog.objects.filter(athlete=user)
.order_by("-created_at")`, walk to plan). Under parse-at-commit every performed
entry writes a `SessionLog`+`LoggedSet`, so "most recently logged" is a strong,
athlete-scoped, **zero-storage** recency signal — and arguably truer than "opened"
(engagement, not a page view, should resurface a program). Fallbacks for an
athlete who has logged nothing: most-recently-delivered plan (`max
Week.delivered_at`) → then `-modified` (today's order). Keep the card list; make
the derived plan the first/expanded card.

**Position inside a program.** Replace the `is_current` anchor (`presenters.py:1145`)
with a **re-derived-on-read scroll hint**: the last live week containing any of
*this athlete's* logged sets, else the earliest live week (same SessionLog query,
no stored pointer). **Render it with no "current"/"you are here" styling or
label** — this honors the product rule that the app must not tell the athlete
which week they're on. It reads as neutral scroll-restore ("back to where I last
typed"), not a prescription. All cells editable (parse-at-commit); the athlete
scrolls freely across every block/week.

**What dissolves.** With the whole grid editable and free navigation, the per-card
`focus`/`focus_index`/`focus_done`, the `next_week` nudge, and "the focus week's
sessions are the only tappable rows" all go away — the card is just
title/coach/goal + the editable grid scrolled to the derived hint. The chip strip
stays as pure `?week=` navigation (minus its `current` flag).

**New storage: none.** Only add a literal `last_viewed_plan` timestamp if product
later insists on tracking an athlete who *browses but never logs* — the
most-recently-logged proxy covers the described behavior, so don't add it now.

**Designer / deliver defaults:** both already fall through to earliest-live once
`current_week()` loses its `is_current` step — designer opens to the plan's first
block, bare-deliver targets the first block. Optional one-line product toggle
(zero new storage, uses `mesocycle.order`): default to the plan's **last/newest**
block instead, since coaches build and deliver forward. Ship first-block as the
minimal default; treat last-block as a follow-up if the first-block default feels
stale.

---

## 6. Migration + cleanup checklist

### Migration `0043`

`app/store_project/meso/migrations/0043_remove_week_is_current.py`:

- Single `migrations.RemoveField(model_name="week", name="is_current")`.
- `dependencies = [("meso", "0042_prescription_athlete_authored")]`.
- No data migration, no backfill; the field is in no DB constraint.
- **Effectively irreversible:** the reverse re-adds the column with `default=False`
  but not which week each plan pointed at — acceptable per the decision (a boolean
  pointer being deliberately dropped).

The §4b agent-scope change adds a **second** migration (`AddField
AgentProposalBatch.mesocycle`). Order it after `0043`, or land the whole
agent-scope work as its own commit with its own migration (§9).

### Cleanup groups

- **Pure deletions:** all rows in §2.1, §2.9, §2.11, plus `admin.py`, `factories.py`,
  `sheet_import.py`. Mechanical.
- **Consumer rework:** `current_week()` degrade (§2.2); the direct `w.is_current`
  readers in `serializers.py` (`193, 825, 916, 970`) and `presenters.py`
  (`_week_chip_groups`, deliver); `adherence.py` (§4a); templates
  (`athlete_home.html`, `athlete_profile.html`).
- **Endpoint + call-site removal:** `week_set_current` view + URL (§2.5); both
  auto-advance blocks (§2.4); the delete-guard clause (§2.8). Audit the "lock the
  plan then touch weeks" comments that name `week_set_current` as the
  lock-ordering exemplar (e.g. `views.py:2630`; a `history.py` "mirrors
  `week_set_current`" mention) and **repoint them to a surviving endpoint**
  (`week_delete`) so the lock-ordering docs stay coherent.
- **Frontend island + vitest:** §2.10.
- **Seed decision (minor, §4c-minor):** `seed_meso_demo.py:1493-1512` derives
  "the current week" to stamp delivery. With no pointer, **simplest is to deliver
  every live week** (or the last) — pick one and update the fixtures/docstrings.
  Verify in code first that no seed consumer requires the `is_current` key.

---

## 7. Tests

**`athlete_log_session` / `athlete_cell_write` are safe to strip** (verified in
code):

- `athlete_cell_write` (`views.py:1552`): the `_touch_plan(plan)` on the next line
  is **unconditional** and independent of the advance — delete only line 1552, no
  `Plan.modified` bump lost.
- `athlete_log_session` (`views.py:1413-1414`): the `_touch_plan` here is
  **conditional on the advance** (`if advance_current_week(): _touch_plan(...)`).
  Deleting the whole `if` block also drops that bump — **intended**. `athlete_home`
  orders cards by `-modified`, so dropping it means logging no longer floats a plan
  to the top — which is *consistent* with the new "default = last-engaged program"
  model (§5 resurfaces by most-recent log, not by `modified`). Delete the block
  with **no** replacement `_touch_plan`; note the intentional behavior change.
  Neither view touches any other field/method of the removed pointer; the athlete
  response re-serializes from the live grid and `?week=` is display-only.

**Delete outright — tests of removed behavior** (verify line ranges in code first;
they drift):

- `test_athlete_logging.py` — the `#456` auto-advance suite (`advance_current_week`,
  self-heal, stale-instance race).
- `test_athlete_tracking.py` — `test_athlete_sub_line_advances_current_week`.
- `test_week_management.py` — `week_set_current` / "view does not change the current
  week" cases.
- `test_designer_undo.py` — the `week_set_current` undo cases.
- `test_designer_delete.py` — `test_current_week_cannot_be_deleted`,
  `test_week_set_current_404s_for_a_deleted_week`; adjust the
  `test_soft_deletes_non_current_week…` case.
- `test_seed_demo.py` — `test_plan_has_exactly_one_current_week` etc.
- `test_agent_grounding.py` — `assert "is_current" in w["week"]`.

**Re-baseline (assertions read a `current`/`is_current` flag off a payload):**
`test_serializers.py`, `test_plan_create.py`, `test_plan_draft.py`,
`test_deliver.py`, `test_batch_deliver.py`, `test_delivery_diff.py`,
`test_adherence.py`, `test_profile_program.py`.

**Re-author to the new model (not just kwarg-stripping):** `test_athlete_surface.py`
— the many `is_current`-anchor and "advances is_current" cases → the "opens to
earliest week / `?week=` nav / no position label" model.

**`WeekFactory` kwarg fan-out:** `factories.py:106` drops `is_current`, so every
`WeekFactory(..., is_current=…)` call breaks (~30+ files per grep:
`test_designer_save.py`, `test_mesocycle_grid_endpoint.py`, `test_athlete_tracking.py`,
`test_serializers.py`, `test_adherence.py`, `test_deliver.py`, `test_agent_*`, …).
Strip the kwarg (default was `False`, so most are harmless); calls passing `True`
must adjust the expected anchor to "earliest live week."

**New / rewritten tests to add:**

- **§4a compliance:** the chosen date-less signal (recency pill tone bands / the
  `has_program` re-base onto "plan has a live week" / `None` → "No sessions yet");
  assert `roster_activity` still works untouched.
- **§4b agent scope:** `agent_propose` persists the posted `mesocycle_id` on the
  batch; a foreign `mesocycle_id` 404s; grounding + validation scope to the
  *persisted* block (not earliest-live) — the regression guard; `_apply_deload`
  no-session fallback uses the batch block; no-block → 400.
- **Athlete home:** default program = most-recently-logged; scroll hint = last
  logged week; **no "current"/"Week N" label rendered**; `?week=` navigates freely.
- **Delete-guard:** a non-last live week is now deletable; the last live week still
  guarded.

**Run the whole Django suite, not just vitest** (MEMORY: the designer island has
**Python source-scraping tests** that read `.tsx` and assert strings, in CI's
`build` job): `uv run pytest app/store_project/meso/` (or `just test`) **and**
`npm test` in `frontend/designer/`. The 2 `admin_honeypot` failures are
pre-existing on main.

---

## 8. Risks & gotchas

1. **Largest user-visible change: the athlete home stops telling the athlete their
   position** — "This week" heading, "Week N", current-chip fill, current-column
   highlight, "Start next week" nudge all go (§2.3). Intended by the decision, but
   it's the change a returning user will notice first.
2. **The compliance meter changes meaning (§4a, decided)** — "% of current week
   done" has no denominator in a date-less world, so it's replaced by a **cadence**
   signal (recency + 14-day count). Ship the cadence replacement **in the same PR**
   as the removal; a silent earliest-live fallback would pin the old meter to Week 1
   and actively mislead.
3. **The agent scopes to the wrong block if §4b is defaulted** — earliest-live
   means the agent always edits block 1 while the coach works block 2. §4b must
   land **before or with** the removal.
4. **Prod migration numbering:** append `0043` above `0042` — a pure append, **do
   not renumber** anything (MEMORY: check prod `showmigrations` before ever
   renumbering; here we don't). The §4b migration sequences after it.
5. **`SET_NULL` on the agent-batch FK is load-bearing** — the batch is the
   usage/cost ledger; CASCADE would delete billing history when a block is deleted.
6. **Stale MEMORY guardrail** — the "never delete `is_current`" cleanup guardrail
   is superseded; say so in the PR body.
7. **Don't run a JS formatter on the island** (MEMORY: no enforced JS formatter —
   match hand style in `meso.js`/`*.test.js`/the `.tsx` files).
8. **`SQLite-vs-Postgres` / `select_for_update`** does **not** apply here — this
   removal *deletes* the `advance_current_week` locking dance rather than adding
   one; no new locking semantics.
9. **Minor:** bare "Deliver" targets the first block (§2.6); the delete-guard 400
   disappears (§2.8) — both intended, both flagged.

---

## 9. Sequencing vs the parse-at-commit slice (5a)

The companion decision from the same thread — **parse-at-commit** (performed data
typed into grid cells, parsed on blur into a `LoggedSet`; records derive-on-read;
optimistic-then-confirmed PR celebration; 24h quiet-period settle) — **overlaps
this removal at `athlete_cell_write`** (`views.py:1552`), where auto-advance
currently fires. Both slices edit that view.

**Recommended order: land this removal first**, or at least land the
`athlete_cell_write` auto-advance deletion first. Reasons: (a) parse-at-commit
rewrites the cell-write path, and it's cleaner to build it on a view that no longer
carries the pointer-advance; (b) §5's "default program = most-recently-logged"
depends on parse-at-commit writing a `SessionLog`/`LoggedSet` on every entry — so
the athlete-home derivation should be *specced here but verified against the
parse-at-commit write path* once it exists (verify in code first that the derived
scroll-hint query sees the sets parse-at-commit writes). If parse-at-commit lands
first instead, this removal simply deletes the advance line from whatever
`athlete_cell_write` looks like then — a smaller edit but a merge point to watch.

Either way the two slices are **independent in their cores** (this one removes a
pointer; 5a adds a parse pipeline) and only touch at that one view — a clean
rebase, not a redesign, whichever ships first.

---

## 10. Branch / commit shape & Definition of Done

**Branch:** `meso/remove-current-week` (repo convention `meso/<slice>-<name>`).

**Commits** (each keeps the suite green on its own):

1. **Backend + migration** — field drop, `current_week()` degrade, all Python
   consumers (models, serializers, presenters, views, urls, adherence, history,
   admin, sheet_import, factories, seed), migration `0043`. Includes §4a's chosen
   compliance replacement.
2. **Agent scope (§4b)** — UI `mesocycle_id`, `AgentProposalBatch.mesocycle` FK +
   its migration, service/validation/apply threading. Can precede commit 1 but must
   not lag the removal.
3. **Frontend island** — remove `current` from types/payloads, delete
   `setCurrentWeek` + WeekManagerStrip make-current, repoint to viewed/`weeks[0]`;
   its vitest.
4. **Test sweep** — strip `WeekFactory` kwargs, delete removed-behavior suites,
   re-baseline payload assertions, add the new §4a/§4b/athlete-home/delete-guard
   tests.

**Process** (established): red/green, Opus agents, **run the Codex review loop
before PR**. Run `uv run pytest app/store_project/meso/` **and** `frontend/designer`
vitest before pushing.

**Definition of Done:**

- [x] §4a and §4b decided (2026-07-18): §4a cadence compliance (recency + 14-day
      count); §4b persist the viewed block on the agent batch.
- [x] `grep -rn is_current app/store_project/meso frontend/designer` returns only
      the historical `migrations/0002_*` line — **plus `0043`'s own `RemoveField`
      and ~8 prose mentions** in docstrings/test-module headers that explain the
      *removed* concept to future readers (e.g. "'Has a program' no longer means
      'has an `is_current` week'"). Deliberate: they exist to stop someone
      re-adding the pointer. No live code reads the field.
- [x] Migration `0043` applies clean; `showmigrations` shows a pure append after
      `0042`, and `0044` (§4b's FK) appends after that. `makemigrations --check`
      reports no drift.
- [x] Athlete home renders **no** "current"/"Week N"/"This week" label; opens to
      the derived last-engaged program + scroll hint; `?week=` navigates freely.
- [x] Coach roster shows the §4a-chosen signal; `roster_activity` unchanged;
      `has_program` re-based onto "plan has a live week."
- [x] Agent grounds/validates/applies against the **posted** block, verified by a
      "coach viewing block 2" regression test.
- [x] Non-last live weeks deletable; last-live-week guard intact.
- [x] Full Django suite green **and** `frontend/designer` vitest green
      (2261 Django / 517 vitest; the 2 `admin_honeypot` failures did not
      reproduce locally).
- [x] PR body notes the superseded MEMORY guardrail and the intentional athlete
      behavior changes (§8.1, §7 `_touch_plan` loss).

### Audit gaps found during implementation

This doc's surface audit was accurate on the whole, but was wrong or silent in
four places. Recorded so a future reader doesn't treat it as verified ground
truth:

1. **`agent/client.py`'s `SYSTEM_PROMPT`** told the model the plan context
   includes "which week is current." §4b predicted a dangling contract might
   exist but pointed at `agent/` generally; the string lives in a file §2 never
   enumerates. Scrubbed.
2. **`Mesocycle` has no `deleted_at`.** §4b specs `_default_grid_mesocycle` as
   `plan.mesocycles.filter(deleted_at__isnull=True)…`, which does not compile —
   only `Week`/`SessionSlot`/`ExerciseSlot` are soft-deletable. The filter was
   dropped; the "block soft-deleted mid-run" edge case is really about the
   block's *weeks*, which existing week-level filters already handle.
3. **`components/BlockView.tsx`** is missing from §2.10 entirely, but reads
   `w.current` in four places for timeline/calendar highlighting.
4. **§4a's `has_program` re-base** onto "plan has a live week" cannot be a plain
   `filter(mesocycles__weeks__deleted_at__isnull=True)` — that LEFT-JOINs and
   silently matches plans with *zero* weeks (the joined row is NULL, and NULL
   "is null"). It needs an `Exists` subquery.

Also: §2.10 says to update a committed `static/js/dist` bundle, but that path is
`.gitignore`d and CI builds it fresh — there is no artifact to commit.

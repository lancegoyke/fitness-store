# Meso — decisions to make before building real

**Status:** living document · started 2026-06-26
**Context:** The Meso program designer + coaching-loop screens exist today as a
**fully client-side mock** (PRs #267, #268). There is no database, no real agent,
no athlete app — every screen reads canned fixtures from `app/store_project/meso/mockdata.py`.
This doc tracks the decisions that gate turning those mocks into real code, so we
don't write schema or architecture on top of unsettled product questions.

How to read the status field:
- **⏳ Awaiting decision** — genuinely your call; blocks real work until answered.
- **🟡 Proposed** — architect's recommendation; will proceed on the rec unless overridden.
- **✅ Decided** — locked, with the choice recorded.

---

## Blockers (foundation — schema & architecture depend on these)

### B1 · Product shape & tenancy — single-coach or multi-coach?
Is Meso *you* programming for your own clients (single-tenant), or a tool other
coaches log into (multi-tenant SaaS)? Decides whether "coach" is a model, whether
every query is tenant-scoped, and the entire permissions layer. The store today is
B2C (sells programs to individuals); Meso is a different B2B-ish shape.

- Options: (a) single-coach, you only; (b) multi-coach SaaS now; (c) single-coach now but keep a `coach` FK so multi-coach is a clean retrofit.
- **Rec:** (c).
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **(b) Multi-coach SaaS from day one.** Tenant-scope everything: coaches are
  accounts, data is isolated per coach, athletes belong to a coach via a relationship. This
  promotes a tenancy + roles + permissions spine into even the first (persistence) slice —
  see N1–N4 below.

### B2 · What is an "athlete," and do they log in?
(a) Reuse the existing `User` model (UUID PKs, allauth) for athletes, or a lightweight
`Client` record with no login? (b) Do athletes get an **app** at all, or does the coach
log everything? The phone view, "Deliver to her app," and push notifications all imply an
authenticated athlete surface. (c) If yes — native, PWA, or responsive web?

- **Rec:** athletes = `User`s; athlete surface = responsive web/PWA first (no native).
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **Athletes are `User`s who log in** (responsive web / installable PWA; native
  deferred) **and** their coach can edit their program. So we need a coach↔athlete relationship
  that grants the coach edit rights on the athlete's plan (see N1). A single `User` may act as
  both coach and athlete.

### B3 · Domain schema & relationship to the existing `Program` product.
The store already has a sellable `Program` product; a Meso plan is a different thing
(personalized, periodized, mutable). Decide: distinct entities, and the granularity —
`Plan → Mesocycle → Week → Session/Day → ExercisePrescription → SetPrescription`, plus
`SessionLog → LoggedSet`. And: do we need **draft vs delivered versions** + a
`ProposedChange` diff record? (The review screen and "changes since last delivery" only
exist if programs are versioned.)

- **Rec:** distinct from store `Program`; full hierarchy; yes to draft/published versioning + a `ProposedChange` entity (load-bearing for the agent + review gate).
- **Status:** 🟡 Proposed (architect's call once B1/B2 land).
- **Decision:** _tbd_

### B4 · Exercise source — catalog FK, free text, or hybrid?
There's an `exercises` app with videos + alternatives. The prototype lets a coach type any
exercise name AND shows "knee-safe" tags + per-athlete swaps. Decide whether a prescription
FKs to a catalog `Exercise` (enables the picker, alternatives, contraindication matching,
agent grounding) or stays free text.

- Options: (a) hybrid — catalog when matched, free-text fallback; (b) catalog only; (c) free text only.
- **Rec:** (a) hybrid — the agent and auto-adjust need the catalog to be useful.
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **(a) Hybrid.** A prescription FKs to a catalog `Exercise` when one matches
  (nullable), with a free-text name as fallback. Picker/alternatives/contraindication-matching
  light up for catalog-linked exercises; coaches can still type a one-off.

### B5 · Front-end data flow — htmx server-rendered, or JSON API + client state?
Every interactive screen is ephemeral client state today. To persist, pick a fork: lean on
**htmx** (server-rendered partials, Django owns state — matches the rest of the app) vs. a
**JSON API (DRF) + Alpine/JS** owning richer client state (better for the spreadsheet-like
designer grid). Shapes how all five screens get wired; hard to reverse.

- **Rec:** htmx for roster/profile/review/deliver/results; a small JSON-autosave endpoint for the designer grid specifically. Don't adopt DRF wholesale unless the athlete app forces it.
- **Status:** 🟡 Proposed (architect's call; depends on B2).
- **Decision:** _tbd_

### B6 · The agent — architecture, grounding, guardrails, execution.
Provider is **Claude** (project standing guidance). Real decisions:
- **Shape:** structured tool-calling — the model emits a validated batch of program edits
  (swap / load / volume / deload) applied server-side — vs. free text. **Rec: tool-calling.**
- **Grounding:** profile, contraindications, coaching rules, recent logged sessions, exercise
  catalog; how logs get summarized into context.
- **Guardrails:** contraindications enforced in a **validation layer**, not just the prompt;
  keep **human-in-the-loop approval** (the review screen is that gate). **Rec: both.**
- **Execution:** sync / streamed ("drafting…") / background job (Redis is already in the stack).
  **Rec: background job + streamed status.**
- **Eval:** golden cases so quality doesn't silently regress.
- **Model tier + prompt caching:** pin against the `claude-api` reference at build time — not
  guessed here.
- **Status:** ✅ Decided & building (Phase 1 merged 2026-06-27, PR #280).
- **Decision:** **Tool-calling + a server-side validation layer + the human review gate.** Provider =
  Claude (`claude-opus-4-8`), forced `propose_program_changes` tool, prompt caching; adaptive thinking
  omitted (incompatible with a forced `tool_choice`). Contraindications enforced deterministically in
  `meso/agent/validation.py` (not just the prompt); the coach still approves. Execution is sync for
  now (background job + streamed status deferred); eval golden cases deferred. Full phasing in
  [`agent-plan.md`](./agent-plan.md).

---

## Promoted decisions (now in scope because of B1 multi-coach + B2 athlete login)

Choosing multi-coach SaaS pulls these out of "later" and into the **persistence slice** —
you can't write ownership/scoping-correct models without settling them.

### N1 · Coach↔athlete relationship & cardinality ✅
The load-bearing new model.
- **Status:** ✅ Decided (2026-06-26).
- **Decision:** **Many-to-many, athlete-consented.** An athlete may work with multiple coaches
  concurrently (and a coach has many athletes). The link is
  `CoachAthlete(coach, athlete, status, invited_by)`; relationships require the other party's
  acceptance and the **athlete can decline or end** any coach link ("if they so choose"). A
  `User` may be both coach and athlete.
- **Consequences (now load-bearing for the schema):**
  - **D-a · Plans owned per relationship** — each coach programs independently for the athlete; a
    `Plan` FKs its `CoachAthlete`. _(rec)_
  - **D-b · Athlete profile vs plan attributes** — contraindications/injuries + training history
    are **global** to the athlete (every coach sees them); goals/focus live **per plan**. _(rec)_
  - **D-c · Bidirectional invites** — coach invites athlete, or athlete requests coach; both need
    acceptance; either side can end it (archives that coach's plans, never deletes). _(rec)_
  - **Scoping** — a coach sees only athletes they have an active link to and edits only their own
    plans; an athlete sees plans from all their coaches. Cross-coach scheduling collisions in the
    athlete app are a later UX concern.

### N2 · Tenancy scoping enforcement 🟡
How isolation is guaranteed so coach A never sees coach B's athletes/plans. Manager-level
scoping by `coach`, object-level permissions, or a tenancy middleware. **Rec:** explicit
`coach` FK on tenant-owned models + a scoped base manager/queryset mixin + view-level checks.

### N3 · User roles (coach vs athlete) 🟡
The store `User` is currently a customer. Now a `User` may be coach, athlete, both, or neither.
Mark via a `CoachProfile`/`AthleteProfile`, Django groups, or boolean flags. **Rec:** a thin
`CoachProfile` (presence = is-a-coach) + the `CoachAthlete` link (presence = is-an-athlete);
avoids overloading the User model.

### N4 · Athlete onboarding / invites 🟢 (Phases 1–3 built)
How an athlete joins a coach: coach invites by email → athlete signs up (allauth) → link
created; or coach creates a stub athlete and sends a claim link. **Decision:** email invite +
claim, reusing allauth — **Phase 1 built** (the `CoachInvite` email artifact → bearer-token
claim → materialized active `CoachAthlete`; rides allauth's `?next=` with no custom adapter).
**Phase 2 built** — the reverse direction: an athlete *requests* a coach by email
(`CoachAthlete.request`), the coach accepts/declines on the roster, both sides see the pending
state on their own surface, and any non-coach now lands on their training home (where the
request form lives). **Phase 3 built** — invite *lifecycle*: a 14-day TTL (`expires_at`) + a
new `EXPIRED` status, lazy + swept expiry (the claim path refuses a stale token;
`meso_expire_invites` bulk-sweeps overdue invites), and an explicit **resend** (rotates the
token + resets the clock, re-arms an expired invite) surfaced on the roster. Plan + deferred
items in [`invites-plan.md`](./invites-plan.md).

---

## Secondary decisions (resolve per-slice; not blocking the foundation)

| # | Decision | Note |
|---|----------|------|
| S1 | **Groups** — "shared program + per-athlete auto-adjust" modeling (template + override diffs) | 🟡 In progress — Phase 1 (group + membership spine + read surface) + Phase 2a (shared group program + Group-mode designer) + Phase 3 (per-athlete overrides — the `adj` overlay) built; plan in [`groups-plan.md`](./groups-plan.md) |
| S2 | **Units & RPE vs %1RM** | ✅ Complete — units (kg/lb) shipped with earlier slices; Phase 1 (first-class `load_type` `abs`/`pct`) + Phase 2a (agent %1RM-awareness — prompt + a deterministic %1RM progression bound) + Phase 2b (athlete %1RM logging ergonomics — the estimated-1RM helper) all built & deployed. **Follow-up: persisted, coach-visible 1RM** (Phase 1 — `AthleteOneRm`, auto-derived from logged history) built. Plans in [`units-rpe-plan.md`](./units-rpe-plan.md) / [`one-rm-plan.md`](./one-rm-plan.md) |
| S3 | **Delivery & notifications** | ✅ Built — push (PWA, Phase 4b) + email (`django-ses` + `notifications`). **Email opt-out shipped** (2026-06-29): the delivery email now carries a working one-click `List-Unsubscribe` (RFC 8058) honored by a per-athlete flag — see decision log. |
| S4 | **Results ↔ `challenges`/records** | ✅ Resolved (2026-06-29, YAGNI review): **keep separate, build nothing now.** The "results screen shows a PR" premise was mock-only (the real results screen never showed a PR), and `challenges.Record` is time-based (wrong domain for a strength PR). Meso already owns best-lift data via `AthleteOneRm`. A motivational PR badge stays deferred until there's a concrete need. |
| S5 | **Real-time transport** | ❌ Deferred (YAGNI, 2026-06-29): the agent result is atomic behind a human review gate and the stack is WSGI — SSE/websockets buy ~1.5s over a cheap bounded poll for disproportionate ASGI/channels/Redis-channel-layer infra. Revisit only if the agent itself becomes genuinely streaming. |
| S6 | **Billing** | 🟢 **Planned, decisions locked** (2026-06-29): **per-active-athlete (seat) pricing** + a **free tier + 14-day no-card trial**; the paywall gates **athlete count + the AI agent** (free tier = capped seats, no agent; groups/notifications free at every tier); first slice = the **subscription spine** for existing coaches (self-serve signup later). Full plan + phasing in [`billing-plan.md`](./billing-plan.md). |
| S7 | **Offline logging** | ✅ Built — athlete PWA offline log queue (Phase 4b). |

---

## Suggested sequence (decisions unblock work, not the reverse)

Chosen first slice: **persistence first.** Note multi-coach (B1) makes this slice bigger than
plain CRUD — it carries the tenancy/roles/relationship spine (N1–N3).

0. ~~Confirm N1 cardinality~~ — **done: many-to-many, athlete-consented.**
1. **Tenancy + persistence slice** — `CoachProfile` / `CoachAthlete`, the program schema (B3),
   hybrid exercises (B4), scoped managers (N2), and designer/roster/profile reading & writing
   real data over htmx (B5). *No agent.* Replaces the most mock-y part; unblocks everything else.
2. **Agent as proposal engine** behind the existing review gate (B6). Writes `ProposedChange`s;
   coach still approves. Safe — the human gate already exists.
3. **Athlete delivery + logging** — the athlete PWA surface, notifications, then results feeding
   back to the agent. *(Built.)*
4. **Billing (S6) — the next major slice (decisions locked, plan written).** Meso is a multi-coach
   SaaS, not a single-operator tool, so coaches pay. Decided 2026-06-29: **per-active-athlete (seat)
   pricing**, a **free tier + 14-day no-card trial**, **athlete-count + the AI agent gated**, monthly/USD,
   and a **spine-first** build (existing coaches; self-serve signup later). The tenancy spine (B1) is
   already in place; what's missing is the subscription infrastructure (today's Stripe is one-time
   only). Full plan + 5-phase build in [`billing-plan.md`](./billing-plan.md). Phase 1 = the
   `CoachSubscription` model + gating accessor + local trial + comped flag (no Stripe yet). Open
   *values* (not architecture): free seat count (rec 1), per-seat price (TBD), trial 14d.

---

## Decision log

_(Append dated entries here as decisions land.)_

- 2026-06-26 — Doc created; B1, B2, B4 and the build-order question raised for decision.
- 2026-06-26 — **Decided:** B1 = multi-coach SaaS from day one · B2 = athletes are Users who
  log in (web/PWA), coach can edit their plan · B4 = hybrid exercise source · first slice =
  persistence. Multi-coach promoted N1–N4 into scope. **Open before schema:** N1 cardinality.
- 2026-06-26 — **Decided:** N1 = many-to-many, athlete-consented (an athlete may work with
  multiple coaches; either party can end it). Plans owned per coach↔athlete relationship (D-a);
  contraindications global, goals per-plan (D-b); bidirectional invites (D-c). Schema is now
  unblocked.
- 2026-06-26 — **Confirmed (build):** role/profile models live in the **`meso`** app (not
  `users`); PR 1 ships the invite **state machine + tokened URLs**, with real email send as a
  follow-up. **Phase 1 built** on branch `meso-persistence-phase1`: `CoachProfile`,
  `AthleteProfile`, `Contraindication`, `CoachAthlete` (M2M-through + state machine + scoped
  manager); migration `meso.0001_initial`; admin; factories; 28 tests; roster + athlete-profile
  read real scoped data. Athlete-profile URL is now `<uuid:pk>` (was a mock slug).
- 2026-06-27 — **Phase 1 merged & deployed:** PR #270 squash-merged to `main` (`ec06974`),
  Django CI green, deployed to Hetzner (migration applied in prod). Resume point → Phase 2
  (program schema).
- 2026-06-27 — **Phase 2 built & merged** (PR #271, squash `079b891`; Django CI green): the program
  schema `Plan → Mesocycle → Week → Session → ExercisePrescription` (hybrid catalog `Exercise` FK,
  nullable = B4) + `PlanQuerySet` scoping (D-a) + `SessionLog`/`LoggedSet` (models now, UI later)
  in `meso/models.py`; `CoachAthlete.end()` now archives the relationship's plans (D-c); a
  `serialize_plan` (`meso/serializers.py`) round-trips a seeded plan to the designer's
  `program`/`weeks`/`phases` shape (macrocycle phase state derived by sequence position, not
  `order` arithmetic — robust to non-contiguous order). Migration `meso.0002`; admin + factories;
  built red→green then a local Codex review pass (19 new tests, 47 meso / 187 project-wide).
  Resume point → Phase 3 (designer save/load). Settles B3 in build form: distinct entities, full
  hierarchy; draft/active/archived status on `Plan` (no separate `ProposedChange` yet — that lands
  with the agent slice).
- 2026-06-27 — **Phase 5 built & merged** (PR #278, squash `5babceb`; Django CI green, deployed to
  Hetzner): `seed_meso_demo` management command (idempotent; `--delete` / `--coach-email`; created
  coach gets a runtime-random password, no literal in source) stands up the coach + five athletes +
  active links + Maya's sample plan as real rows, reproducing the prototype roster/designer. The
  coach-side **mock is retired**: bare `/meso/designer/` + `/meso/deliver/` redirect to the coach's
  working plan (`_coach_working_plan`) or the roster, `DeliverView`'s `mockdata.DELIVER` fallback is
  gone, and `meso.js`'s `program`/`weeks`/`phases` fixtures are emptied (the grid always hydrates
  from an injected plan). `mockdata.py` now serves **only** the review + results screens (their own
  slices). Built red→green then a 3-round local Codex review (reseed-reconcile + redirect ordering):
  20 new tests, 99 meso / 239 project-wide green. No migration (no model changes). The designer's
  left-rail/agent/phone chrome stays static prototype HTML by design — it rebuilds with the agent +
  athlete slices. **Persistence slice complete.** Resume point → the **agent** slice (B6: proposal
  engine behind the review gate).
- 2026-06-27 — **Agent Phase 1 built & merged** (PR #280, squash `953d9d4`; Django CI green, deployed
  to Hetzner — migration `meso.0004` applied): the **B6 proposal engine behind the review gate** is
  live. `AgentProposalBatch` + `ProposedChange` + the `meso/agent/` package (`client`/`validation`/
  `service`); `POST api/plan/<id>/agent/` runs Claude (`claude-opus-4-8`, forced `propose_program_changes`
  tool + prompt caching; **adaptive thinking omitted** — incompatible with a forced `tool_choice`),
  validates server-side, and persists a reviewable batch; read-only `GET review/<batch_id>/` renders
  it. **Contraindications enforced in a deterministic validation layer** (current-week scoping,
  target consistency, swap-only contraindication backstop, plural-folded), not just the prompt;
  human approval gate unchanged. 47 new tests (146 meso / 286 project-wide); local Codex review clean
  (8 rounds). Build plan + phasing in [`agent-plan.md`](./agent-plan.md). Resume point → agent Phase 2
  (per-change approve/reject + **apply** back into the program).
- 2026-06-27 — **Agent Phase 2 built & merged** (PR #282, squash `ee7d456`; Django CI green, deployed
  to Hetzner — **no migration**, `status`/`payload` already existed): the review gate now **writes
  back**. `meso/agent/apply.py` applies each approved change's structured `payload` (swap → prescription
  name; progress → load; volume → set count; deload → flags the week), built deterministically by
  `agent.validation` from the tool's new `new_name`/`new_load`/`new_sets` fields. Endpoints (scoped to a
  coach-owned batch): `POST api/change/<pk>/status/` persists per-change approve/reject;
  `POST api/batch/<id>/apply/` applies every non-rejected change in one transaction → batch `applied`,
  bumps `Plan.modified`; `POST api/batch/<id>/dismiss/` → `dismissed`. `review.html` persists toggles
  and wires Apply/Discard; bare `review/` redirects to the latest pending batch and `mockdata.PROPOSED_CHANGES`
  is retired. No migration (status/payload already existed). +33 tests (179 meso / 319 project-wide).
  Resume point → agent Phase 3 (designer agent-chat column).
- 2026-06-27 — **Agent Phase 3 done & merged** (PR #284, squash `5bfe754`; Django CI green, deployed to
  Hetzner — no migration; deployed `meso.js` serving the new chat confirmed live): the designer's
  agent-chat column goes **live**. The canned keyword intent engine (`detectIntent`/`applyIntent`/`dispatch` in `meso.js`,
  which matched the coach's text to one of four scripted edits and mutated the grid in place) is retired;
  a coach turn — typed or via a chip — now POSTs to `api/plan/<id>/agent/` (the Phase 1 endpoint) and the
  returned batch renders inline (per-change `title`/`before`→`after` under the summary) with a
  **"Review N changes →"** link to the review gate. The agent only **proposes** — the chat never mutates
  the program grid; changes stay inert until applied at review. Friendly fallbacks for 503/502/400/network
  errors; composer + chips disable while drafting. No backend change (the endpoint already existed); chat
  is **not persisted yet**. Tests (`test_designer_agent_chat.py`): no JS runner in-project, so they guard
  the retirement + real wiring at the source level + a render check. +8 tests (192 meso / 332 project-wide);
  local Codex review clean (1 round). Resume point → agent Phase 4 (background job + streamed status +
  golden eval cases).
- 2026-06-27 — **Agent Phase 4 built** (execution + eval; branch `meso-agent-phase4`): the proposal run
  moved **off the request thread**. `agent/service.py` split into `create_drafting_batch` +
  `run_proposal_job` (never raises — flips a `drafting` batch to `pending`/`failed`); `agent/jobs.py`
  dispatches it in a daemon thread deferred to `transaction.on_commit` (ATOMIC_REQUESTS visibility), with
  `MESO_AGENT_RUN_SYNC` for inline/deterministic test runs. The endpoint returns **202** + a `status_url`;
  the designer chat **polls** `GET api/batch/<id>/status/` (`meso.js` `pollBatch`) keeping the "drafting…"
  state up until the batch lands. **No real task queue** — a daemon thread is right-sized for a single
  short call behind the human gate on this box (Redis is cache/sessions only); a worker queue is a drop-in
  later behind `run_proposal_job`. Schema `meso.0005` (batch `drafting`/`failed` + `error`). **Logged
  sessions feed grounding** (`build_context.recent_logs` via `serialize_recent_logs`). **Golden evals**
  (`agent/evals.py` model-agnostic invariants responsive/grounded/safe + `manage.py meso_agent_eval`,
  side-effect-free, `--dry-run` without a key). Built red→green (+40 tests). **Closes the B6 agent slice**
  (only persisted chat thread + athlete-facing surfaces remain, both later slices).
- 2026-06-27 — **Agent Phase 4 merged & deployed** (PR #286, squash `82fd360`; Django CI green, deployed to
  Hetzner — migration `meso.0005_agentproposalbatch_error_and_more` applied in prod; local Codex review
  clean, 1 round; +40 tests, 219 meso / 359 project-wide). **The B6 agent slice is complete.** Resume point
  → either a **persisted chat thread** (saving the designer conversation, deferred since Phase 3) or the
  **athlete-facing slice** (delivery + logging PWA, then results feeding back to the agent — decisions S3/S7).
- 2026-06-27 — **Athlete-facing slice started** (decision: build item 3 of the suggested sequence — the
  athlete surface — over the persisted chat thread). Plan + phasing in [`athlete-plan.md`](./athlete-plan.md)
  (Phase 1 read surface · Phase 2 logging · Phase 3 results-feedback · Phase 4 PWA + notifications, S3/S7).
  **Phase 1 built** (branch `meso-athlete-phase1`): the athlete's own read surface — `AthleteHomeView`
  (`/meso/me/`) lists their active-coach, non-archived plans with each plan's latest **delivered** week +
  sessions (done/pending from the athlete's own `SessionLog`); `AthleteSessionView` (`/meso/me/session/<id>/`)
  renders one delivered session's prescribed grid read-only. Athlete-side scoping (`_athlete_plans` /
  `_athlete_session_or_404`) mirrors the coach's `_coach_plan_or_forbidden`: out-of-scope (foreign athlete /
  undelivered week / archived plan / unknown id) is a flat 404. Role-aware nav (overridable `navlinks` /
  `topnav_avatar` blocks; pure athletes redirected off the coach roster). **No model change / no migration**
  (`SessionLog`/`LoggedSet` already exist; B2 confirms athletes are Users who log in). Settles the **delivery
  contract**: delivery gates a week's *visibility*, contents stay live, the `WeekDelivery` snapshot is the
  deferred-diff record (Codex-review nit resolved this way — see `athlete-plan.md` design note). Built
  red→green: +20 tests (239 meso / 379 project-wide); local Codex review clean (1 round). Resume point →
  athlete Phase 2 (session logging — the write path that produces the rows `serialize_recent_logs` grounds
  the agent on).
- 2026-06-27 — **Athlete Phase 1 merged & deployed** (PR #288, squash `42bb805`; Django CI green, deployed
  to Hetzner — **no migration**; `/meso/me/` + `/meso/me/session/<id>/` live and login-gated in prod). The
  athlete read surface is live. Resume point → **athlete Phase 2** (session logging — the write path).
- 2026-06-27 — **Athlete Phase 2 built** (branch `meso-athlete-phase2`): the **write path** —
  `athlete_log_session` (`POST /meso/api/me/session/<id>/log/`) upserts the logged-in athlete's own
  `SessionLog` + `LoggedSet` rows (most-recent-wins, idempotent), flips the session done, stamps the date.
  Scoped by the read surface's `_athlete_session_or_404` (foreign/undelivered/archived/unknown → flat 404),
  **validated before any write** (`_clean_logged_sets`; bad input is a 400 that persists nothing), wrapped in
  a transaction (replace, not append, the set rows). The session screen becomes the interactive logger
  (`presenters.athlete_session` pre-fills set rows from the existing log carrying the coach's full target;
  `athlete_log_payload` + `athlete_session.html` + `meso_athlete.js`). **These are the first real rows
  `serialize_recent_logs` grounds the agent on** — the agent slice already consumed `recent_logs`, this
  produces them. **No model change / no migration** (`SessionLog`/`LoggedSet` already existed). Built
  red→green: **+33 tests** (272 meso / 412 project-wide), ruff clean. **Local Codex review: 0 blocking
  across 5 rounds**; nits fixed (set_number bound + render cap, duplicate-key reject, prescribed load/RPE
  shown, Save-progress keeps done, row-state sync, **workout-date preserved on later edits**). **Declined by
  design:** a `unique(session, athlete)` constraint — the model intentionally permits multiple logs per
  athlete/session (dated history that `serialize_recent_logs` + `test_recent_logs_are_capped_and_newest_first`
  rely on); the constraint would break grounding, and the re-save path is already idempotent. Resume point →
  **athlete Phase 3** (results feed back: retire `mockdata.RESULTS_*`, light up the designer's `last`/`adj`
  from real logs).
- 2026-06-28 — **Athlete Phase 4b built — PWA + web push (closes S7, and the S3 push half deferred from 4a)**
  (branch `meso-athlete-phase4b`). The athlete surface is now **installable, offline-tolerant, and
  push-capable**. *Installable (S7):* the manifest (`/meso/manifest.webmanifest`) and service worker
  (`/meso/sw.js`) are served as **views, not static files** — WhiteNoise's `CompressedManifestStaticFilesStorage`
  hashes static filenames (unstable URL + wrong scope for a worker), and a worker only controls pages at/below
  its own path, so it lives at `/meso/sw.js` to scope `/meso/`. The worker template resolves the **hashed**
  precache URLs via `{% static %}` so the cached shell auto-busts every deploy. Strategy: precache shell +
  offline page on install; network-first navigations (fall back to last-good cached page → offline page);
  stale-while-revalidate static GETs; POSTs pass through (the page owns offline writes). *Offline logging (S7):*
  `meso_athlete.js` stashes a save that fails on an unreachable network into a localStorage outbox (one per
  session, latest wins) and flushes it on `online`/load — safe because the log endpoint is idempotent (pinned
  by `TestOfflineReplayIsIdempotent`). *Web push (S3):* a `PushSubscription` model (**the one migration this
  slice**, `0006_pushsubscription`) the browser registers via `api/me/push/subscribe/` (caller-scoped, upsert
  by endpoint); `meso/push.py` signs with VAPID (`pywebpush`, **new dep**) and `notify_week_delivered` pushes
  the deliver payload to the athlete's devices — prunes a 404/410-Gone endpoint, swallows other failures, and
  is a **silent no-op without `MESO_VAPID_*` keys** (same graceful degradation as the no-address email). The
  deliver hook fires email **and** push, each independently best-effort on `on_commit`; athlete-only. PWA chrome
  + push subscribe-flow ride an athlete-only `pwa` block (`_pwa_head.html`), so **coach screens stay plain web**.
  Built red→green: **+43 tests** (`test_athlete_pwa.py`, `test_push.py`; 354 meso / 494 project-wide), ruff
  clean. Deployment verified locally (collectstatic + hashed SW precache under manifest storage,
  `makemigrations --check`). **Activating push in prod needs `MESO_VAPID_PUBLIC_KEY` / `MESO_VAPID_PRIVATE_KEY`
  / `MESO_VAPID_SUBJECT` provisioned** (see `docs/deploy-hetzner.md`); the migration + deploy succeed without
  them (push just stays dormant). Resume point → **the athlete slice is feature-complete** (install + offline
  logging + delivery email/push + results feeding the coach & agent). Open follow-ups: Background Sync,
  re-deliver push debouncing, in-app notification settings.
- 2026-06-28 — **Persisted designer chat thread built** (branch `meso-chat-thread`; the loose end deferred
  since agent Phase 3/4). The designer's agent conversation now **survives a reload**. Key realization: the
  thread is **already persisted, losslessly**, in the `AgentProposalBatch` rows — each coach turn is a batch
  (`instruction` = the coach's message, `summary` + the `ProposedChange` rows = the agent's reply,
  `status`/`created_at` = state + order), and the agent never sends free-form chat, so the batches **are** the
  thread. So we rebuild it rather than adding a model: `serializers.serialize_chat_thread(plan)` expands the
  plan's batches (oldest first) into the exact `meso.js` `messages` shape (failed → an error note; drafting →
  a neutral note carrying a `pollUrl`; else the summary + inline changes + a review link), the designer view
  injects it via `json_script`, and `meso.js` `hydrateThread()` replaces the lone greeting (kept only for an
  empty history), scrolls to the latest turn, and **resumes polling** a still-drafting run so a reload mid-run
  doesn't go stale. **No model, no migration** — same "reuse what exists, defer new tables" taste as the
  athlete slice. Built red→green: **+16 tests** (`test_chat_thread.py`; 379 meso / 519 project-wide), ruff
  clean. **Local Codex review: 0 blocking across 3 rounds → CLEAN** (two nits fixed: the drafting resume-poll
  and the scroll-to-latest). Plan in [`chat-thread-plan.md`](./chat-thread-plan.md). **Deferred:** a dedicated
  `ChatMessage` model (only if the agent ever sends text not tied to a batch) · editing past turns ·
  pagination of a very long thread. Resume point → next-slice options: **groups (S1)** is the main remaining
  Meso feature area.
- 2026-06-28 — **Groups slice (S1) started — Phase 1 built** (branch `meso-groups-phase1`). The
  tenancy-correct **group + membership spine** + the read surface, no shared program yet. `MesoGroup`
  (coach-owned: name, focus, status; `MesoGroupQuerySet.for_coach/active`) + `GroupMembership` (group ↔ an
  **active** `CoachAthlete` link, `unique(group, relationship)`); helpers `add_athlete` (off the coach's
  active link — raises on no-link / cross-coach / self; idempotent), `remove_athlete`, `active_member_users`
  (scoped to *active* links so an ended relationship hides the member **without deleting the row** — reopening
  the link restores them; read-side scoping, not deletion). Migration `meso.0007`. The roster's dead
  `groups = []` lights up off real rows (`presenters.roster_group`), and a coach-scoped `GroupDetailView`
  (`/meso/group/<id>/`) lists members + the folded "flags across group" (`presenters.group_detail`); the
  roster card links to it (was a dead designer link). Admin (group + membership inline), factories, and a
  seeded demo group ("Tue/Thu Strength Squad", 3 members) in `seed_meso_demo` (idempotent; torn down on
  `--delete`). **Membership tenancy is enforced two ways** (both from the Codex review): `active_member_users`
  scopes to `relationship.coach == group.coach` (a row written outside `add_athlete`, e.g. a raw admin inline,
  can't leak a foreign coach's athlete onto the read surface) and `GroupMembership.clean` rejects a cross-coach
  relationship + an inactive link **on creation only** (so a since-ended row stays re-savable). Built red→green:
  **+28 tests** (`test_groups.py` + seed coverage; 407 meso / 545 project-wide), ruff clean, `makemigrations
  --check` clean. **Local Codex review: 0 blocking → CLEAN** (3 rounds; two P2 membership-tenancy nits fixed).
  Plan + phasing in [`groups-plan.md`](./groups-plan.md). Resume point → **groups Phase 2** (the shared group
  program: `Plan.group` FK + nullable `relationship`, the designer's Group mode, create-group UI).
- 2026-06-28 — **Groups slice (S1) Phase 2a built** (branch `meso-groups-phase2a`). The **shared group
  program**: a `Plan` rooted at a `MesoGroup` instead of a `CoachAthlete` relationship. Phase 2 is split (like
  the athlete slice's Phase 4) into **2a** (this — the program spine + Group-mode designer) and **2b**
  (create-group-from-roster UI). `Plan.relationship` is now nullable and `Plan.group` (FK → `MesoGroup`,
  `related_name="plans"`) added, with a `plan_relationship_xor_group` `CheckConstraint` so a plan is rooted at
  **exactly one** of the two — the program tree is reused, gaining only a root (and, Phase 3, an override
  overlay). Migration `meso.0008`. **The load-bearing scoping decision:** `PlanQuerySet.editable_by(user)` is
  the wider designer/autosave gate (individual-active **or** group-owned), while **`for_coach` stays
  individual-only** so the athlete-shaped deliver/results/review flows never see a group plan (which has no
  single `athlete` — `Plan.athlete` returns `None`). `MesoGroup.shared_plan()/create_shared_plan()` (a starter
  scaffold — there's no add-session/week endpoint yet, so a bare plan would be uneditable). The designer opens a
  group plan (`MesoDesignerView` via `editable_by`), `serialize_plan` carries a `group` identity payload
  (`serialize_group_identity`: members + folded flags) and skips the athlete-scoped "last" column; `meso.js`
  hydrates Group mode off it (no more hardcoded squad; fabricated per-athlete adjusts → honest Phase-3
  placeholder). **Deliver + the agent reject a group plan with 400** (Phases 4/3; both deref `plan.athlete`);
  autosave is athlete-agnostic and works. A `group_design` POST entry point + the group-detail "Design / Open
  shared program" card; the seeded demo group gets a shared program. `initials` moved to `serializers` (avoids a
  presenters import cycle). Built red→green: **+29 tests** (`test_group_program.py` + seed coverage; 583
  project-wide), ruff clean, `makemigrations --check` clean. Plan + build notes in
  [`groups-plan.md`](./groups-plan.md). Resume point → **groups Phase 3** (per-athlete overrides: the `adj`
  overlay — `PrescriptionOverride`, effective-program resolution, the designer's per-row `adj` badge), then
  **Phase 2b** (create-group UI) and **Phase 4** (deliver-to-all).
- 2026-06-28 — **Groups slice (S1) Phase 3 built** (branch `meso-groups-phase3`). **Per-athlete overrides —
  the `adj` overlay.** A `PrescriptionOverride(membership FK → GroupMembership, prescription FK →
  ExercisePrescription, swap_name, load_pct, sets, reps, note)` with `unique(membership, prescription)` is a
  thin diff layered on a group's *shared* `ExercisePrescription`: a member's **effective program = shared
  template + their override diffs** (no second hierarchy). It hangs off the `GroupMembership` (so the same
  `CoachAthlete` link that owns the member's individual plans — D-a), guarded by a **same-group invariant**
  (the override's prescription must live in the membership's group's shared program — `set_override` raises
  `InvalidTransition`, `clean` backstops the admin). Migration `meso.0009`. `serializers.resolve_prescription`
  is the pure resolver (swap replaces the name, `load_pct` scales a numeric load 2.5-rounded à la the
  designer's `round25`, `sets`/`reps`/`note` override volume/note); `group_adjustments` is one query over the
  plan's overrides scoped to **active** members → a per-row `adj` summary (`"{initials} {label}"` for one,
  `"N adjusts"` for several) + an `adjusts` breakdown, which `serialize_plan` attaches to group-plan grid rows
  (the group analogue of the individual plan's logged `last` column). `prescription_override` (POST
  `/meso/api/plan/<id>/prescription/<pk>/override/`) sets/clears one member's adjust — group-only (individual
  → 400), coach-scoped (403), prescription-in-plan (404), active-member (400), `load_pct` bounded; the reply
  carries the recomputed row `adj` so the badge repaints. The designer grid renders the badge off real diffs
  (per-athlete breakdown on hover); the seeded demo group gets a few overrides (idempotent). **No in-grid
  override *editor* yet** — the badge renders off seed/admin/API-created diffs; the click-to-adjust UI is the
  immediate follow-up. Built red→green: **+41 tests** (`test_group_overrides.py` + seed coverage; 625
  project-wide), full suite + 30 JS tests green, ruff + format clean, `makemigrations --check` clean. Plan +
  build notes in [`groups-plan.md`](./groups-plan.md). Resume point → the **override editor UI** (click a row
  to set a member's adjust), then **Phase 2b** (create-group UI) and **Phase 4** (deliver-to-all — fan a
  per-athlete *resolved* snapshot out to each member, reusing `resolve_prescription`).
- 2026-06-28 — **Groups slice (S1) COMPLETE** (Phases 2b + 3-editor + 4 all built, merged & deployed — PRs
  #301/#302/#303). The whole Meso feature area (individual coach + agent + athlete PWA + groups) is now real
  & deployed; `mockdata.py` is gone. **Next slice chosen by the user: S2 (units & RPE/%1RM).** Units (kg/lb)
  turned out already shipped with earlier slices (`Unit`/`CoachProfile.default_unit`/`Plan.unit`, threaded
  through serializers/presenters/designer/seed); the remaining gap is **first-class %1RM** — the designer's
  Load number always meant an absolute load, with no way to prescribe "75% of 1RM" (RPE already has its own
  orthogonal column). **S2 Phase 1 building** (branch `meso-units-rpe-phase1`): a `LoadType`
  (`ABSOLUTE`/`PERCENT`) + `ExercisePrescription.load_type` (default ABSOLUTE — data-safe) carried through
  the serializer, the per-athlete override resolver, and the group deliver fan-out; the designer Load cell
  toggles `%` ⇄ the unit and autosaves the type; the athlete sees a `%` target and the coach results screen
  labels a %1RM target with `%`. Migration `meso.0011`. Agent %1RM-awareness deferred to Phase 2 (the agent
  is type-agnostic — a %1RM number progresses as a number). Plan + phasing in
  [`units-rpe-plan.md`](./units-rpe-plan.md).
- 2026-06-28 — **S2 Phase 2a — agent %1RM-awareness** (branch `meso-units-rpe-phase2a-agent`, **no
  migration**). Phase 2 split 2a/2b (groups-slice cadence). The agent grounding already carried each row's
  `load_type` (Phase 1 wired `serialize_prescription`), so the two real gaps were the **prompt** (never
  explained `load_type`) and the **validation backstop** (never bounded a %1RM progression). Closed both:
  `SYSTEM_PROMPT` + the `new_load` tool field now explain `abs` vs `pct` (%1RM); `clean_change` bounds a
  `progress` on a `PERCENT`-typed target to `0 < pct ≤ 120` (rejects an absolute-looking "180" or a
  non-numeric value, normalizes `'82.5 %'` → `'82.5'`), leaving the absolute path unbounded. The agent still
  does **not** change a row's type. Athlete %1RM logging ergonomics remain → **Phase 2b**. Plan in
  [`units-rpe-plan.md`](./units-rpe-plan.md).
- 2026-06-28 — **S2 Phase 2b — athlete %1RM logging ergonomics → S2 COMPLETE** (branch
  `meso-units-rpe-phase2b`, **no migration**). A %1RM target is an *intensity, not a weight*: Phase 1 let
  the athlete *see* the `%`, but converting "75%" to a bar load was still manual. Phase 2b adds an
  **estimated-1RM helper** (% ⇄ load). The data contract is the only backend change — `athlete_session`
  carries the plan's `unit`, and `athlete_log_payload` threads `unit` + each row's structured
  `load`/`load_type` — so the client knows which rows are %1RM. The maths is **client-side** (the athlete's
  1RM estimate is per-device convenience, not coach-owned program data): `meso_athlete.js` gains pure
  helpers (`epleyOneRm` — Epley, single-rep = the load itself; `loadForPercent` — plate-rounded à la the
  designer's `round25`) + component methods (`isPercentLift`/`suggestedLoad`/`setImpliedOneRm`), with the
  estimate persisted in **localStorage** keyed by exercise id (same "defer new tables" taste as the offline
  log queue). The logger renders a `%1RM` badge, a "your 1RM" input, the suggested load (`75% ≈ 90 kg`), and
  a per-set implied-1RM hint — all gated on a %1RM lift (absolute lifts untouched). A `LoggedSet` still
  records the *actual* (absolute) weight. Built red→green: **+3 pytest** (566 meso) + **+14 Vitest** (60
  frontend), ruff + prettier clean, `makemigrations --check` clean. **Local Codex review: CLEAN on
  iteration 1.** **Deferred:** a persisted/coach-visible estimated 1RM (model + migration) and
  auto-deriving it from logged history. **The whole Meso feature area is now real & deployed; S2 is
  complete — no obvious next big slice, ask the user.** Plan in [`units-rpe-plan.md`](./units-rpe-plan.md)
- 2026-06-28 — **S2 follow-up: persisted, auto-derived, coach-visible 1RM — Phase 1 built** (branch
  `meso-one-rm-phase1`). The deferred Phase-2b follow-up: the athlete's estimated 1RM lived only in
  per-device `localStorage`. Phase 1 promotes it to a real **`AthleteOneRm`** row (one per
  `(athlete, lift)`; lift identity = the B4 hybrid `_exercise_key`, denormalized into a `key` with
  `unique(athlete, key)`), **auto-derived from the athlete's completed logged sets** — the best Epley
  estimate per lift (`one_rm.py`: `epley_one_rm` mirrors `meso_athlete.js` exactly, `derive_one_rm_values`
  / `refresh_one_rms` / `one_rm_values`). The log endpoint refreshes the rows on every *done* save;
  migration `0012_athleteonerm` (schema) + `0013_backfill_one_rms` (derives from existing history,
  idempotent). **Two surfaces:** the athlete logger seeds its suggested bar load from the derived value
  (a typed per-device override still layers on top — `effectiveOneRm`), and the coach designer shows a
  `1RM: 140 kg` badge on an individual plan's `%1RM` row (`serialize_plan` threads it; a group plan has
  no single athlete, so none). The seed derives Maya's Box Squat 1RM (84) so the demo shows it. Built
  red→green: **+33 pytest** (`test_one_rm.py`; 600 meso / 740 project-wide) + **+5 Vitest** (65 frontend)
  + a seed assertion, ruff + format clean, `makemigrations --check` clean. Plan in
  [`one-rm-plan.md`](./one-rm-plan.md). **Deferred:** manual entry persisted server-side (a `source` field
  + endpoint — today logs only *raise* the estimate), coach-editable 1RM, smarter derivation / unit
  conversion..
- 2026-06-28 — **S2 follow-up — Phase 2: manual, server-persisted 1RM built** (branch
  `meso-one-rm-phase2`). Closes the first deferred item: the athlete's *typed* 1RM override (per-device
  `localStorage` since Phase 2b — lost on a device change, invisible to the coach, and able only ever to
  *raise* the suggestion) is now a real **`AthleteOneRm.source`** (`logged`/`manual`, default `logged`;
  migration `0014_athleteonerm_source`, schema-only — existing rows were all auto-derived). A `manual` row
  is the athlete's own number: **`refresh_one_rms` skips it** (logs never clobber a manual value, so it can
  sit *below* the heaviest logged set — the thing localStorage couldn't express server-side); a `logged`
  upsert stamps `source=logged`. `one_rm.set_manual_one_rm` upserts the manual row or, on a blank value,
  **clears** it — deleting the manual row and re-deriving from logs immediately so the lift falls back to
  its log-derived estimate (`clean_manual_value` is the reusable validator: blank → clear, positive +
  column-bounded → quantized, else reject). New endpoint **`POST /meso/api/me/session/<pk>/one-rm/`**
  (`{prescription, value}`) scoped exactly like the log endpoint (`_athlete_session_or_404` — a foreign /
  undelivered / unknown session is a flat 404, an out-of-session prescription a 400), returning
  `{one_rm, source}`. The logger payload carries `one_rm_source` + `one_rm_url`; `meso_athlete.js` seeds a
  manual value into the editable input, keeps a logged value as the placeholder, and the input's `@input`
  is now a **debounced best-effort server POST** (`saveOneRm`/`_postOneRm`) — the `meso-e1rm` localStorage
  store is **retired**. Admin surfaces `source` (`list_display` + `list_filter`). Built red→green: **+28
  pytest** (`test_one_rm.py`; 664 meso / 776 project-wide) + **+8 Vitest** (70 frontend, net), ruff +
  prettier + `makemigrations --check` clean. Plan in [`one-rm-plan.md`](./one-rm-plan.md). **Deferred
  (Phase 3+):** coach-editable 1RM (the `source` field already supports it), offline persistence of a
  manual edit, smarter derivation / cross-unit conversion.
- 2026-06-29 — **N4 — athlete onboarding / email invites — Phase 1 built** (branch
  `meso-invites-phase1`). Closes the still-open foundation decision: how an athlete *joins* a
  coach. The Phase-1 peer-invite state machine on `CoachAthlete` required the athlete to already
  be a `User` and nothing in the UI ever *created* an invite, so links were only ever seeded.
  Phase 1 adds the real email-onboarding loop end to end. New **`CoachInvite(coach, email, token,
  status)`** (migration `0015_coachinvite`): a coach invites an *email* (the invitee may have no
  account yet — decoupled from `CoachAthlete`, whose `athlete` FK is non-null, rather than making
  the load-bearing relationship nullable). State machine `open_for`/`accept`/`decline`/`revoke`;
  **`accept(user)` materializes — and immediately activates — a `CoachAthlete`** (the claim *is*
  the acceptance), idempotent against an existing active link, resolves a pending peer link, and
  rejects a coach claiming their own invite; partial-unique `(coach, email)` while pending so a
  re-invite reuses the open row. **Bearer-token claim, no email match** — the 122-bit token in the
  link authorizes (email-only login coexists with social signup, so a new athlete may use a
  different address); the coach sees who accepted and can `end` it. **Rides allauth with no custom
  adapter**: the claim view is `@login_required`, so an anon visitor bounces to
  `/accounts/login/?next=<claim>` and allauth carries `next` through both login and signup
  (`ACCOUNT_EMAIL_VERIFICATION` unset → `"optional"` → signup logs in immediately). Email via
  `notifications.send_coach_invite_email` (+ 3 templates), best-effort on `transaction.on_commit`.
  Roster gains an "Invite an athlete" disclosure + a pending-invite list with Revoke; admin +
  factory + a seeded demo pending invite. Built red→green: **+38 pytest** (`test_invites.py`) + 3
  seed assertions; full suite green (the 2 pre-existing `admin_honeypot` failures are unrelated).
  **Codex review loop CLEAN after iteration 1** (2 fixes: a P1 referrer-leak — moved the
  `no-referrer` meta into a new `_meso_base` `head_top` block that precedes the font `<link>`s; a
  P2 claim race — `select_for_update` on the invite row in the claim/revoke views). Plan +
  deferred (athlete→coach request UI, resend/expiry, stub-athlete) in
  [`invites-plan.md`](./invites-plan.md).
- 2026-06-29 — **N4 — athlete onboarding / invites — Phase 2 built** (branch
  `meso-invites-phase2`, **no migration**). Closes the bidirectional half the relationship spine
  always supported in the model (`CoachAthlete.request` → `pending_athlete_request`) but never in
  the UI: an athlete who already has an account asks to train under a coach, the coach
  accepts/declines on the roster, and either party sees the pending state on their own surface.
  New `CoachAthlete.initiator()` (mirror of `recipient()` — who may *withdraw* a pending link).
  **`athlete_request_coach`** (`POST /meso/request/`): resolves the posted email to a *coach* (a
  `User` with a `CoachProfile`, excluding self), rejecting unknown/non-coach/own; an already-active
  link is untouched, an already-pending request (or coach-invite already awaiting the athlete) is a
  friendly no-op, else `request()` opens/reopens; emails the coach best-effort on
  `transaction.on_commit`. **`request_withdraw`** (`POST /meso/request/<token>/withdraw/`):
  initiator-only (recipient/stranger → 403), pending-only → declined. The coach's accept/decline
  rides the **existing** `invite_accept`/`invite_decline` recipient views unchanged (a request's
  recipient *is* the coach). `notifications.send_coach_request_email` (+ 3 templates), mirror of the
  invite email. Surfaces: the roster gains a pending-request list (Accept/Decline), the athlete home
  gains a "Your coaches" card (incoming invites + sent requests + a request-a-coach form).
  **Routing change:** `RosterView` now sends *any* non-coach to `/meso/me/` (coach = `CoachProfile`
  **or** a coach-side link **or** a sent invite), so a brand-new athlete (or one merely awaiting an
  invite) reaches the request form instead of an empty coach roster. Seeded a demo pending request
  (`hopeful@example.com`) so the surface shows on a fresh DB (idempotent + torn down). Built
  red→green: **+34 pytest** (`test_requests.py`) + 3 seed assertions; full suite green (867).
  **Codex review loop CLEAN on iteration 1.** Plan + deferred (resend/expiry, stub-athlete,
  attribution) in [`invites-plan.md`](./invites-plan.md).
- 2026-06-29 — **N4 — invites — Phase 3 built** (branch `meso-invites-phase3`). Invite
  *lifecycle*: a TTL + an explicit resend, closing the top deferred item. **One migration**
  (`0016_coachinvite_expiry`): `CoachInvite.expires_at` + a new `Status.EXPIRED`. A fresh invite
  is stamped `now + INVITE_TTL` (14 days) by `open_for`; a **null** clock = never expires (legacy
  rows stay claimable — data-safe, no backfill). `is_claimable = is_pending and not is_expired` is
  the single gate the claim path enforces. `expire()` (`pending → expired`, past-due only) fires
  **lazily** (the claim view ages out an overdue link on view; `accept()` flips + refuses one as a
  backstop, so a stale token can never materialize a link) and in **bulk** via a new
  `meso_expire_invites` management command (cron-friendly sweep of `overdue()`, `--dry-run`).
  `resend()` re-arms an outstanding invite — **new token** (old emailed link dies — the Phase-3
  decision), reset clock, `expired → pending`; `open_for` reuses an outstanding (pending/expired)
  row and re-arms a stale one (no duplicate pending+expired pair); `revoke()` broadened so a coach
  can dismiss an *expired* invite too. New `POST /meso/invite/<token>/resend/`
  (`coach_invite_resend`): coach-scoped (404), row-locked, best-effort email on `on_commit`,
  answered-invite no-op (no 500). Querysets `claimable`/`overdue`/`outstanding`; the roster lists
  `outstanding()` (an expired one reads "Expired" + offers Resend); the claim page gains an
  "expired" state; admin lists `expires_at`; the demo invite seeds via `open_for` (real TTL).
  Built red→green: **+38 pytest** (`test_invite_lifecycle.py`); full project suite 904 + 83 Vitest
  green. Plan + deferred (configurable TTL, expiry reminder, cron scheduling, stub-athlete) in
  [`invites-plan.md`](./invites-plan.md).
- 2026-06-29 — **Agent job → django-q `async_task` built** (branch `meso-agent-django-q`,
  **no migration**). Closes the top deferred item of the scheduling plan: `meso/agent/jobs.py` ran
  the proposal job on a bare daemon thread because there was no queue; now that django-q2 + the
  `qcluster` exist (the invite sweeps' scheduler), the agent job rides that same cluster.
  `dispatch_proposal` enqueues `run_proposal_job` (the unchanged unit of work) via `async_task`
  **on commit** — so a worker in another process never races the not-yet-committed drafting batch,
  and a rolled-back request enqueues nothing. **Only the batch id is enqueued**: the worker is a
  separate process that rebuilds its own Claude client (`get_default_client` off the shared `.env`
  `ANTHROPIC_API_KEY`), and a client isn't picklable. The dotted path lives in one constant
  (`RUN_PROPOSAL_TASK`) covered by an end-to-end test that runs the enqueued job under django-q's
  `sync` mode (catches a rename that would break dispatch silently). `MESO_AGENT_RUN_SYNC` still runs
  the job inline (tests + any queue-free env); a broker-write failure resolves the batch to `failed`
  rather than stranding it `drafting` (mirrors the service's "never leave a batch stuck drafting"
  invariant). No compose change — the `qcluster` already runs and shares web's image + `.env`. Built
  red→green: **+3 pytest** (`test_agent_jobs.py` `TestDispatch`, net; the daemon-thread test
  retired). Plan + remaining deferred in [`scheduling-plan.md`](./scheduling-plan.md).
- 2026-06-29 — **"What's next?" YAGNI review + delivery-email opt-out built.** With the whole
  Meso area shipped and no obvious next big slice, each remaining candidate was reviewed
  one-by-one (a skeptical agent per candidate, grounded in the real code) to decide *build now* vs
  YAGNI. **Verdicts:**
  - **S4 (results ↔ records) — skip.** The "results screen shows a PR" premise was mock-only (the
    real results screen never showed a PR; the lone PR string lived in a retired roster mock), and
    `challenges.Record` is a `DurationField`/time-based model — wrong domain for a strength PR.
    Meso already owns best-lift data (`AthleteOneRm`). Embedded modeling question resolves for
    free: **keep separate**; build a PR surface only on a concrete need.
  - **S5 (real-time transport) — skip.** The agent emits one atomic tool-call result behind the
    human review gate (no partial output to stream), the status poll is cheap + bounded (1.5s,
    ≤40 attempts), and the stack is pure WSGI. SSE/websockets would pull in ASGI + channels + a
    Redis channel layer on a `noeviction` box for a ~1.5s cosmetic win. Revisit only if the agent
    becomes genuinely streaming.
  - **Deferred follow-ups — 5 of 6 skip:** cross-unit 1RM (no in-app way to change a plan's unit),
    1RM `set_by` attribution (no consumer), push re-deliver debounce (already mitigated by the
    push `tag` collapse), invite configurable-TTL (one-line constant), invite stub-athlete (a
    *deliberately rejected* design — `decisions.md` N4). **The 1 that made the cut → built (below).**
  - **S6 (billing) — re-added to the plan, NOT skipped.** The review found Meso is single-operator
    *today* (no self-serve coach signup, no subscription infra), but the owner's direction is that
    Meso is **not** to be planned as a single-operator tool — multi-coach SaaS with paid coaches is
    intended. So billing is promoted to the **next major slice** (suggested-sequence item 4); it
    needs the pricing/packaging product decisions first, then an additive Stripe-subscription
    build. (A `billing-plan.md` should capture those decisions before code.)
- 2026-06-29 — **Delivery-email opt-out built** (branch `meso-email-unsubscribe`, migration
  `0019_athleteprofile_delivery_email_opt_out`). The one follow-up that made the YAGNI cut: the
  delivered-week email — the single transactional message a coached athlete receives — had **no
  off switch** (web push is opt-in via the browser permission; email was not). Built the email
  best-practice: a working, login-free, RFC 8058 **one-click `List-Unsubscribe`**. New
  `meso/unsubscribe.py` mints a signed token (`django.core.signing`, no token column, no expiry)
  naming the athlete; `send_week_delivered_email` (switched `send_mail` → `EmailMultiAlternatives`)
  sets `List-Unsubscribe` + `List-Unsubscribe-Post` headers and a visible footer link when given an
  `unsubscribe_url`. The `unsubscribe_delivery_email` view (`/meso/unsubscribe/<token>/`,
  `@csrf_exempt`, login-free) **never mutates on GET** (scanners/prefetchers issue GETs → a confirm
  page); POST (one-click or the human form) flips a single **`AthleteProfile.delivery_email_opt_out`**
  flag. The deliver hook (`_notify_athlete_delivered`) gates the email on the flag — an opted-out
  athlete is emailed nothing, but **push still fires** (separate channel) and delivery still
  succeeds. Intentionally *not* a notification-preferences system — one flag for the one email that
  needed an off switch. Admin surfaces + filters the flag. Built red→green: **+16 pytest**
  (`test_unsubscribe.py`); ruff + format + `makemigrations --check` clean.
- 2026-06-29 — **First-time UX / onboarding slice planned** (not built; plan in
  [`first-time-ux-plan.md`](./first-time-ux-plan.md)). The feature area is broad
  and deployed but has never had an onboarding pass. The plan covers all three
  first-timers (cold visitor · new coach · new athlete) and surfaces the
  **headline blocker**: a coach **cannot create an individual program in the UI** —
  `Plan.objects.create` lives only in `MesoGroup.create_shared_plan`
  (`models.py:1488`), there's no individual-plan / add-week / add-session endpoint,
  and both "+ New program" and "Build a program" CTAs bounce off the bare designer
  back to the roster; only `seed_meso_demo` builds an individual plan tree. Phased
  fix (Phase 1 = individual plan creation, the structural fix; 2–5 = front
  door, empty states, role fork, athlete + designer first-run polish). Reconciled
  with the post-#311 routing (`RosterView` now sends any non-coach to `/meso/me/`,
  so the new-coach gap is now *reaching the coach surface at all*; #311's
  athlete→coach **request** loop already covers most of Phase 4's athlete-initiated
  item — distinct from Q4's "become a coach / beta access").
  **Decisions Q1–Q4 resolved (2026-06-29):** **Q1** coaches = **closed beta /
  allowlisted** (`CoachProfile` auto-creates on first coach action for an
  allowlisted user; open self-serve deferred to billing S6 — no billing yet + a
  per-coach Claude cost + a single box make open signup premature; the YAGNI-review
  entry above promotes **billing/S6 to the next major slice**, which is exactly when
  open coach self-serve becomes viable); **Q2** plan creation = **blank editable
  scaffold first**, optional agent draft as a fast follow (template library
  deferred); **Q3** = **yes** to a removable one-click demo (no demo-athlete
  email/push); **Q4** = a **logged-out `/meso/` landing** with "I have an invite" +
  "Request coach access" + one main-site link (not instant-signup). Q1 is the most
  consequential and the easiest to revisit.
- 2026-06-29 — **S6 billing — decisions locked + plan written** (no code yet; planning only).
  Worked through the billing decision set with the owner. **Locked:** D1 the **coach** pays (B2B);
  D2 **per-active-athlete (seat) pricing** (a seat = an active `CoachAthlete` link); D3 **free tier
  + a 14-day no-card trial** (the trial is **local state** — no Stripe until a card is actually
  collected — and the free tier is the lapse/cancel landing spot); D4 the paywall gates **athlete
  count + the AI agent** (the Claude agent has real per-call cost → free tier gets no agent;
  trial/paid/comped get it; groups + notifications stay free; both gates share the `is_active`
  predicate); D5 **monthly/USD** (annual
  deferred); D6 Stripe Smart Retries then **downgrade to free at period end**, over-limit blocks
  new athletes + edits but **never deletes**; D11 first slice = the **subscription spine** for
  existing coaches (public self-serve coach signup is a later phase). **Recommended architecture
  (proceed unless overridden):** Stripe Billing + subscription Checkout + the hosted **Customer
  Portal**; Stripe is source-of-truth with a thin local **`CoachSubscription`** mirror (1:1 → coach
  `User`) for fast gating; a **separate clean billing webhook** (the messy products webhook is left
  alone); one `billing/access.py` accessor (`can_add_athlete`); seat-quantity sync best-effort +
  a daily **`reconcile_seats` qcluster sweep** (reuses the django-q2 cluster); a **`comped`** status
  so the owner + demo coaches are never paywalled. **5-phase build** in
  [`billing-plan.md`](./billing-plan.md) — Phase 1 (this slice) = `CoachSubscription` model +
  migration + the gating accessor + local trial + comped seed/admin, **no Stripe, no enforcement
  wired** (same state-machine-first order as the invite slice). **Open values** (not architecture):
  free seat count (rec 1), per-seat price (TBD — owner's number), trial 14d.
- 2026-06-29 — **First-time UX — Phase 1 built & merged** (PR #326): individual
  plan creation, the headline structural fix (`Plan.scaffold` + `CoachAthlete.create_plan` /
  `working_plan` + `plan_create` / `session_add` endpoints + wired CTAs). A real coach can now
  build an individual program in the UI with no seed. Plan in
  [`first-time-ux-plan.md`](./first-time-ux-plan.md).
- 2026-06-29 — **First-time UX — Phase 2 built** (branch `meso-first-time-ux-phase2`): **coach
  first-run — one-click demo + empty-state teaching** (Q3). `meso/demo.py`
  (`load_demo` / `clear_demo` / `has_demo`) is a coach-scoped, idempotent wrapper over the
  `seed_meso_demo` data that stands up five demo athletes + a built/delivered/logged individual
  program + a group (shared program + a couple of per-athlete overrides), **namespaced per coach**
  (non-routable `@<coach-hex>.demo.invalid` addresses, collision-free across coaches). New
  **`is_demo`** flag on `CoachAthlete` + `MesoGroup` (migration `0022`) makes demo data **clearly
  labeled** (roster banner + per-row "Demo" badge), **fully removable** (`clear_demo` deletes the
  demo group + demo athlete users, cascading their links/plans/logs/memberships/overrides), and
  **billing-neutral** — a new `CoachAthlete.billable()` (= `active().exclude(is_demo=True)`) backs
  `access.active_seat_count` / `suspended_athlete_ids` so loading the demo never trips the paywall
  or suspends real athletes. **No demo-athlete email/push:** the load delivers at the model layer
  (no notification), the addresses are non-routable, and each carries `delivery_email_opt_out`.
  `POST /meso/demo/{load,clear}/`; the empty roster gets a first-run onboarding card (teach
  Invite → Build → Deliver + the demo CTA); `_meso_base.html` now renders flashed messages
  (previously swallowed on every meso page). **Q1 dropped** — its "allowlisted-coach access"
  premise (*no billing yet*) was obsoleted by billing S6 Phase 4 (#323, open self-serve coach
  signup), so the closed-beta allowlist was **not** built (it would contradict shipped behavior).
  Built red→green: **+23 pytest** (`test_demo.py`); ruff + `makemigrations --check` clean.
  Resume point → first-time-UX **Phase 3** (anon `/meso/` landing + main-site link).
- 2026-06-29 — **First-time UX — Phase 3 built** (branch `meso-first-time-ux-phase3`,
  PR #329, **no migration**): **the front door** (anonymous visitor +
  discoverability). `/meso/` was login-gated (`RosterView(LoginRequiredMixin)`), so a
  cold visitor met a bare login wall and Meso was linked from **nowhere** on the main
  site. `RosterView` now **splits on auth**: an anonymous visitor renders the new
  login-free `meso/landing.html` (what Meso is + two honest entry actions — *log in as
  an athlete*, carrying `?next=` back to `/meso/me/`, and *become a coach* via the
  #323 funnel) instead of bouncing to `/accounts/login/`; an authenticated visitor
  keeps the post-#311 role routing (coach → roster, anyone else → `/meso/me/`)
  untouched (the authenticated branches read `request.user` only after the anonymous
  one returns). A discreet **"Coaching"** link in the main-site nav (`_nav.html`)
  makes Meso discoverable without already knowing the URL. **Q1's closed-beta
  "Request coach access" was not built** — obsoleted by #323's open self-serve signup
  (same reconciliation as Phase 2); the coach entry action is the plain *become a
  coach* path, and the **become-a-coach-from-athlete-home** item already shipped in
  #323 (`athlete_home.html`'s "Are you a coach?" card), so Phase 3 narrowed to the
  anon landing + the main-site link. Built red→green: **+11 pytest**
  (`test_landing.py`) + the stale `test_roster_requires_login` repurposed to
  `test_anonymous_sees_landing_not_login`; full project suite green, ruff + format +
  `makemigrations --check` clean. **Codex review loop CLEAN on iteration 1.** Resume
  point → first-time-UX **Phase 4** (athlete install/first-log polish) **or Phase 5**
  (designer/agent self-explanation). Plan in
  [`first-time-ux-plan.md`](./first-time-ux-plan.md).
- 2026-06-30 — **First-time UX — Phase 4 built & merged** (branch
  `meso-first-time-ux-phase4`, PR #330, **no migration**): **athlete first-run
  polish** — a PWA **install prompt** + a one-time **first-log coachmark**. The
  coachmark is **server-driven**: `views._athlete_has_completed_log(user)` gates it
  on the home (only when a delivered session exists to tap) and the session logger,
  so it's naturally one-time + cross-device and vanishes the moment the first
  *completed* log lands — **no per-device flag, no migration**. Gated on a `done`
  log specifically (a "Save progress" `pending` draft must not retract the hint that
  teaches the final "Log session" step). New **`meso_onboarding.js`** reveals the
  install card from a captured `beforeinstallprompt` (Chromium), falls back to manual
  *Add to Home Screen* steps on iOS (incl. **iPadOS-13+ Safari's desktop "Macintosh"
  UA** via `detectIOS(ua, maxTouchPoints)`), stays hidden when already
  installed/dismissed, and persists manual coachmark dismissals. Visibility toggles
  via inline `style.display`, **not** the `hidden` attribute — an inline `display:flex`
  carried for layout beats the UA `[hidden]` rule (the existing push CTA has this
  latent bug, masked only because push is unconfigured in prod). The script is added
  to the SW **PRECACHE** (cache bumped **`v1`→`v2`**) so it works offline like the
  other athlete scripts. Built red→green: **+12 pytest** (`test_athlete_onboarding.py`
  + a precache guard in `test_athlete_pwa.py`) + **+13 vitest**
  (`meso_onboarding.test.js`); 1035 meso pytest + 99 vitest green. **Codex review loop
  CLEAN after 3 fix iterations** (iPadOS UA detection → done-log gating → SW precache).
  **Prod-verified:** `/meso/sw.js` now serves `CACHE = "meso-pwa-v2"` + the hashed
  `meso_onboarding.*.js` (HTTP 200). Resume point → first-time-UX **Phase 5**
  (designer/agent self-explanation) **or** the add-week/week-switcher deferral. Plan in
  [`first-time-ux-plan.md`](./first-time-ux-plan.md).
- 2026-06-30 — **First-time UX — Phase 5 built** (branch `meso-first-time-ux-phase5`,
  **no migration**): **designer & agent self-explanation** — the **last first-time-UX
  phase**. The designer is a self-contained Alpine page that shipped a pile of
  *prototype chrome*: a hardcoded fake athlete (a stock name + invented
  contraindications), a fabricated "Coach's programming style" block, and a hardcoded
  macrocycle — **all rendered over whatever real plan the coach opened**. A first-time
  coach also got no orientation: nothing said the grid autosaves, that the agent only
  *proposes* (changes wait at the review gate), or that the phone column is the
  athlete's real view. Three parts: **(1) Coachmarks** — three **dismissible** first-run
  notes anchor the designer's regions (week grid · agent · phone preview); they show
  until dismissed, the dismissal persisting client-side in `localStorage` (`meso.js`,
  namespaced `meso-coachmark-designer-<key>` so it never collides with the athlete
  onboarding coachmarks' `meso-coachmark-` prefix) — **no server "seen" flag**, like
  the athlete chrome. **(2) Agent self-explanation** — a **persistent** "propose →
  review → apply" note under the agent header makes the review gate explicit for
  *everyone* (not just first-timers — a newcomer won't expect the agent to only
  propose); individual-only (the group agent's composer is hidden). **(3) Real chrome**
  — `serialize_plan` now carries the individual plan's **real athlete identity**
  (`serialize_athlete_identity`: name / initials / goal / **active** contraindications,
  the same global injuries the agent grounds on), and the left-rail athlete card,
  macrocycle rail, top-bar identity/chip, and week/block headers render the real
  `athlete`/`weeks`/`phases` (new `meso.js` getters `currentWeek`/`currentPhase`/
  `cycleLabel`/`weekHeading`/`blockHeading`). The invented coach-preferences block was
  **removed** as misleading; group mode (already real, via `group`) is untouched —
  `serialize_athlete_identity` returns `None` for a group plan. Built red→green: the
  dismiss logic is unit-tested in `frontend/meso.test.js` (**+5 vitest**), the server
  seam (serializer identity, rendered coachmarks + note, absence of the fabricated
  chrome, `meso.js` dismiss API) in `test_designer_onboarding.py` (**+10 pytest**);
  1055 meso pytest + 104 vitest green, ruff + format clean, DjHTML clean. **Gotcha
  (cost me 2 cycles):** the render tests assert the fabricated strings are *gone*
  (`"Maya"`, `"programming style"` ∉ body) — my own explanatory template comments
  mentioning those strings tripped the asserts, so dev-facing comments must avoid the
  very tokens the tests forbid. **Codex review loop CLEAN on iteration 1.** This
  **completes the first-time-UX slice** (Phases 1–5). Remaining Meso backlog: the
  **add-week / week-switcher** deferral (designer is single-current-week) and **S6
  billing Phase 5 annual prices** (blocked on the owner's per-seat number + a Stripe
  annual Price). Plan in [`first-time-ux-plan.md`](./first-time-ux-plan.md).

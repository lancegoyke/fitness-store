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

### N4 · Athlete onboarding / invites 🟡
How an athlete joins a coach: coach invites by email → athlete signs up (allauth) → link
created; or coach creates a stub athlete and sends a claim link. **Rec:** email invite +
claim, reusing allauth. Detailed design when we build the relationship.

---

## Secondary decisions (resolve per-slice; not blocking the foundation)

| # | Decision | Note |
|---|----------|------|
| S1 | **Groups** — "shared program + per-athlete auto-adjust" modeling (template + override diffs) | Defer until individuals work |
| S2 | **Units & RPE vs %1RM** | Per-athlete/coach setting; needs a home |
| S3 | **Delivery & notifications** | Push needs PWA + push infra; email via existing `django-ses` + `notifications` app |
| S4 | **Results ↔ `challenges`/records** | Results screen shows a PR — reuse the records model or keep separate? |
| S5 | **Real-time transport** | HTMX polling vs SSE/websockets for chat/drafting |
| S6 | **Billing** | Paid/sub feature (Stripe is here) or internal-only? Tied to B1 |
| S7 | **Offline logging** | Gym wifi is bad — does the athlete logger need offline/PWA? |

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
   back to the agent.

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

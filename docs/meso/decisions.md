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
- **Status:** 🟡 Proposed (detailed design after B1–B3).
- **Decision:** _tbd_

---

## Promoted decisions (now in scope because of B1 multi-coach + B2 athlete login)

Choosing multi-coach SaaS pulls these out of "later" and into the **persistence slice** —
you can't write ownership/scoping-correct models without settling them.

### N1 · Coach↔athlete relationship & cardinality ⏳
The load-bearing new model. Decide: one **active** coach per athlete (simple, recommended for
v1) vs. many concurrent coaches. How an athlete links to a coach (invite — see N4). A `User`
acting as both coach and athlete (decided: allowed). Edit rights: the coach edits the athlete's
plan; what can the athlete edit (log sessions only, or also tweak)?
- **Rec:** `CoachAthlete(coach=User, athlete=User, status)` with one active coach per athlete;
  coach has plan-edit rights, athlete has log + read. _Confirm cardinality before schema._

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

0. **Confirm N1 cardinality** (one active coach per athlete?) — the one product fork still open before schema.
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

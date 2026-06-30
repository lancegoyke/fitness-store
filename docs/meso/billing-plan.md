# Meso — billing (S6)

## Why

B1 decided Meso is a **multi-coach SaaS**, and the owner has confirmed it is
**not** to be planned as a single-operator tool — coaches will pay to use it.
Today there is no billing of any kind: the store's Stripe integration is
one-time only (`payments.views.create_checkout_session` uses `mode="payment"`;
the webhook grants a Django `Permission` per product), `CoachProfile` has no
paid/tier/seat marker, and there is no self-serve "become a coach" flow
(`CoachProfile` is created only via admin or the demo seed).

This plan captures the billing decisions **before** any code, so we don't bake
pricing assumptions into the schema.

## Decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| D1 | **Who pays** | **The coach** (B2B SaaS). Athletes log in as the coach's clients; they never pay the platform. |
| D2 | **Pricing model** | **Per-active-athlete (seats).** A coach pays per active `CoachAthlete` link, beyond a small free allotment. |
| D3 | **Free access** | **Free tier + 14-day no-card trial.** A forever-free tier (a few free seats), plus a frictionless trial that needs no card; the free tier is also the lapse/cancel landing spot. |
| D4 | **What the paywall gates** | **Active-athlete count + the AI agent.** The Claude-powered program agent (B6) has real per-call API cost, so it's paid-only — trial/paid/comped get the full agent. Groups (S1) and notifications stay free at every tier. (v1 gave the free tier **no** agent; **Phase 5 meters it** — a free coach gets a small monthly `FREE_AGENT_ALLOWANCE` of runs, then 402s. The seat gate still shares the `is_active` predicate.) |
| D5 | **Cadence + currency** | **Monthly, USD** for v1 (annual deferred — annualizing a fluctuating seat count adds proration complexity for little v1 value). |
| D6 | **Lapse / cancel** | Stripe Smart Retries → on final failure / cancel, **downgrade to free at period end**. Over the free limit ⇒ block *new* athletes + block edits/deliver until back within the limit or re-subscribed; **never delete data** (matches how an ended relationship archives, not deletes). (Phase 5 makes the edit freeze **per athlete** — the oldest `FREE_SEAT_LIMIT` links stay editable, the rest soft-suspend; v1 froze the whole coach.) |
| D11 | **First slice** | **The subscription spine** for existing logged-in coaches. The public self-serve coach-signup funnel is a later phase. |

### Architecture decisions (recommended; proceed unless overridden)

- **D7 — Stripe Billing + Checkout (`mode="subscription"`) + the hosted Customer
  Portal.** The Portal owns card updates / cancel / invoices / dunning UX — we
  don't hand-roll any of it. A coach subscribes via a subscription Checkout
  Session; they manage the subscription in the Portal.
- **D8 — Stripe is the source of truth; a thin local mirror gates fast.** A new
  **`CoachSubscription`** model (1:1 with the coach `User`) holds just enough to
  gate a request without calling Stripe: `status`, `stripe_subscription_id` /
  `stripe_item_id`, `current_period_end`, `trial_end`, last-synced `quantity`.
- **D9 — A clean, separate billing webhook handler** (`meso/billing/webhooks.py`)
  — `customer.subscription.updated|deleted`, `invoice.paid`,
  `invoice.payment_failed` — idempotent upsert keyed by `stripe_subscription_id`.
  The messy products webhook (debug prints, inline test-user creation) is left
  untouched.
- **D10 — One gating accessor** (`meso/billing/access.py`): `is_active(coach)`,
  `effective_seat_limit(coach)`, `can_add_athlete(coach)`, `can_use_agent(coach)`.
  Two gates, one predicate: `can_use_agent = is_active`, and the seat gate also
  keys off `is_active` (∞ seats when active, else the free cap).
- **D12 — A `comped` status** (unlimited, no Stripe) so the owner and the seeded
  demo coaches are never paywalled.
- **D13 — Pricing structure: base + per-seat (TrainHeroic-style).** Decided
  2026-06-30: **$9.99/mo flat base + $1/mo per active seat**, USD — mirroring
  TrainHeroic's direct-pay Coach Plan ($9.99 + $1/athlete). Implemented as a
  **two-line-item** subscription (a flat base Price, quantity 1, alongside the
  per-seat Price, quantity = active seats). This **supersedes** the single-Price
  shape sketched for Phase 2; the conversion is **Phase 6** below.

## Shape

### The seat = an active relationship

A **billable seat** is an **active `CoachAthlete` link** for the coach
(`CoachAthlete.objects.for_coach(coach).active().count()`). Pending invites /
requests don't count — only an accepted, active relationship. A link going
active (`accept`) increments the count; `end` decrements it.

### `CoachSubscription` (meso, 1:1 → coach `User`)

```
status            free | trialing | active | past_due | canceled | comped
stripe_subscription_id   (null until they actually pay)
stripe_item_id           (the subscription line item — for quantity updates)
trial_end                (null; set locally when the no-card trial starts)
current_period_end       (mirrored from Stripe for paid coaches)
quantity                 (last seat count synced to Stripe; cache, not truth)
created / modified
```

Derived: `is_active = status in {trialing, active, comped}`;
`effective_seat_limit = ∞ if is_active else FREE_SEAT_LIMIT`;
`can_add_athlete(coach) = active_seat_count < effective_seat_limit`.

### The trial is local (no Stripe until they pay)

A **no-card** trial means Stripe shouldn't be involved until a card is actually
collected. So the 14-day trial is **local state** — `status=trialing` +
`trial_end = now + TRIAL_DAYS` — granted when a free-tier coach first wants to
exceed the free seat limit. During the trial the seat limit is ∞. At trial end a
qcluster sweep flips an un-subscribed coach back to `free` (→ the D6 downgrade).
**Stripe objects exist only for coaches who have actually subscribed** (entered a
card), which keeps free/trial coaches entirely off Stripe.

### Subscribing + managing (Stripe)

- One Stripe **Product** ("Meso Coaching") with one recurring **Price**
  (per-seat, monthly, USD; `usage_type=licensed`, quantity = seats). *(Phase 6
  splits this into two line items — a flat base Price + the per-seat Price — per
  D13.)*
- **Subscribe:** `stripe.checkout.Session.create(mode="subscription",
  line_items=[{price: SEAT_PRICE_ID, quantity: active_count}], customer=…)` →
  on success the webhook materializes the `CoachSubscription` (status `active`,
  stores `stripe_subscription_id` + `stripe_item_id` + `current_period_end`).
- **Manage:** `stripe.billing_portal.Session.create(customer=…)` — the coach
  updates card / cancels / sees invoices in Stripe's hosted Portal.

### Seat quantity sync

When a **paid** coach's active count changes (accept/end), best-effort
`stripe.Subscription.modify(items=[{id: item, quantity: n}],
proration_behavior="create_prorations")`. The relationship change is **never
blocked on the Stripe call** — it's mirrored locally and a **daily
`reconcile_seats` qcluster task** recomputes each paid coach's active count and
corrects any Stripe drift (covers a missed/failed inline sync). This reuses the
existing django-q2 cluster — the same one the invite sweeps + agent job run on.

### Enforcement (two gates: seat count + the agent)

**Seat gate — `can_add_athlete(coach)`** is checked at the two points a new
active relationship is created:
- **Sending/accepting an invite** (`CoachInvite` create + `accept`),
- **Accepting an athlete's request** (`invite_accept` on a `pending_athlete_request`).

A free coach at the limit can't open a new invite and a pending accept is
refused with an upgrade CTA ("Start your free trial / subscribe to add more
athletes"). A `trialing`/`active`/`comped` coach is unlimited (a paid coach just
gets a higher Stripe quantity — they're never *blocked*, they pay for the seat).
Adding an athlete to a **group** creates no new relationship (members are already
active links), so groups need no extra gate.

**Agent gate — `can_use_agent(coach)`** (= `is_active`) is checked at the agent
proposal endpoint (`POST api/plan/<id>/agent/`). A free-tier coach gets a 402/JSON
"upgrade to use the AI agent" instead of a drafting batch; the designer's agent
column shows the upgrade CTA in place of the composer. Trial/paid/comped coaches
are unaffected. (Defended at the endpoint, not just the UI — the API cost is real.)

### Downgrade behavior (D6)

On trial-end-without-subscription or a final failed payment / cancel at
`current_period_end`: `status=free`. If the coach is over `FREE_SEAT_LIMIT`,
they keep **read access** to everything and cannot **add** new athletes; nothing
is deleted; re-subscribing restores full access. The edit/deliver freeze is
**per athlete** (Phase 5): the app keeps the coach's **oldest `FREE_SEAT_LIMIT`
active links** editable and soft-suspends the rest — those plans go read-only
(no edit/deliver/agent-apply) until the coach re-subscribes or ends relationships
to get back within the cap. Keeping the *oldest* avoids the app arbitrarily
choosing which athletes to freeze. (A **group** plan serves many athletes through
no single relationship, so it falls back to the coarse coach-wide freeze.)
`billing/access.py` owns this: `suspended_athlete_ids(coach)` (the frozen-link
set) and `can_edit_plan(plan)` (the per-plan gate at the mutating endpoints);
v1 froze the whole coach via the coarse `can_edit(coach)`.

## Phasing

> **Status:** Phase 1 ✅ (PR #319, migration `0020`). Phase 2 ✅ (PR #320,
> migration `0021`) — Stripe Checkout + Customer Portal + the clean webhook +
> best-effort seat sync + the daily `reconcile_seats` sweep. Phase 3 ✅ (this
> slice) — the gates have teeth: `can_add_athlete` at the invite/request choke
> points, `can_use_agent` at the agent endpoint (402), `can_edit` (the D6
> over-limit freeze) at the edit/deliver endpoints, the local trial-start
> endpoint, and the paywall UI (roster billing card + designer agent CTA).
> Phase 4 ✅ (this slice, no migration) — the self-serve signup funnel: a public
> `become_coach` landing page (plan tiers + adaptive CTAs) and a `start_coaching`
> action that creates the `CoachProfile` (idempotent; `plan=trial` also starts
> the no-card trial), plus an entry-point link from the athlete home. A
> `CoachProfile` is no longer admin/seed-only. Phase 5 ✅ (this slice, no
> migration) — the **free-tier agent allowance**, the metered refinement of the
> binary agent gate: a free coach gets `FREE_AGENT_ALLOWANCE` (5) agent runs per
> calendar month (counted from the `AgentProposalBatch` ledger — no new model),
> then the endpoint 402s; the designer shows a "N of M runs left" meter / upgrade
> CTA and the roster card reflects it. Phase 5 also adds **per-athlete suspension
> granularity** (this slice, no migration) — the D6 downgrade edit freeze is now
> per-athlete (`can_edit_plan` / `suspended_athlete_ids`): an over-limit coach
> keeps editing/delivering their oldest `FREE_SEAT_LIMIT` athletes and is frozen
> only on the rest (group plans keep the coarse coach-wide freeze), with a
> per-athlete "Suspended" badge on the roster. **Next: Phase 6** — base +
> per-seat pricing (decided $9.99 base + $1/seat, 2026-06-30), with annual prices
> as a ride-along sub-step.

### Deploying Phase 2 (Stripe configuration)

The code ships dormant — billing does nothing until these are configured, so a
deploy succeeds without them (like the VAPID push keys):

1. In Stripe, create one **Product** ("Meso Coaching") with one recurring
   per-seat **Price** (monthly, USD, `usage_type=licensed`). Set
   `MESO_SEAT_PRICE_ID` to that Price id.
2. Register a **billing webhook endpoint** → `https://<host>/meso/billing/webhook/`
   subscribed to `customer.subscription.created|updated|deleted`, `invoice.paid`,
   `invoice.payment_failed`. Set `MESO_STRIPE_WEBHOOK_SECRET` to that endpoint's
   signing secret (a *separate* secret from the products webhook's
   `STRIPE_ENDPOINT_SECRET`).
3. The `qcluster` already runs the daily sweeps; the `meso-reconcile-seats`
   schedule registers itself via migration `0021`.

1. **Phase 1 — the spine (DONE).** `CoachSubscription` model + migration;
   constants (`FREE_SEAT_LIMIT`, `TRIAL_DAYS`, price-id setting); the
   `billing/access.py` accessor (`is_active` / `effective_seat_limit` /
   `can_add_athlete`); local trial start; the `comped` status + seed/admin (so
   the demo coach is comped and never paywalled). Pure backend state + accessor,
   fully tested. **No Stripe and no enforcement wired yet** — same build order as
   the invite slice (state machine first, UI/enforcement next).
2. **Phase 2 — Stripe.** Subscription Checkout + Customer Portal + the clean
   webhook handler + seat-quantity sync + the `reconcile_seats` qcluster sweep.
   Now a coach can actually pay.
3. **Phase 3 — enforcement + UI (DONE).** `can_add_athlete` wired into the
   invite/request choke points (open invite, accept request, claim invite),
   `can_use_agent` into the agent endpoint (402 + designer upgrade CTA),
   `can_edit` (the D6 over-limit freeze) into the autosave/deliver/group edit
   endpoints, the local trial-start endpoint (`billing/trial/`), and the roster
   billing card (tier + seat usage + start-trial / subscribe / manage-billing
   CTAs). Billing now has teeth. Fully tested in `test_billing_enforcement.py`.
4. **Phase 4 — self-serve coach signup (DONE).** The public become-a-coach
   funnel: a `become_coach` landing page (GET `/meso/coach/`, public — pitches
   the free / trial / paid tiers; an existing coach is bounced to the roster, an
   anonymous visitor gets allauth signup/login CTAs with `?next=` back, a
   logged-in non-coach gets the start form) and `start_coaching` (POST
   `/meso/coach/start/`, login-required) which `get_or_create`s the
   `CoachProfile` (idempotent) and — with `plan=trial` — starts the no-card local
   trial in the same step, then lands on the roster where the Phase 3 billing
   card owns plan choice (free / trial / subscribe). An entry-point link sits on
   the athlete home. No new model/migration. Tested in `test_coach_signup.py`.
5. **Phase 5 — partly DONE.** The **free-tier agent allowance** is done (this
   slice, no migration): `CoachSubscription.FREE_AGENT_ALLOWANCE` (5) +
   `billing/access.py` (`agent_runs_this_month` / `free_agent_runs_remaining` +
   the metered `can_use_agent`, counting `AgentProposalBatch` rows in the current
   calendar month — the batch table is the ledger, no new model) + the
   `presenters.agent_allowance` meter wired into the designer (composer + "N of M
   runs left" / upgrade CTA) and the roster card + the allowance-aware 402 copy at
   `agent_propose`. Tested in `test_billing.py::TestAgentAllowance` and
   `test_billing_enforcement.py`. **Per-athlete suspension granularity** is also
   done (this slice, no migration): `billing/access.py` gains
   `suspended_athlete_ids(coach)` (the active links beyond the oldest
   `FREE_SEAT_LIMIT`, frozen on a downgrade) + `can_edit_plan(plan)` (per-plan
   gate; a group plan falls back to the coarse `can_edit(coach)`), wired into
   `_editable_plan_or_response` + `batch_apply` so an over-limit coach keeps
   editing/delivering their kept athletes; `presenters` surface a per-athlete
   "Suspended" roster badge + a `suspended_count` on the billing card. Tested in
   `test_billing_suspension.py`.
6. **Phase 6 — base + per-seat pricing (TrainHeroic-style) (NEXT).** Convert the
   single per-seat subscription into a **two-line-item** one (D13): a flat **base**
   Price (`MESO_BASE_PRICE_ID`, quantity 1, $9.99/mo) alongside the existing
   **per-seat** Price (`MESO_SEAT_PRICE_ID`, quantity = active seats, $1/mo).
   - **`stripe_gateway.subscribe`** — Checkout with **both** line items
     (`[{price: BASE, quantity: 1}, {price: SEAT, quantity: active_count}]`).
   - **Model** — add a nullable **`stripe_base_item_id`** (a small migration) so
     seat-sync can target only the *seat* line item; `stripe_item_id` stays the
     seat item. The webhook upsert records **both** item ids.
   - **`sync_seat_quantity` + `reconcile_seats`** — adjust **only** the seat
     item's quantity; the base line is fixed at 1 and never resized.
   - **Paywall UI** — copy reads "$9.99/mo + $1/athlete" (roster billing card +
     designer CTA + `become_coach` tiers).
   - Free/trial/comped gates are **unchanged**; ships **dormant** until the owner
     creates **both** Prices and registers the webhook (see "Deploying Phase 2").
   - **Annual prices** ride along once the annual numbers are decided: a second
     `*_ANNUAL` Price per line (`MESO_BASE_PRICE_ID_ANNUAL` /
     `MESO_SEAT_PRICE_ID_ANNUAL`) + a monthly/annual toggle at Checkout.
   - Build red→green (mock the `stripe` SDK as the existing billing tests do — the
     conversion needs **no live Stripe access**).

## Open values (numbers, not architecture — confirm before/with Phase 1)

- **Free seat limit** — rec **1** active athlete.
- **Trial length** — rec **14 days** (matches the invite TTL cadence).
- **Base fee** — **$9.99 / month**, USD (a flat per-coach charge; new
  `MESO_BASE_PRICE_ID`). Decided 2026-06-30 (D13).
- **Per-seat price** — **$1 / active athlete / month**, USD (`MESO_SEAT_PRICE_ID`).
  Decided 2026-06-30 (D13) — mirrors TrainHeroic's direct-pay Coach Plan.
- **Monthly first**; annual prices are a Phase-6 ride-along once numbers are set.
- **Free agent allowance** — set to **5** runs / calendar month (Phase 5; tunable
  via `CoachSubscription.FREE_AGENT_ALLOWANCE`).

## Test / dev story

- Stripe **test mode**; tests mock the `stripe` SDK (follow the existing
  `payments/tests` patterns) and construct/mocked signed webhook events.
- The local trial + gating accessor are pure Django — testable with **no Stripe
  at all** (Phase 1 has zero Stripe surface).
- The `stripe` CLI forwards webhooks locally for Phase 2.

## Deferred

- Annual billing; promo codes / coupons (Stripe supports both when wanted).
- Tax / VAT handling (Stripe Tax) if selling internationally.
- Email receipts beyond Stripe's own invoice emails.

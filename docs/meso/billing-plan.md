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
| D4 | **What the paywall gates** | **Active-athlete count + the AI agent.** The Claude-powered program agent (B6) has real per-call API cost, so it's paid-only — free tier gets **no** agent; trial/paid/comped get the full agent. Groups (S1) and notifications stay free at every tier. Both gates share one predicate (`is_active`), so the free tier is exactly "capped seats, no agent." |
| D5 | **Cadence + currency** | **Monthly, USD** for v1 (annual deferred — annualizing a fluctuating seat count adds proration complexity for little v1 value). |
| D6 | **Lapse / cancel** | Stripe Smart Retries → on final failure / cancel, **downgrade to free at period end**. Over the free limit ⇒ block *new* athletes + block edits/deliver until back within the limit or re-subscribed; **never delete data** (matches how an ended relationship archives, not deletes). |
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
  (per-seat, monthly, USD; `usage_type=licensed`, quantity = seats).
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
they keep **read access** to everything but cannot deliver / edit / add until
they end relationships to get back within the free limit or re-subscribe.
Nothing is deleted; re-subscribing restores full access. (A finer per-athlete
suspension — auto-keeping the N oldest active links live and soft-suspending the
rest — is a Phase-5 refinement; v1 keeps the coarse "coach is over the limit"
rule to avoid the app arbitrarily choosing which athletes to freeze.)

## Phasing

> **Status:** Phase 1 ✅ (PR #319, migration `0020`). Phase 2 ✅ (this slice) —
> Stripe Checkout + Customer Portal + the clean webhook + best-effort seat sync +
> the daily `reconcile_seats` sweep. Enforcement + paywall UI is Phase 3 (next).

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
3. **Phase 3 — enforcement + UI.** Wire `can_add_athlete` into the invite/request
   choke points **and `can_use_agent` into the agent endpoint**; the paywall /
   upgrade CTA + a billing settings page (trial banner, "manage subscription" →
   Portal); the D6 downgrade behavior. Now billing has teeth.
4. **Phase 4 — self-serve coach signup.** The public become-a-coach → choose
   plan → subscribe → `CoachProfile` created funnel (today coach creation is
   admin/seed-only).
5. **Phase 5 — later.** Annual prices; per-athlete suspension granularity on
   downgrade; a small free-tier agent *allowance* / metering (Phase 3 ships the
   binary free=no-agent gate; an allowance is the refinement).

## Open values (numbers, not architecture — confirm before/with Phase 1)

- **Free seat limit** — rec **1** active athlete.
- **Trial length** — rec **14 days** (matches the invite TTL cadence).
- **Per-seat price** — **needs a number** (e.g. $X / active athlete / month, USD).
- **Monthly only** for v1 (annual deferred).

## Test / dev story

- Stripe **test mode**; tests mock the `stripe` SDK (follow the existing
  `payments/tests` patterns) and construct/mocked signed webhook events.
- The local trial + gating accessor are pure Django — testable with **no Stripe
  at all** (Phase 1 has zero Stripe surface).
- The `stripe` CLI forwards webhooks locally for Phase 2.

## Deferred

- Annual billing; promo codes / coupons (Stripe supports both when wanted).
- Per-athlete soft-suspension on downgrade (v1: coarse over-limit rule).
- A small free-tier agent *allowance* / usage metering (v1 ships a binary gate:
  free = no agent, paid = full agent).
- Tax / VAT handling (Stripe Tax) if selling internationally.
- Email receipts beyond Stripe's own invoice emails.

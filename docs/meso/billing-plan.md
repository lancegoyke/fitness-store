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
  shape sketched for Phase 2; the conversion is **Phase 6** below. **⚠ Superseded
  by D14 — per-seat billing was dropped for a flat plan.**
- **D14 — Pricing pivot: a single FLAT monthly Pro price.** Decided 2026-06-30
  (supersedes D13): **$19/mo flat, unlimited athletes**, USD — one recurring Stripe
  Price (`MESO_PRO_PRICE_ID`, quantity 1). **Why:** the only cost that scales with
  usage is the **AI agent** (Claude, ~$0.05–0.10/run) — athletes/groups/storage/
  delivery are ~$0 marginal — so per-seat pricing didn't track cost, it just dragged
  in complexity (seat-quantity sync, the daily `reconcile_seats` sweep, proration,
  two-line-item Checkout). Annual was dropped too: Stripe can't mix monthly + annual
  lines in one subscription (annual base + monthly seats would need two
  subscriptions), so it isn't worth it pre-revenue. **The agent is now metered at
  every tier** (bounds worst-case COGS per coach at cap × ~$0.10): free =
  `FREE_AGENT_ALLOWANCE` (5)/mo, trial/active = `PAID_AGENT_ALLOWANCE` (150)/mo,
  comped = unlimited. The free-tier seat **cap** (and the D6 downgrade suspension)
  stay — only the per-seat **charge** is gone. The seat-sync/annual machinery was
  removed (dead + a footgun that would mis-resize the flat plan). The agent-usage
  tracking (`/meso/usage/`) measures real per-coach cost, so per-seat/annual can be
  reconsidered later from data. Ships **dormant** until the owner creates the one
  Stripe Price + registers the webhook.

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

Derived: `is_active = status in {trialing, active, comped}` (a **Stripe trial**
never lapses on the local clock — see #555 below);
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

### Subscribing during the trial (#555)

**Production, 2026-09-17:** the first real Pro checkout. A coach on the local
trial clicked Subscribe and was charged $19 immediately — Checkout opened with
no trial, so Stripe billed on completion — losing 13+ days still left on their
trial. Lance refunded the charge and kept the coach on Pro.

**Expected behavior:** subscribing during the trial saves the card now and
defers the first charge to when the local trial would have ended. The coach
keeps full access throughout, and the billing page says when the first charge
happens before they click.

**The Checkout rule:** pass `subscription_data.trial_end` = the local
`trial_end` (a unix timestamp) to `stripe.checkout.Session.create`. Stripe
requires this to be **at least 48 hours in the future** (verified in the
Checkout API docs, 2026-09-19) — under that, plus a 5-minute margin for the
request round trip / clock skew, subscribing charges today instead, and the
page says so *before* the click rather than surprising the coach after it.
This never extends a trial — the deferred date is always the existing local
`trial_end`, never pushed out. Because the page is rendered before the POST,
a trial can cross the 48h05m line in between; the Subscribe form then carries
a hidden `first_charge=deferred` marker, and a stale POST (the promise no
longer holds) bounces back to the roster with a message instead of silently
charging today when the coach was told otherwise.

**Which way out, and why.** Once Checkout defers the charge, the subscription
arrives at the webhook with Stripe status `trialing` — same as before, but now
carrying a `stripe_subscription_id`. Two ways to represent that locally:

- **Way B** — map Stripe `trialing` to local `active`. Rejected: an unpaid
  coach would then read as `active` everywhere, including
  `agent_usage_report`'s `monthly_revenue` / `cost_bucket` / `is_paid` and the
  margin alert — counting money that hasn't been charged yet as revenue. It
  also can't distinguish "first charge on X" (the deferred case) from
  "charged today, but the local clock is still counting down" (the under-48h
  case) without a new field and a migration.
- **Way A (chosen)** — a `trialing` row that has a `stripe_subscription_id` is
  a live Stripe subscription, not the local no-card trial. `status` keeps
  mirroring Stripe honestly (still `trialing`, not `active`) and
  `LIVE_STRIPE_STATUSES` stays `(active, past_due)` unchanged, so reporting
  stays correct with no schema change. Everything else follows from two
  predicates on `CoachSubscription`: `is_stripe_trial` (`status == trialing`
  and a `stripe_subscription_id` is set) and `has_live_stripe_subscription`
  (`LIVE_STRIPE_STATUSES` or `is_stripe_trial`) — no migration needed.

That predicate closes four traps a Stripe trial would otherwise fall into:

1. **Lazy expiry locks the coach out.** `is_active` expires a `trialing` row
   once its local `trial_end` passes, so a coach who paid could be locked out
   between the local clock running out and the `updated(active)` webhook
   landing. Fix: `is_trial_expired` adds `and not self.stripe_subscription_id`
   — once Stripe is tracking the trial, only Stripe ends it.
2. **Stale-event takeover.** The webhook's guard against a late/retried event
   for a *different* subscription id used `LIVE_STRIPE_STATUSES`, which never
   included `trialing` — so a stale event could clobber a coach who just
   subscribed mid-trial. Fix: the guard reads `has_live_stripe_subscription`
   instead.
3. **Double Checkout.** The billing page's `on_trial` (Subscribe button, "keep
   full access when it ends") and the view's double-subscribe guard both
   missed a Stripe trial, so a coach could open a second Checkout. Fix:
   `on_trial` now means only the *local* trial (`not sub.stripe_subscription_id`);
   the view's guard checks `has_live_stripe_subscription`.
4. **Trial_end never copied.** The webhook didn't write Stripe's `trial_end`
   onto the row, so the mirror couldn't answer "when does the first charge
   happen." Fix: `_sync_from_subscription` copies `sub_obj["trial_end"]` onto
   the row whenever Stripe sends one; when it doesn't (a plain, non-trial
   subscription), the local value is left alone — it's the single-use trial
   marker `start_trial` sets once, and the product-analytics dashboard (#509)
   reads it for "trial started."

An adversarial review of this slice found two more, both in the webhook's
event-ordering assumptions (Stripe doesn't guarantee delivery order, so a
handler has to treat every event as possibly late or retried):

5. **A canceled subscription id looked reopenable.** A late/retried
   `created`/`updated` for the *same* id as an already-CANCELED row would flip
   it back to TRIALING/ACTIVE — and, per trap 1, a TRIALING row with a
   `stripe_subscription_id` never lazily expires, so that reopen would be
   *permanent*, with no later event able to correct it. Stripe never
   reactivates a canceled subscription (`canceled`/`incomplete_expired` are
   terminal states), so this follows straight from Stripe's own semantics: a
   non-delete event for the same id as a CANCELED row is ignored outright. A
   duplicate `deleted` stays idempotent as before.
6. **A stale `trialing` event could look current.** Stripe moves a
   subscription out of `trialing` at its own `trial_end` (to `active`, or to
   `past_due` on a failed first charge). A `created`/`updated(trialing)`
   delivered late — after the real `updated(active)`/`updated(past_due)` — but
   reporting a `trial_end` already in the past can't be describing the
   subscription's current state; applying it anyway would flip an
   already-charged coach back to "Pro — first charge on `<past date>`" and
   misreport them as unpaid. Fix: a `trialing` event whose own `trial_end` has
   already passed is ignored before the mirror write. A `trialing` event with
   no `trial_end` is unaffected (trap 4's "left alone" case).

An admin who hand-sets a row to `trialing` must also clear
`stripe_subscription_id` — a trialing row with a Stripe id is read as a
Stripe trial and never lapses on the local clock (trap 1); `start_trial`
already clears it for the one in-app path that starts a trial.

**Analytics (#509):** `subscription_started` fires exactly once — on
`created(trialing)` — not again when the subscription flips to `active` at
trial end. The existing `already_live` / ledger check already gives that: the
`created(trialing)` event isn't `already_live` (no matching `stripe_subscription_id`
yet) so it tracks; the later `updated(active)` *is* already live (same
subscription id, a status already in `ACTIVE_STATUSES`) so it doesn't.

**Local timezone display.** Dates are shown in the coach's local timezone (a
small script rewrites the server's UTC date), matching Stripe's own Checkout
page.

**Verified in Stripe test mode (2026-09-19).** Checkout Sessions with
`subscription_data.trial_end` 47h and 47h59m out were rejected ("The
`trial_end` date has to be at least 2 days in the future."); 48h+1min,
48h+5min, and 10 days out were accepted. A trial session has `amount_total`
0 and `payment_method_collection` `"always"`. Stripe's hosted page read "9
days free · Then $19.00 per month starting September 29, 2026 · Total due
today $0.00" with a "Start trial" button for a 10-day `trial_end`. On a test
clock (API `2020-08-27`) a subscription with `trial_end` was created
`trialing` with `current_period_end == trial_end` and a paid $0
`subscription_create` invoice, and at trial end sent
`updated(active, previous=trialing)` *before* the charge, then either
`invoice.paid` ($19) or, for a failing card, `invoice.payment_failed` then
`updated(past_due)`. Those real payloads replayed through the handler gave
the expected rows with exactly one `subscription_started`. Not verified:
completing a hosted Checkout end to end — a test-mode endpoint forwards
`checkout.session.completed` to production's store webhook, so we didn't
complete one.

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
> per-athlete "Suspended" badge on the roster. **Phase 6 ✅** (2026-06-30,
> migration `0024`) — base + per-seat pricing ($9.99 base + $1/seat): two-line-item
> Checkout, the `stripe_base_item_id` field, Price-id item classification in the
> webhook, the both-Prices subscribe guard, and the `PRICE_SUMMARY` paywall copy.
> **⚠ Superseded by Phase 7. Phase 7 ✅** (2026-06-30, D14, migration `0028`) — the
> **flat-plan pivot**: dropped per-seat billing for a single flat **$19/mo Pro
> Price** (`MESO_PRO_PRICE_ID`, quantity 1). Checkout is one flat line item; the
> webhook records the single item; the subscribe guard needs only the one Price. The
> **seat-sync/reconcile machinery was removed** (`billing/seats.py`,
> `sync_seat_quantity`, `meso_reconcile_seats`, its schedule — dropped in `0028`) —
> dead + a footgun that would mis-resize the flat plan. The **AI agent is now metered
> at every tier** (`access.agent_allowance` / `agent_runs_remaining`): free = 5/mo,
> trial/active = `PAID_AGENT_ALLOWANCE` (150)/mo, comped = unlimited; the designer /
> roster / coach-billing surfaces speak the flat price + a tier-aware meter. Revenue
> math is the flat `PRO_PRICE_USD`. The `stripe_base_item_id`/`quantity` columns stay
> (unused, no schema churn). Tested in `test_billing_flat.py` (+ the reworked
> `test_billing_stripe.py`; `test_billing_phase6.py` removed). **No autonomous
> billing backlog remains** — annual is deferred (not worth the two-subscription
> complexity pre-revenue; revisit from usage data).

### Deploying billing (Stripe configuration)

> **Status: ✅ LIVE & VERIFIED 2026-07-01.** Live flat Pro price
> `price_1ToS4CGoL3YAcQLUbTsF2Zba` ($19/mo, `usage_type=licensed`, lookup
> `meso_pro_flat`) + live webhook endpoint `we_1ToSLwGoL3YAcQLUTAF4l9wY`
> (`https://mastering.fitness/meso/billing/webhook/`, the 5 events below) are
> registered; `MESO_PRO_PRICE_ID` + `MESO_STRIPE_WEBHOOK_SECRET` are set on the box
> (live `sk_live_` keys, containers recreated to load them). Verified end-to-end: a
> no-charge throwaway `incomplete` subscription drove `customer.subscription.created`
> and (on cleanup) `customer.subscription.deleted` to the endpoint — **both returned
> HTTP 200** in the box logs (signature verified, handler ran). Issues #321 + #381
> closed. (One earlier `price.created` shows "failed" in the Stripe event log — benign:
> Stripe's reason is "Endpoint not configured for event type"; it never reached the app.)

The code ships dormant — billing does nothing until these are configured, so a
deploy succeeds without them (like the VAPID push keys):

1. In Stripe, create the **Meso Coaching** Product with **one** recurring monthly
   USD Price (D14 flat plan): the flat **Pro** Price ($19/mo, `usage_type=licensed`,
   quantity 1) → `MESO_PRO_PRICE_ID`. The subscribe view stays dormant until it's set.
2. Register a **billing webhook endpoint** → `https://<host>/meso/billing/webhook/`
   subscribed to `customer.subscription.created|updated|deleted`, `invoice.paid`,
   `invoice.payment_failed`. Set `MESO_STRIPE_WEBHOOK_SECRET` to that endpoint's
   signing secret (a *separate* secret from the products webhook's
   `STRIPE_ENDPOINT_SECRET`).
3. No billing sweep is needed. The flat plan has no per-seat quantity to
   reconcile, so the old daily `meso-reconcile-seats` schedule (registered by
   `0021`) was retired by migration `0028`. Billing state is kept current by the
   Stripe webhook (step 2) alone.

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
6. **Phase 6 — base + per-seat pricing (TrainHeroic-style) (DONE, ⚠ SUPERSEDED by
   Phase 7).** Converted the single per-seat subscription into a **two-line-item**
   one (D13): a flat base Price + a per-seat Price. Phase 7 removed the per-seat line
   entirely, so the two-line-item machinery below is gone.
7. **Phase 7 — the flat-plan pivot (D14) (DONE).** Drop per-seat billing for a
   single flat **$19/mo Pro Price** (`MESO_PRO_PRICE_ID`, quantity 1).
   - **`stripe_gateway`** — Checkout with one flat line item
     (`[{price: PRO, quantity: 1}]`); the seat-quantity helpers are gone.
   - **Webhook** — records the single subscription item as `stripe_item_id`; the
     base-vs-seat `_classify_items` split is removed. The `stripe_base_item_id` /
     `quantity` columns stay (unused — no schema churn).
   - **Removed** — `billing/seats.py`, `sync_seat_quantity`, the
     `meso_reconcile_seats` command + task, and its `django_q.Schedule` (dropped in
     migration `0028`). Dead + a footgun that would mis-resize the flat plan.
   - **Agent metering** — `access.agent_allowance` / `agent_runs_remaining` meter
     **every** tier except comped: free 5/mo, trial/active `PAID_AGENT_ALLOWANCE`
     (150)/mo. The endpoint 402 + the designer/roster/coach-billing meters are
     tier-aware (a free coach's exhausted-CTA offers an upgrade; a paid coach's
     notes the monthly reset).
   - **Revenue math** — `monthly_revenue(status)` returns the flat `PRO_PRICE_USD`
     (no seats arg); the `PRICE_SUMMARY` paywall copy reads "$19/mo — unlimited
     athletes".
   - Free/trial/comped seat gates + the D6 downgrade suspension are **unchanged**
     (the free-tier seat cap stays; only the per-seat charge is gone). Ships
     **dormant** until the owner creates the one Price + registers the webhook.
   - Built red→green (mock the `stripe` SDK; `test_billing_flat.py` +
     reworked `test_billing_stripe.py`; `test_billing_phase6.py` removed).

## Open values (numbers, not architecture) — settled at the D14 flat pivot

- **Free seat limit** — **1** active athlete (`CoachSubscription.FREE_SEAT_LIMIT`).
- **Trial length** — **14 days** (matches the invite TTL cadence).
- **Pro price** — **$19 / month flat**, USD, unlimited athletes (`MESO_PRO_PRICE_ID`).
  Decided 2026-06-30 (D14 — supersedes D13's $9.99 base + $1/seat).
- **Free agent allowance** — **5** runs / calendar month
  (`CoachSubscription.FREE_AGENT_ALLOWANCE`).
- **Paid agent allowance** — **150** runs / calendar month
  (`CoachSubscription.PAID_AGENT_ALLOWANCE`; trial + active, comped uncapped) — the
  D14 cap that bounds worst-case COGS per coach.
- **Annual** — deferred (Stripe can't mix monthly + annual lines in one subscription;
  not worth two subscriptions pre-revenue). Revisit from `/meso/usage/` data.

## Test / dev story

- Stripe **test mode**; tests mock the `stripe` SDK (follow the existing
  `payments/tests` patterns) and construct/mocked signed webhook events.
- The local trial + gating accessor are pure Django — testable with **no Stripe
  at all** (Phase 1 has zero Stripe surface).
- The `stripe` CLI forwards webhooks locally for Phase 2.

## Related work

- **Agent usage & cost tracking** — [`agent-usage-plan.md`](./agent-usage-plan.md).
  The $1/seat margin rides on the agent's per-run Claude cost; that plan
  instruments real usage attributed to the coach + athlete (client) so we can
  validate the margin, catch the tail-risk power user, and decide later whether
  to pull either pressure valve (drop the agent's `MESO_AGENT_MODEL` tier, or
  meter paid runs). Independent of the Stripe wiring — can ship before go-live.

## Deferred

- Annual billing; promo codes / coupons (Stripe supports both when wanted).
- Tax / VAT handling (Stripe Tax) if selling internationally.
- Email receipts beyond Stripe's own invoice emails.

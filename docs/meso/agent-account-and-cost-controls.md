# Meso agent — Anthropic account ownership & cost controls

**Status:** 🟡 Runbook — not yet executed · drafted 2026-07-01
**Owner action required** (manual steps in Cloudflare / Anthropic / the prod box).

## Why

The Meso AI agent is the **cost-bearing feature** — every proposal run is a Claude
API call billed to whatever Anthropic account owns the production
`ANTHROPIC_API_KEY`. Now that Meso charges coaches (flat **$19/mo**, D14 — see
[`billing-plan.md`](./billing-plan.md)), that Claude bill is the business's **cost
of goods**, so it should sit on an account the business **owns and pays for**, with
its own hard spend ceiling — not commingled with a personal/work-funded account.

Two separable concerns:

1. **Account ownership & payment** — the prod key currently lives on a personal
   Anthropic account whose subscription is paid on an employer credit card. The
   account is the owner's (kept after leaving the job), but a revenue business's
   COGS shouldn't be on the employer's card. Move billing to the business.
2. **Bankruptcy protection** — a per-coach run cap (below) bounds cost *per coach*,
   not org-wide. A hard **monthly spend limit on the Anthropic account** is the true
   circuit-breaker against a runaway bill (e.g. a free-signup storm).

## What already protects us (in the app)

Per-coach agent metering is live (D14, `billing/access.py`):

| Tier | Cap / month | Enforced |
|------|-------------|----------|
| Free | `FREE_AGENT_ALLOWANCE` (5) | 402 at `agent_propose`, atomic reservation |
| Trial / Active | `PAID_AGENT_ALLOWANCE` (150) | same |
| Comped (owner/demo) | uncapped | — |

Each run's cost is a **real ceiling**, not an average: `max_tokens=8000`, bounded
context (`RECENT_LOG_LIMIT=5`, no full catalog), cached system prompt → ~$0.05–0.10
per run. So a paid coach's worst case is 150 × $0.10 = **$15 vs $19 revenue**
(margin-positive); a free coach is 5 × $0.10 = **$0.50** (acceptable CAC).

**The gap:** exposure is bounded *per coach*, not *globally* — total spend scales
with the number of accounts, so a free-signup storm is the tail risk. The Anthropic
spend limit (Step 4) is the backstop; the app-level controls (Deferred) are the
graceful-degradation layer.

## Current state (verified 2026-07-01)

- **Registrar:** Porkbun (moved from Namecheap). **DNS host:** Cloudflare
  (`nelly`/`hayes.ns.cloudflare.com`). Registrar ≠ DNS host — the registrar move
  left the Cloudflare DNS records untouched.
- **MX:** `eforward1–5.registrar-servers.com` — **stale Namecheap Email
  Forwarding**, now dead (Namecheap only forwards for domains registered with them,
  and the domain left). Orphaned records, safe to delete.
- **No apex SPF/TXT** — so a new email setup adding SPF is additive, not
  destructive. (The app's delivery email sends via SES; it is not authenticated on
  the bare apex, so it's unaffected by apex email-DNS changes.)
- **Prod key:** `ANTHROPIC_API_KEY` in the Hetzner box `.env` (and the local `.env`)
  — read by both `web` and the `qcluster` worker (a separate process rebuilds its
  own Claude client, see [`agent-plan.md`](../archive/meso/agent-plan.md)).

## Step 1 — Dedicated business email (`@mastering.fitness`)

Goal: an inbox the business owns, to be the account owner for Anthropic (and Stripe).
**Receive-only is enough** — account ownership only needs to *receive* verification /
billing / password-reset mail; you never send *from* it.

Since DNS is already on Cloudflare and the old MX are dead, use **Cloudflare Email
Routing** (free, native):

- [ ] Cloudflare dashboard → **mastering.fitness** → **Email → Email Routing**.
- [ ] Run the setup; when it flags the conflicting `eforward*` MX, **let it remove
      them** and add its own MX (`route1/2/3.mx.cloudflare.net`). They're dead —
      nothing to preserve.
- [ ] Add a **destination address** = your personal inbox; click Cloudflare's
      confirmation email.
- [ ] Create a route: **`lance@mastering.fitness` → your inbox** (or a catch-all
      `*@mastering.fitness`).
- [ ] Send a test email to the new address; confirm it lands.

**Heads-up:** Email Routing adds an SPF TXT (`v=spf1 include:_spf.mx.cloudflare.net
~all`). Additive today (no apex SPF exists). Don't delete it. Revisit SPF only if the
app ever sends delivery email *from* the apex `@mastering.fitness`.

**Alternative** (only if you want to *send as* the address, not just receive): add
`mastering.fitness` as a **domain alias** on your existing self-employed Google
Workspace (no new subscription) and point MX at Google. Overkill for account
ownership; skip unless send-as is needed.

## Step 2 — Business-owned Anthropic account

- [ ] Create an Anthropic account / organization under **`lance@mastering.fitness`**.
- [ ] Set its **payment method to the business's** card/account (not the employer's).
- [ ] Generate a **new production API key** there (name it, e.g., `meso-prod`).

Secret handling: the API key is a secret — it goes **only** into the prod env and
your local `.env`, never into chat, commits, or this doc.

## Step 3 — Rotate the production key onto the new account

- [ ] Edit the box env at `/srv/fitness-store/.env` — set `ANTHROPIC_API_KEY` to the
      new key (see [`../deploy-hetzner.md`](../deploy-hetzner.md) for box access).
- [ ] Update the local `.env` too.
- [ ] Restart the stack so `web` **and** `qcluster` pick up the new key (via the
      `deploy` CLI / `docker compose up -d`).
- [ ] **Verify:** run one agent proposal from the designer; confirm it returns a
      batch, and that usage appears on the **new** account's dashboard.
- [ ] **Revoke** the old API key on the old account once the new one is confirmed
      working.

## Step 4 — Hard monthly spend limit (the circuit-breaker)

- [ ] Anthropic Console (new account) → **Limits / Billing** → set a **hard monthly
      spend cap**. This bounds the org-wide bill regardless of app logic — the true
      bankruptcy backstop covering the per-coach cap's global gap.
- [ ] Pick a starting number well above expected COGS but survivable as a worst case
      (e.g. low tens of dollars while pre-revenue; raise as paying coaches grow).
      Worst case if it trips: the agent globally pauses and you're alerted — rare,
      and far better than a surprise invoice.

## Deferred — app-level cost controls (build only if a storm materializes)

The Anthropic spend limit is the hard backstop; these are the graceful-degradation
layer, deferred until there's a reason (owner said "okay for now"):

1. **Free-first selective kill switch.** An org-wide monthly-spend threshold that
   cuts off **free** runs first while paid/trial/comped keep flowing — never punish
   paying coaches for a free-tier problem. The agent-usage tracking
   ([`agent-usage-plan.md`](./agent-usage-plan.md)) already computes the org-wide
   monthly number it would key off. Free users see the same polite "assistant at
   capacity" they'd get hitting their own cap; paid users are unaffected.
2. **Verified-email gate on the free agent.** Account signup email verification is
   currently optional, so disposable emails = free runs. Requiring a **verified
   email before the free agent unlocks** kills the economics of a scripted
   signup-storm at the source, with zero impact on legitimate users. Smaller and
   more targeted than an org meter.

## Related

- [`billing-plan.md`](./billing-plan.md) — the flat $19/mo plan (D14) + the agent
  metering these controls complement.
- [`agent-usage-plan.md`](./agent-usage-plan.md) — per-coach cost tracking
  (`/meso/usage/`); the data source for the free-first kill switch.
- [`../deploy-hetzner.md`](../deploy-hetzner.md) — box access + the `.env` the prod
  key lives in.

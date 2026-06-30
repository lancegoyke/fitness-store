# Meso — agent usage & cost tracking

**Status:** 🟡 Proposed · started 2026-06-30
**Context:** Billing launches at **$9.99/mo base + $1/mo per active seat** (D13 in
[`billing-plan.md`](./billing-plan.md)). The **AI agent is the cost-bearing
feature** — every proposal run is a Claude API call. To confirm the $1/seat
margin holds, catch the tail-risk power user, and decide *later* (on real data,
not guesses) whether to meter paid runs or drop the agent's model tier, we need
**real per-run usage attributed to the coach and the athlete (client).**

Anthropic's invoice is a single org-level total with **no coach/athlete
attribution** — so we must capture usage ourselves at the call site. This plan
is the instrumentation; **the pricing decision is already made: ship at
$1/seat** and let this data tell us whether to adjust.

---

## What we already have (most of the attribution is free)

`AgentProposalBatch` is **already the per-run ledger** — one row per agent run:

- `coach` (FK) — who pays.
- `plan` (FK) — → `relationship` → **athlete (the client/seat)**, or → **group**.
- `model` — the Claude model id the run used (e.g. `claude-opus-4-8`).
- `status` — `drafting`/`pending`/`failed`/`applied`/`dismissed`.
- `instruction`, `summary`, `created_at`.

The free-tier allowance (S6 Phase 5) already **counts batches per calendar
month** as the run ledger. So **coach + client + model are already
attributable** — the gap is purely the **token usage and cost** per run.

> **Reading of "client":** the **athlete** (the coach's client / the billable
> seat). Group plans serve many athletes through no single relationship, so a
> group run attributes to the **group** (athlete null). The **model** is also a
> first-class dimension because it's the cost driver and we may A/B Opus vs
> Sonnet.

---

## Decisions (proposed — proceed unless overridden)

| # | Decision | Choice |
|---|----------|--------|
| U1 | **Where usage lives** | **Extend `AgentProposalBatch`** with usage columns — it *is* the run ledger, one Claude call per run today. Split into a 1:N `AgentApiCall` child **only** when a run becomes multi-call (group-agent fan-out / multi-turn). |
| U2 | **Source of truth** | Capture the Anthropic `response.usage` block + `_request_id` at the call site (`MesoAgentClient.propose`); thread it through `service._persist_result` onto the batch. Anthropic's invoice stays the billing source of truth; our number is an **estimate** for attribution + margin. |
| U3 | **Cost** | Compute `estimated_cost_usd` **at write time** from a per-model rate table in code, and store the Decimal — so historical cost survives a price change. |
| U4 | **Attribution** | coach + athlete (client/seat); group runs → group (athlete null). Also capture `model`, `trigger`, and the coach's `billing_status` **at run time** for slicing. |
| U5 | **Capture on failure too** | Write usage even when `status=failed` — a run that errored mid-stream can still have billed output tokens. Don't only record successful runs. |

---

## The record — fields to add to `AgentProposalBatch`

### Already present (no change)
`coach`, `plan` (→ athlete **or** group), `model`, `status`, `created_at`.

### Usage (new — the gap)
| Field | Type | Notes |
|---|---|---|
| `input_tokens` | int | Uncached input. |
| `output_tokens` | int | |
| `cache_creation_input_tokens` | int | Cache **writes** (~1.25× input). |
| `cache_read_input_tokens` | int | Cache **reads** (~0.1× input). |
| `api_calls` | int (default 1) | >1 once group-agent/multi-turn lands. |
| `request_id` | char | Anthropic `_request_id` — tracing / support escalation. |
| `stop_reason` | char | Diagnostics: `max_tokens` truncation, `refusal`, etc. |
| `duration_ms` | int | Latency / UX + slow-run detection. |

### Cost (new)
| Field | Type | Notes |
|---|---|---|
| `estimated_cost_usd` | Decimal | tokens × per-model rate, computed at write. **Estimate** — reconcile the monthly sum vs the Anthropic invoice. |

### Dimensions for slicing (new)
| Field | Type | Notes |
|---|---|---|
| `trigger` | char choices | `manual` / `draft` / `eval` / (future) `group`. **Excludes eval runs** (`evals.py`) from cost reports. |
| `billing_status` | char | Snapshot of the coach's tier at run time (`free`/`trial`/`active`/`comped`) → **COGS (paid) vs CAC (free/trial) split**. Lossy to reconstruct later, so snapshot it. |
| `served_model` | char (nullable) | What actually ran (= requested today; differs only under future server-side fallbacks). Low priority. |
| `counted_against_free_allowance` | bool | Reconciles the Phase-5 free-tier meter. Optional. |

**Core set** = usage + cost + `trigger` + `billing_status`. The rest
(`served_model`, `counted_against_free_allowance`) are nice-to-have; trim if the
migration feels heavy.

### Outcome (already joinable — for cost-per-value)
`status` + the `ProposedChange` row count give "cost of dismissed/failed runs"
and "cost per applied change" without new columns.

---

## Cost computation — `meso/billing/agent_costs.py`

```
RATES = {                       # USD per 1M tokens (update on Anthropic price changes)
  "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
  "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
  "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
}

def estimate_cost(model, usage) -> Decimal:
    # (input·in + output·out + cache_create·write + cache_read·read) / 1_000_000
```

- Rates live in code (a one-line edit per price change). Cache write = 1.25×
  input (5-min TTL); cache read = 0.1× input — matches the `claude-api` ref.
- Unknown model → cost `None` (don't guess); log it.
- The number is an **internal estimate**; the Anthropic invoice is authoritative.

---

## Reporting (v1 — internal, owner-facing)

- **`meso_agent_usage_report` management command** (month arg): per coach →
  run count, tokens, **estimated cost** vs **revenue** (`$9.99 base + $1 ×
  billable seats`) → **margin**; flag coaches where cost > revenue (the tail the
  $1/seat discussion flagged).
- **Per-(coach, athlete) breakdown** — find the heavy *seats*.
- **Roll-ups** by `model` (validate the Opus-vs-Sonnet call), by `trigger`
  (exclude `eval`), and **free vs paid** (`billing_status` → COGS vs CAC).
- **Admin:** surface the usage columns read-only on the `AgentProposalBatch`
  admin.
- *(Deferred)* an owner dashboard; a coach-facing usage view.

---

## Phasing

1. **Capture (NEXT, autonomous — no Stripe).** Add the columns + migration;
   thread `usage` / `_request_id` / `duration_ms` from `MesoAgentClient.propose`
   → `service._persist_result` → the batch; add `agent_costs.py` + compute
   `estimated_cost_usd` on **every** run (success **and** failure, U5);
   snapshot `trigger` + `billing_status`. Red→green, `stripe` SDK untouched.
2. **Report.** The `meso_agent_usage_report` command + the admin readout.
3. *(Deferred)* dashboard; a margin-threshold alert (e.g. flag when a coach's
   monthly agent cost exceeds a set fraction of their revenue); a reconciliation
   job against the Anthropic Admin/Usage API.

This should land **before or with billing go-live** so the very first paid month
produces real margin data — but it's independent of the Stripe wiring, so it can
ship now regardless of when the owner configures Prices/webhook.

---

## Relationship to the billing pressure valves

The $1/seat unit-economics discussion (see `billing-plan.md` D13 / the
`meso-designer-origin` cost note) identified two levers if margin compresses;
this tracking is what tells us **whether** to pull either:

1. **Model tier** — the agent pins `claude-opus-4-8` via the **`MESO_AGENT_MODEL`
   setting** (a config change, not code). Sonnet 4.6 ≈ 40% cheaper, Haiku 4.5 ≈
   80%. The per-`model` roll-up makes the quality-vs-cost tradeoff measurable.
2. **Meter paid runs** — a generous cap (e.g. 100/mo) bounds the tail. The
   per-(coach, athlete) report shows whether any coach is near it. **Deferred —
   gated on what the data shows.**

---

## Open values

- **Rate table** — current Anthropic pricing (above); update on price changes.
- **Margin-alert threshold** — e.g. flag a coach when monthly agent cost > 50%
  of their revenue. (Phase 3, deferred.)

## Testing

pytest + factory_boy, mirroring the slice discipline:
- **cost helper** is pure → unit-test per model + a cache-heavy mix + the
  unknown-model → `None` path.
- **client** returns usage — mock the SDK response carrying `usage`
  (input/output/cache) + `_request_id`.
- **`_persist_result`** writes usage on **success** and on **failure** (U5).
- **attribution** — a group run attributes to the group (athlete null); a
  `draft` run is tagged `trigger=draft`; an `eval` run is excluded from the
  report; the report's margin math (revenue vs summed cost) is correct.

## Deferred

- Coach-facing usage UI; real-time per-run cost display in the designer.
- Reconciliation against the Anthropic Admin/Usage API (sanity-check our
  estimate vs the invoice).
- 1:N `AgentApiCall` child (when the group agent / multi-turn makes runs
  multi-call — U1).
- A hard paid-run cap (the billing pressure valve) — gated on the data.

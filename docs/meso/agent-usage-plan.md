# Meso — agent usage & cost tracking

**Status:** 🟢 Phase 1 (capture) shipped · 🟢 Phase 2 (report) shipped · 🟢 Phase 3 (margin alert) shipped · 🟢 Phase 4 (owner dashboard) shipped · 🟢 Coach-facing billing & usage page shipped · started 2026-06-30
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
- **Owner dashboard** — `/meso/usage/`, staff-gated (Phase 4).
- **Coach-facing billing & usage page** — `/meso/billing/`, coach-scoped: their
  plan/tier, the bill they owe (base + per active seat), and their agent runs this
  month per athlete/group. **Revenue + run counts only — never the internal cost
  estimate** (that's the owner dashboard's COGS view). Shipped (see below).

---

## Phasing

1. **Capture — ✅ DONE (migration `0025`, no Stripe).** Added the usage/cost/
   dimension columns to `AgentProposalBatch` (the per-run ledger). `client.propose`
   now returns a `ProposalResult(data, usage)` carrying the Anthropic `usage` block
   + `_request_id` + `stop_reason` (`RunUsage`); the service threads it (plus the
   measured `duration_ms`) through `_persist_result` onto the batch and computes
   `estimated_cost_usd` from `billing/agent_costs.py` (per-model rate table; unknown
   model → `None`). `trigger` (`manual`/`draft`/`eval`/`group`) + the coach's
   `billing_status` are snapshotted at batch creation. A **failed** run still records
   model + duration (U5); a non-streaming raise yields no `usage` block, so tokens
   stay 0 and the deferred invoice reconciliation covers any tokens billed on a drop.
   The admin surfaces the columns read-only. Tests: `test_agent_costs.py` (pure
   helper) + `test_agent_usage.py` (client capture, persist on success+failure,
   trigger/billing snapshots). Existing fakes returning a bare dict normalize to
   zero usage, so they stayed green.
2. **Report — ✅ DONE (no migration).** `meso/billing/agent_usage_report.py`
   `build_report(start, end)` aggregates a calendar month's **non-eval** runs into
   per-coach **cost vs revenue → margin** (a `flagged` flag fires only for a
   *paying* coach whose summed cost beats revenue — a free/trial coach's $0-revenue
   cost is CAC by design, never a flag), a per-(coach, client) breakdown sorted by
   cost (client = athlete on an individual plan, or the **group** on a group plan),
   and roll-ups by `model`, `trigger`, and **billing tier** (`cost_bucket` → paid /
   comped / free-trial off each run's snapshot `billing_status`). Revenue =
   `BASE_PRICE_USD + SEAT_PRICE_USD × current billable seats` (the constants mirror
   `presenters.PRICE_SUMMARY`; only `active`/`past_due` coaches bill, all else $0) —
   an approximation, since per-month historical seat counts aren't stored. The
   `meso_agent_usage_report` command renders a text table (`--month YYYY-MM`,
   default current month) or `--json`. Unknown-model runs (cost `None`) are counted
   as `unknown_cost_runs`, never summed as $0. The admin readout shipped in Phase 1.
   Tests: `test_agent_usage_report.py` (helpers, month window, attribution, eval
   exclusion, aggregation, margin/flagging, tier/model/trigger roll-ups, the command).
3. **Margin alert — ✅ DONE (migration `0026`, schedule-only; no Stripe).** The
   early-warning push on top of Phase 2's passive report. `CoachUsage` gained
   `cost_to_revenue_ratio` (`None` when revenue is $0) and `at_risk(threshold)` —
   a *paying* coach whose estimated agent cost crossed `threshold × revenue`
   (strict; `flagged` is the `threshold == 1` case it generalizes). A new
   `agent_usage_report.margin_alerts(report, threshold)` collects the at-risk
   coaches worst-ratio-first, and `previous_month_bounds()` gives the closed-month
   window for the cron. `notifications/emails.send_margin_alert_email` emails the
   owner (`settings.ADMINS`, from the `SERVER_EMAIL` robot) a summary; sent only
   when there are alerts and an admin address. The `meso_agent_margin_alert`
   command (`--month` / `--last-month` / `--threshold` / `--dry-run`) builds the
   month, finds the at-risk coaches, prints them, and sends the email best-effort
   (a mail failure is logged, never fails the run). The default threshold is
   `MESO_MARGIN_ALERT_THRESHOLD` (0.5). `tasks.agent_margin_alert` wraps it with
   `--last-month`; migration `0026` registers a **monthly** `django_q.Schedule`
   (`schedule_type="M"`) pointing at it — the first non-daily Meso sweep. Free/
   trial coaches never alert ($0 revenue is CAC by design). Tests:
   `test_agent_margin_alert.py` (ratio/at-risk/margin_alerts pure logic, the
   previous-month window, the owner email, the command across windows/thresholds/
   dry-run/validation) + `test_scheduler.py` (the monthly registration + the task
   wrapper over the previous month).
4. **Owner dashboard — ✅ DONE (no migration).** The web read-out of the Phase-2
   report. `UsageDashboardView` (`/meso/usage/`, **staff-gated** via
   `UserPassesTestMixin` on `is_staff` — anon bounces to login, an authenticated
   non-staff coach gets a flat 403, so a coach can't probe org-wide spend) renders
   `build_report` for a `?month=YYYY-MM` window (a malformed month degrades to the
   current month with a flashed warning — never 500s). `presenters.usage_dashboard`
   adapts the `Report` into the template context (a `YYYY-MM` label + prev/next
   month nav, the threshold %, the `margin_alerts` subset, the roll-ups pre-sorted
   by cost). New pure, tested helpers on `agent_usage_report`: `shift_month` (the
   prev/next arithmetic), `resolve_alert_threshold` (a never-raising
   settings/override resolver, `DEFAULT_ALERT_THRESHOLD` 0.5 — the dashboard must
   render even with a misconfigured env value), and `sorted_totals` (cost-sorted
   roll-up pairs). `usage_dashboard.html` + `_usage_rollup.html` render the totals,
   the margin-alert banner, the by-tier/model/trigger roll-ups, and the per-coach
   cost-vs-revenue-margin rows with a per-client breakdown; an `is_staff`-gated
   "Usage" nav link in `_meso_base.html`. Tests: `test_agent_usage_dashboard.py`
   (pure helpers, the presenter, the staff gate, month windowing/invalid-month
   fallback, margin-alert surfacing, group attribution).
5. **Coach-facing billing & usage page — ✅ DONE (no migration).** The coach-scoped
   complement to Phase 4's staff-only owner dashboard. `/meso/billing/`
   (`CoachBillingView`, `LoginRequiredMixin`; a non-coach is routed to their training
   home, mirroring the roster's role split) shows a coach **their** plan/tier, the
   bill they owe (base + per active seat, the seat line floored at 1 to mirror Stripe),
   the upgrade CTAs (start trial / subscribe / manage billing), and their AI-agent
   runs this month broken down per athlete/group. The hard line: a coach sees **what
   they pay** (revenue) and **how much they've used** (run counts), **never** the
   internal per-run cost estimate (`estimated_cost_usd`) — that stays owner-only.
   `agent_usage_report.coach_run_breakdown(coach, start, end)` + a `ClientRun` row
   (run counts only, no cost) reuses `_attribution` (a group plan → the group) and
   counts *all* of the coach's in-window batches, so the breakdown total reconciles
   with `billing/access.agent_runs_this_month` (the free-tier meter). `presenters.coach_billing`
   composes `billing_state` + the projected bill + the breakdown. A "Billing" nav
   link renders on the coach surfaces (athlete pages override the `navlinks` block).
   Tests: `test_coach_billing.py` (the breakdown helper, the bill math, the
   no-COGS-leak invariant, the view gate + scoping). Codex review CLEAN iter 1.
6. *(Deferred)* a reconciliation job against the Anthropic Admin/Usage API
   (sanity-check our estimate vs the invoice) — needs an Admin API key + live org
   access, so it can't ship autonomously.

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
- **Margin-alert threshold** — flag a coach when monthly agent cost > 50% of
  their revenue. **Decided & shipped (Phase 3):** default `0.5` via
  `MESO_MARGIN_ALERT_THRESHOLD`, per-run overridable with `--threshold`.

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

- ~~Coach-facing usage UI~~ — **shipped** (the `/meso/billing/` page, Phase 5
  above). Real-time per-run cost display in the designer is still deferred.
- Reconciliation against the Anthropic Admin/Usage API (sanity-check our
  estimate vs the invoice).
- 1:N `AgentApiCall` child (when the group agent / multi-turn makes runs
  multi-call — U1).
- A hard paid-run cap (the billing pressure valve) — gated on the data.

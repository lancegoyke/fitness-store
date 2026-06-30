"""Estimate the USD cost of one Meso agent run from its token usage (agent-usage v1).

Anthropic bills the org a single monthly total with **no per-coach / per-athlete
attribution**, so we estimate each run's cost at the call site from a per-model
rate table and store the Decimal on the batch (``docs/meso/agent-usage-plan.md``
U3). Computing at write time means a later price change can't rewrite history.

The number is an **internal estimate** for margin + attribution; the Anthropic
invoice stays the billing source of truth. Update ``RATES`` whenever Anthropic
changes pricing — an unknown model yields ``None`` (we don't guess; the report
flags it) so a model the table doesn't know never silently reports as free.
"""

from decimal import Decimal

# USD per 1M tokens, per model. Cache write ≈ 1.25× the input rate (the 5-min
# ephemeral TTL the agent uses), cache read ≈ 0.1× input — matches the
# ``claude-api`` reference. Strings so the Decimals are exact. Update on a price
# change (and add a row when the agent's model — ``settings.MESO_AGENT_MODEL`` —
# changes); a missing key → ``None``.
RATES = {
    "claude-opus-4-8": {
        "input": "5.00",
        "output": "25.00",
        "cache_write": "6.25",
        "cache_read": "0.50",
    },
    "claude-sonnet-4-6": {
        "input": "3.00",
        "output": "15.00",
        "cache_write": "3.75",
        "cache_read": "0.30",
    },
    "claude-haiku-4-5": {
        "input": "1.00",
        "output": "5.00",
        "cache_write": "1.25",
        "cache_read": "0.10",
    },
}

_PER_MILLION = Decimal(1_000_000)
# Six decimal places mirrors the model field — a single run is fractions of a cent.
_QUANTUM = Decimal("0.000001")


def estimate_cost(model, usage):
    """Estimated run cost as a ``Decimal`` (USD), or ``None`` for an unknown model.

    ``usage`` is an ``agent.client.RunUsage`` (or anything exposing the same four
    token attributes). Each token bucket is priced at its own rate — uncached
    input, output, cache **writes** (more than input), and cache **reads** (much
    cheaper) — so a cache-heavy run prices correctly, not at the flat input rate.
    """
    rate = RATES.get(model)
    if rate is None:
        return None
    total = (
        Decimal(usage.input_tokens) * Decimal(rate["input"])
        + Decimal(usage.output_tokens) * Decimal(rate["output"])
        + Decimal(usage.cache_creation_input_tokens) * Decimal(rate["cache_write"])
        + Decimal(usage.cache_read_input_tokens) * Decimal(rate["cache_read"])
    ) / _PER_MILLION
    return total.quantize(_QUANTUM)

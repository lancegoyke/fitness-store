"""Cost estimation for one agent run (agent-usage tracking v1).

``billing.agent_costs.estimate_cost`` is a pure function over a per-model rate
table and a ``RunUsage`` token block. Each token bucket prices at its own rate —
uncached input, output, cache writes, cache reads — so a cache-heavy run costs
correctly, not at the flat input rate. An unknown model yields ``None`` (we never
guess); the number is an internal estimate, the Anthropic invoice is the truth.
"""

from decimal import Decimal

from store_project.meso.agent.client import RunUsage
from store_project.meso.billing import agent_costs


def test_opus_input_and_output_only():
    # 1M input @ $5 + 1M output @ $25 = $30 exactly.
    usage = RunUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert agent_costs.estimate_cost("claude-opus-4-8", usage) == Decimal("30.000000")


def test_cache_heavy_mix_prices_each_bucket_at_its_own_rate():
    # Opus: input 5, output 25, cache_write 6.25, cache_read 0.50 (USD / 1M).
    usage = RunUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=3000,
    )
    # (1000*5 + 500*25 + 200*6.25 + 3000*0.50) / 1e6 = 20250 / 1e6
    assert agent_costs.estimate_cost("claude-opus-4-8", usage) == Decimal("0.020250")


def test_sonnet_and_haiku_use_their_own_rates():
    usage = RunUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Sonnet: 3 + 15 = 18; Haiku: 1 + 5 = 6.
    assert agent_costs.estimate_cost("claude-sonnet-4-6", usage) == Decimal("18.000000")
    assert agent_costs.estimate_cost("claude-haiku-4-5", usage) == Decimal("6.000000")


def test_zero_usage_is_zero_cost_for_a_known_model():
    assert agent_costs.estimate_cost("claude-opus-4-8", RunUsage()) == Decimal("0")


def test_unknown_model_returns_none_rather_than_guessing():
    usage = RunUsage(input_tokens=1000, output_tokens=1000)
    assert agent_costs.estimate_cost("gpt-x", usage) is None
    assert agent_costs.estimate_cost("", usage) is None


def test_cost_is_quantized_to_six_places():
    cost = agent_costs.estimate_cost(
        "claude-opus-4-8", RunUsage(input_tokens=1, output_tokens=1)
    )
    assert cost == Decimal("0.000030")
    assert cost.as_tuple().exponent == -6

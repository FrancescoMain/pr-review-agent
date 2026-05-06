"""Unit tests for ``compute_cost_usd``.

The actual prices live in ``cost_table._PRICES`` and are sourced from
Anthropic. We pin the current values here in test names so a rate
change requires updating both — easier to spot in PR review.
"""

from decimal import Decimal

import pytest

from pr_review_agent.agent.cost_table import compute_cost_usd


def test_haiku_at_published_rates() -> None:
    # Haiku 4.5: $1.00 / $5.00 per 1M tokens.
    cost = compute_cost_usd("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("1.000000")
    cost = compute_cost_usd("claude-haiku-4-5-20251001", input_tokens=0, output_tokens=1_000_000)
    assert cost == Decimal("5.000000")


def test_sonnet_at_published_rates() -> None:
    # Sonnet 4.6: $3.00 / $15.00 per 1M tokens.
    cost = compute_cost_usd("claude-sonnet-4-6", input_tokens=500_000, output_tokens=200_000)
    expected = Decimal("3.00") * Decimal("0.5") + Decimal("15.00") * Decimal("0.2")
    assert cost == expected.quantize(Decimal("0.000001"))


def test_opus_at_published_rates() -> None:
    # Opus 4.7: $15.00 / $75.00 per 1M tokens.
    cost = compute_cost_usd("claude-opus-4-7", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("90.000000")


def test_unknown_model_returns_zero() -> None:
    cost = compute_cost_usd("claude-fake-99-9", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("0.000000")


def test_zero_tokens_returns_zero() -> None:
    cost = compute_cost_usd("claude-sonnet-4-6", input_tokens=0, output_tokens=0)
    assert cost == Decimal("0.000000")


@pytest.mark.parametrize("in_tok,out_tok", [(1, 1), (100, 0), (0, 100), (12345, 6789)])
def test_compute_cost_is_quantized_to_six_decimals(in_tok: int, out_tok: int) -> None:
    cost = compute_cost_usd("claude-sonnet-4-6", input_tokens=in_tok, output_tokens=out_tok)
    # NUMERIC(10, 6) → exactly 6 decimal places.
    assert cost.as_tuple().exponent == -6

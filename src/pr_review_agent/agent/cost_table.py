"""Anthropic per-model pricing for cost estimation.

Prices are per 1M tokens, in USD, sourced from Anthropic's public
pricing as of 2026-05. Anthropic prices change a few times per year;
we update them in code (via PR) rather than from .env so deploys
can't drift onto a stale rate.

If a model is not in the table, ``compute_cost_usd`` returns 0 and
callers get a structured warning — better than guessing or crashing.
"""

from decimal import Decimal

import structlog

_log = structlog.get_logger(__name__)

# (input_per_1m_usd, output_per_1m_usd)
_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5-20251001": (Decimal("1.00"), Decimal("5.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-opus-4-7": (Decimal("15.00"), Decimal("75.00")),
}

_MILLION = Decimal("1000000")


def compute_cost_usd(model_id: str, *, input_tokens: int, output_tokens: int) -> Decimal:
    """Return the cost in USD for a (model, in_tokens, out_tokens) triple.

    Quantized to 6 decimals — matching ``NUMERIC(10, 6)`` in
    ``agent_runs.cost_usd``. Unknown models log a warning and return 0.
    """
    if model_id not in _PRICES:
        _log.warning("cost_table.unknown_model", model_id=model_id)
        return Decimal("0.000000")
    in_rate, out_rate = _PRICES[model_id]
    cost = (Decimal(input_tokens) * in_rate + Decimal(output_tokens) * out_rate) / _MILLION
    return cost.quantize(Decimal("0.000001"))

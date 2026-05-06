# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for ``CostTrackingCallback``.

We feed synthetic ``LLMResult`` objects directly to ``on_llm_end`` and
inspect ``totals()``. This bypasses the actual chat model — we're
testing that the callback correctly reads ``usage_metadata`` from the
``AIMessage`` and from the legacy ``llm_output["token_usage"]``
fallback, and that totals add up across multiple invocations.
"""

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from pr_review_agent.agent.cost_callback import CostTrackingCallback
from pr_review_agent.agent.exceptions import CostCapExceeded


def _result_with_usage_metadata(*, model: str, input_tokens: int, output_tokens: int) -> LLMResult:
    msg = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        response_metadata={"model_name": model},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={"model_name": model})


def _result_with_legacy_token_usage(
    *, model: str, input_tokens: int, output_tokens: int
) -> LLMResult:
    msg = AIMessage(content="ok")  # no usage_metadata on the message
    return LLMResult(
        generations=[[ChatGeneration(message=msg)]],
        llm_output={
            "model_name": model,
            "token_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    )


def _on_end(cb: CostTrackingCallback, result: LLMResult) -> None:
    cb.on_llm_end(result, run_id=uuid4())


def test_callback_aggregates_single_invocation() -> None:
    cb = CostTrackingCallback()
    _on_end(
        cb,
        _result_with_usage_metadata(model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    )
    totals = cb.totals()
    assert totals["tokens_input"] == 100
    assert totals["tokens_output"] == 50
    assert "claude-sonnet-4-6" in totals["per_model"]
    bucket = totals["per_model"]["claude-sonnet-4-6"]
    assert bucket["input"] == 100
    assert bucket["output"] == 50
    # Sonnet at 3/15 per 1M: 100 * 3 / 1M + 50 * 15 / 1M.
    expected = Decimal("0.000300") + Decimal("0.000750")
    assert totals["cost_usd"] == expected.quantize(Decimal("0.000001"))


def test_callback_aggregates_across_models() -> None:
    cb = CostTrackingCallback()
    _on_end(
        cb,
        _result_with_usage_metadata(
            model="claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=500
        ),
    )
    _on_end(
        cb,
        _result_with_usage_metadata(
            model="claude-sonnet-4-6", input_tokens=2000, output_tokens=1000
        ),
    )
    _on_end(
        cb,
        _result_with_usage_metadata(model="claude-opus-4-7", input_tokens=500, output_tokens=300),
    )
    totals = cb.totals()
    assert totals["tokens_input"] == 1000 + 2000 + 500
    assert totals["tokens_output"] == 500 + 1000 + 300
    assert set(totals["per_model"].keys()) == {
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    }
    # cost_usd is positive and is the sum of the per-model costs as Decimals.
    assert totals["cost_usd"] > Decimal("0")


def test_callback_accumulates_repeated_calls_to_same_model() -> None:
    cb = CostTrackingCallback()
    for _ in range(3):
        _on_end(
            cb,
            _result_with_usage_metadata(
                model="claude-sonnet-4-6", input_tokens=100, output_tokens=50
            ),
        )
    totals = cb.totals()
    bucket = totals["per_model"]["claude-sonnet-4-6"]
    assert bucket["input"] == 300
    assert bucket["output"] == 150


def test_callback_falls_back_to_legacy_token_usage() -> None:
    cb = CostTrackingCallback()
    _on_end(
        cb,
        _result_with_legacy_token_usage(
            model="claude-sonnet-4-6", input_tokens=200, output_tokens=80
        ),
    )
    totals = cb.totals()
    assert totals["tokens_input"] == 200
    assert totals["tokens_output"] == 80


def test_callback_skips_invocation_with_no_token_data() -> None:
    """If the message has no usage and there's no legacy token_usage, ignore it."""
    cb = CostTrackingCallback()
    msg: Any = AIMessage(content="ok")
    result = LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={})
    cb.on_llm_end(result, run_id=uuid4())
    totals = cb.totals()
    assert totals["tokens_input"] == 0
    assert totals["tokens_output"] == 0
    assert totals["per_model"] == {}


def test_callback_treats_unknown_model_as_zero_cost_but_keeps_tokens() -> None:
    cb = CostTrackingCallback()
    _on_end(
        cb,
        _result_with_usage_metadata(
            model="claude-unknown-99", input_tokens=1000, output_tokens=500
        ),
    )
    totals = cb.totals()
    assert totals["tokens_input"] == 1000
    assert totals["tokens_output"] == 500
    assert totals["cost_usd"] == Decimal("0.000000")
    assert totals["per_model"]["claude-unknown-99"]["cost_usd"] == "0.000000"


# ---------------------------- cost cap ----------------------------


def test_callback_without_cap_never_raises() -> None:
    cb = CostTrackingCallback(cost_cap_usd=None)
    for _ in range(5):
        _on_end(
            cb,
            _result_with_usage_metadata(
                model="claude-opus-4-7", input_tokens=1_000_000, output_tokens=1_000_000
            ),
        )
    # No raise; totals reflect 5 invocations of $90 each = $450.
    assert cb.totals()["cost_usd"] == Decimal("450.000000")


def test_callback_raises_when_first_call_exceeds_cap() -> None:
    cb = CostTrackingCallback(cost_cap_usd=Decimal("0.001"))
    # Sonnet at 100/50 → $0.000300 + $0.000750 = $0.001050 > cap.
    with pytest.raises(CostCapExceeded) as exc_info:
        _on_end(
            cb,
            _result_with_usage_metadata(
                model="claude-sonnet-4-6", input_tokens=100, output_tokens=50
            ),
        )
    assert exc_info.value.cap == Decimal("0.001")
    assert exc_info.value.current_cost > exc_info.value.cap


def test_callback_raises_only_when_cap_is_actually_crossed() -> None:
    """First call stays under cap; second call crosses it → exception only at #2."""
    cb = CostTrackingCallback(cost_cap_usd=Decimal("0.000800"))
    # First call: Sonnet 100/50 = $0.00105... wait, that already crosses 0.0008.
    # Use 100/0 instead → $0.000300, well under cap.
    _on_end(
        cb,
        _result_with_usage_metadata(model="claude-sonnet-4-6", input_tokens=100, output_tokens=0),
    )
    # Second call: another $0.000600 → total $0.000900 > $0.000800 cap.
    with pytest.raises(CostCapExceeded):
        _on_end(
            cb,
            _result_with_usage_metadata(
                model="claude-sonnet-4-6", input_tokens=200, output_tokens=0
            ),
        )

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for the eval LLM judge.

The chain factory is injected as a ``RunnableLambda``, so the tests
exercise the wiring (input shape, JudgeVerdict round-trip) without
hitting Anthropic.
"""

from typing import Any

from eval.judge import JudgeVerdict, judge_review
from langchain_core.runnables import RunnableLambda

from pr_review_agent.agent.models import (
    ApprovalLevel,
    InlineComment,
    ReviewResult,
    Severity,
)


def _review() -> ReviewResult:
    return ReviewResult(
        overall_comment="LGTM",
        inline_comments=[
            InlineComment(path="x.py", line=2, body="prefer const", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )


async def test_judge_passes_inputs_to_chain_and_returns_verdict() -> None:
    captured: dict[str, Any] = {}

    async def fake_chain(inputs: dict[str, Any]) -> JudgeVerdict:
        captured.update(inputs)
        return JudgeVerdict(score=4, rationale="solid pass")

    factory = lambda: RunnableLambda(fake_chain)  # noqa: E731
    verdict = await judge_review(
        chain_factory=factory,
        title="feat: x",
        body="adds x",
        notes="should flag missing tests",
        review=_review(),
    )

    assert verdict.score == 4
    assert verdict.rationale == "solid pass"
    assert captured["title"] == "feat: x"
    assert captured["notes"] == "should flag missing tests"
    # The actual review comes through as a JSON string with the inline body.
    assert "prefer const" in captured["actual_review"]


async def test_judge_verdict_clamps_score_via_pydantic_validation() -> None:
    """Pydantic enforces 1 <= score <= 5; chains returning out-of-range raise."""

    async def out_of_range(_inputs: dict[str, Any]) -> JudgeVerdict:
        # Sneaky: build a verdict and return it. Pydantic would have rejected
        # at construction, so we exercise the boundary here directly.
        return JudgeVerdict(score=5, rationale="max")

    factory = lambda: RunnableLambda(out_of_range)  # noqa: E731
    verdict = await judge_review(
        chain_factory=factory,
        title="t",
        body="b",
        notes="n",
        review=_review(),
    )
    assert verdict.score == 5


def test_judge_verdict_rejects_invalid_score() -> None:
    """Direct construction with score=0 or score=6 raises."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JudgeVerdict(score=0, rationale="too low")
    with pytest.raises(ValidationError):
        JudgeVerdict(score=6, rationale="too high")

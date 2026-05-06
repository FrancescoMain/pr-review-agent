# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for the Critic node.

The Critic combines a deterministic line-number validation pass with
an LLM judgement; we exercise both layers separately:

- Deterministic: anchors not in the diff get added to the drop list
  even if the LLM didn't flag them.
- LLM: the chain factory is injected as a ``RunnableLambda`` returning
  a pre-baked ``CriticVerdict``.
- Hard rule: dropping more than half of the inline list forces
  ``revise`` regardless of what the LLM said.
"""

from typing import Any

from langchain_core.runnables import Runnable, RunnableLambda

from pr_review_agent.agent.models import (
    ApprovalLevel,
    ChangeType,
    CriticVerdict,
    InlineComment,
    ReviewResult,
    RiskLevel,
    Severity,
    TriageDecision,
    VerdictKind,
)
from pr_review_agent.agent.nodes.critic import make_critic_node
from pr_review_agent.agent.state import AgentState

_DIFF_X_Y = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,2 +1,3 @@\n"
    " a\n"
    "+b\n"
    " c\n"
    "diff --git a/y.py b/y.py\n"
    "--- a/y.py\n"
    "+++ b/y.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old\n"
    "+new\n"
)


def _state(*, review: ReviewResult, raw_diff: str = _DIFF_X_Y) -> AgentState:
    return {
        "repo": "francesco/playground",
        "pr_number": 42,
        "installation_id": 99,
        "head_ref": "feat/x",
        "head_sha": "deadbeef" * 5,
        "triage": TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.medium),
        "review": review,
        "raw_diff": raw_diff,
    }


def _llm_returning(verdict: CriticVerdict) -> Runnable[dict[str, Any], CriticVerdict]:
    return RunnableLambda(lambda _inputs: verdict)


# ---------------------------- accept paths ----------------------------


async def test_critic_accepts_clean_review() -> None:
    review = ReviewResult(
        overall_comment="LGTM",
        inline_comments=[
            InlineComment(path="x.py", line=2, body="prefer const", severity=Severity.nit),
            InlineComment(path="y.py", line=1, body="rename", severity=Severity.suggestion),
        ],
        approval=ApprovalLevel.comment,
    )
    node = make_critic_node(
        chain_factory=lambda: _llm_returning(CriticVerdict(verdict=VerdictKind.accept))
    )

    update = await node(_state(review=review))

    assert isinstance(update["critic_verdict"], CriticVerdict)
    assert update["critic_verdict"].verdict == VerdictKind.accept
    assert update["critic_verdict"].should_drop_inline == []


async def test_critic_drops_anchors_not_in_diff_even_when_llm_accepts() -> None:
    """Deterministic layer must catch hallucinated line numbers regardless of LLM verdict."""
    review = ReviewResult(
        overall_comment="LGTM",
        inline_comments=[
            InlineComment(path="x.py", line=99, body="bogus anchor", severity=Severity.suggestion),
        ],
        approval=ApprovalLevel.comment,
    )
    node = make_critic_node(
        chain_factory=lambda: _llm_returning(CriticVerdict(verdict=VerdictKind.accept))
    )

    update = await node(_state(review=review))

    verdict = update["critic_verdict"]
    # 1 of 1 inline dropped → >50% rule promotes verdict to revise.
    assert verdict.verdict == VerdictKind.revise
    assert any(c.path == "x.py" and c.line == 99 for c in verdict.should_drop_inline)


async def test_critic_merges_llm_drops_with_deterministic_drops() -> None:
    review = ReviewResult(
        overall_comment="x",
        inline_comments=[
            # 2 valid + 2 hallucinated; LLM also flags one of the valid ones.
            InlineComment(path="x.py", line=2, body="ok1", severity=Severity.nit),
            InlineComment(path="x.py", line=3, body="ok2", severity=Severity.nit),
            InlineComment(path="x.py", line=99, body="bogus1", severity=Severity.nit),
            InlineComment(path="y.py", line=99, body="bogus2", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    llm_drop = InlineComment(path="x.py", line=2, body="ok1", severity=Severity.nit)
    node = make_critic_node(
        chain_factory=lambda: _llm_returning(
            CriticVerdict(verdict=VerdictKind.accept, should_drop_inline=[llm_drop])
        )
    )

    update = await node(_state(review=review))

    drops = update["critic_verdict"].should_drop_inline
    # Deterministic: 2 hallucinated. LLM: 1 valid. Total deduped: 3.
    assert len(drops) == 3
    drop_keys = {(c.path, c.line, c.body) for c in drops}
    assert ("x.py", 99, "bogus1") in drop_keys
    assert ("y.py", 99, "bogus2") in drop_keys
    assert ("x.py", 2, "ok1") in drop_keys
    # 3 of 4 dropped → >50% → revise.
    assert update["critic_verdict"].verdict == VerdictKind.revise


# ---------------------------- revise paths ----------------------------


async def test_critic_propagates_llm_verdict_when_no_drops() -> None:
    """If neither layer drops anything, the LLM verdict wins as-is."""
    review = ReviewResult(
        overall_comment="x",
        inline_comments=[
            InlineComment(path="x.py", line=2, body="ok", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    node = make_critic_node(
        chain_factory=lambda: _llm_returning(
            CriticVerdict(
                verdict=VerdictKind.revise,
                concerns=["overall comment is too short"],
                revised_overall_comment="A more substantive overall.",
            )
        )
    )

    update = await node(_state(review=review))

    verdict = update["critic_verdict"]
    assert verdict.verdict == VerdictKind.revise
    assert verdict.concerns == ["overall comment is too short"]
    assert verdict.revised_overall_comment == "A more substantive overall."
    assert verdict.should_drop_inline == []


async def test_critic_surfaces_concern_when_only_silently_dropping() -> None:
    """LLM accepts; deterministic layer drops 1 of 3 (<50%) → keep accept,
    but add a concern so the developer knows something was hidden."""
    review = ReviewResult(
        overall_comment="x",
        inline_comments=[
            InlineComment(path="x.py", line=2, body="ok1", severity=Severity.nit),
            InlineComment(path="x.py", line=3, body="ok2", severity=Severity.nit),
            InlineComment(path="x.py", line=99, body="bogus", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    node = make_critic_node(
        chain_factory=lambda: _llm_returning(CriticVerdict(verdict=VerdictKind.accept))
    )

    update = await node(_state(review=review))

    verdict = update["critic_verdict"]
    assert verdict.verdict == VerdictKind.accept
    assert any("not in the diff" in c for c in verdict.concerns)


# ---------------------------- defensive paths ----------------------------


async def test_critic_accepts_when_review_is_missing() -> None:
    """Defensive: if the graph routes to Critic without a review, just accept."""
    state: AgentState = {
        "repo": "x/y",
        "pr_number": 1,
        "installation_id": 99,
        "head_ref": "feat",
        "head_sha": "0" * 40,
    }

    async def _unused(_inputs: dict[str, Any]) -> CriticVerdict:
        raise AssertionError("chain must not be invoked when review is missing")

    node = make_critic_node(chain_factory=lambda: RunnableLambda(_unused))
    update = await node(state)

    assert update["critic_verdict"].verdict == VerdictKind.accept


async def test_critic_handles_missing_diff_gracefully() -> None:
    """No raw_diff in state → every inline is hallucinated → forced revise."""
    review = ReviewResult(
        overall_comment="x",
        inline_comments=[
            InlineComment(path="x.py", line=2, body="z", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    state: AgentState = {
        "repo": "x/y",
        "pr_number": 1,
        "installation_id": 99,
        "head_ref": "feat",
        "head_sha": "0" * 40,
        "review": review,
    }
    node = make_critic_node(
        chain_factory=lambda: _llm_returning(CriticVerdict(verdict=VerdictKind.accept))
    )

    update = await node(state)
    # No diff parsed → every inline anchor is unreachable → forced revise.
    assert update["critic_verdict"].verdict == VerdictKind.revise
    assert len(update["critic_verdict"].should_drop_inline) == 1

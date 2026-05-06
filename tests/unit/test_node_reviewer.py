# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for the Reviewer node.

We inject a fake ``chain_factory`` that returns a ``RunnableLambda``
producing a pre-baked ``ReviewResult``, so the tests never touch
Anthropic. The GitHub client is replaced by a recording stub. We
cover: structured output flows through, model routing per risk level,
the 20-inline cap, error propagation, and that ``get_pr_diff`` is
called with the correct PR coordinates.
"""

from typing import Any

import pytest
from langchain_core.runnables import Runnable, RunnableLambda

from pr_review_agent.agent.models import (
    ApprovalLevel,
    ChangeType,
    GatheredContext,
    InlineComment,
    LinkedIssue,
    ReviewResult,
    RiskLevel,
    Severity,
    TriageDecision,
)
from pr_review_agent.agent.nodes.reviewer import make_reviewer_node
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.exceptions import GitHubAPIError


class _RecordingClient:
    def __init__(
        self, *, diff: str = "diff --git a/x b/x", raise_exc: Exception | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._diff = diff
        self._raise = raise_exc

    async def get_pr_diff(self, *, installation_id: int, repo: str, pr_number: int) -> str:
        self.calls.append(
            {"installation_id": installation_id, "repo": repo, "pr_number": pr_number}
        )
        if self._raise is not None:
            raise self._raise
        return self._diff


def _state(**overrides: Any) -> AgentState:
    base: AgentState = {
        "repo": "francesco/playground",
        "pr_number": 7,
        "installation_id": 99,
        "head_ref": "feat/x",
        "head_sha": "0" * 40,
        "pr_title": "Add dark mode",
        "pr_body": "Closes #1",
        "triage": TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.medium),
        "gathered_context": GatheredContext(
            summary="adds a toggle",
            relevant_files=["src/theme.ts"],
            linked_issues=[LinkedIssue(number=1, title="dark mode req", state="open")],
            notes="",
        ),
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


def _fixed_chain(result: ReviewResult) -> Runnable[dict[str, Any], ReviewResult]:
    return RunnableLambda(lambda _inputs: result)


def _baseline_review() -> ReviewResult:
    return ReviewResult(
        overall_comment="LGTM with minor nits.",
        inline_comments=[
            InlineComment(
                path="src/theme.ts", line=10, body="prefer const here", severity=Severity.nit
            ),
        ],
        approval=ApprovalLevel.comment,
    )


# ---------------------------- happy path ----------------------------


async def test_reviewer_returns_structured_review() -> None:
    review = _baseline_review()
    client = _RecordingClient(diff="diff --git a/x b/x\n+++ b/x\n@@ -0,0 +1 @@\n+new\n")
    factory_calls: list[RiskLevel | None] = []

    def factory(risk: RiskLevel | None) -> Runnable[dict[str, Any], ReviewResult]:
        factory_calls.append(risk)
        return _fixed_chain(review)

    node = make_reviewer_node(github_client=client, chain_factory=factory)  # type: ignore[arg-type]
    update = await node(_state())

    assert update["review"] == review
    assert update["raw_diff"] == "diff --git a/x b/x\n+++ b/x\n@@ -0,0 +1 @@\n+new\n"
    assert factory_calls == [RiskLevel.medium]
    assert client.calls == [{"installation_id": 99, "repo": "francesco/playground", "pr_number": 7}]


async def test_reviewer_passes_diff_and_context_to_chain() -> None:
    """The chain receives the diff, gathered_context, and triage info in inputs."""
    captured: dict[str, Any] = {}

    async def capture(inputs: dict[str, Any]) -> ReviewResult:
        captured.update(inputs)
        return _baseline_review()

    client = _RecordingClient(diff="diff --git a/main b/main\n+new line")
    node = make_reviewer_node(
        github_client=client,  # type: ignore[arg-type]
        chain_factory=lambda _risk: RunnableLambda(capture),
    )
    await node(_state())

    assert captured["diff"] == "diff --git a/main b/main\n+new line"
    assert "adds a toggle" in captured["gathered"]
    assert "dark mode req" in captured["linked_issues"]
    assert captured["risk_level"] == "medium"
    assert captured["change_type"] == "feature"
    # First-pass review: critic_feedback block is "no critic feedback yet".
    assert "first review attempt" in captured["critic_feedback"]


async def test_reviewer_includes_critic_feedback_on_retry() -> None:
    """When critic_verdict is set, prompt input gets prior concerns + previous draft."""
    from pr_review_agent.agent.models import CriticVerdict, VerdictKind

    captured: dict[str, Any] = {}

    async def capture(inputs: dict[str, Any]) -> ReviewResult:
        captured.update(inputs)
        return _baseline_review()

    state = _state(
        critic_verdict=CriticVerdict(
            verdict=VerdictKind.revise,
            concerns=["drop the comment on cache.py:42 — it's about a line that wasn't changed"],
            revised_overall_comment="Try this overall instead.",
        ),
        review=_baseline_review(),
    )
    client = _RecordingClient()
    node = make_reviewer_node(
        github_client=client,  # type: ignore[arg-type]
        chain_factory=lambda _risk: RunnableLambda(capture),
    )
    update = await node(state)

    feedback = captured["critic_feedback"]
    assert "Address these concerns" in feedback
    assert "cache.py:42" in feedback
    assert "Try this overall instead" in feedback
    # retry_count must be incremented so the graph can break the loop.
    assert update["retry_count"] == 1


async def test_reviewer_does_not_increment_retry_count_on_first_pass() -> None:
    client = _RecordingClient()
    node = make_reviewer_node(
        github_client=client,  # type: ignore[arg-type]
        chain_factory=lambda _risk: _fixed_chain(_baseline_review()),
    )
    update = await node(_state())
    assert "retry_count" not in update


# ---------------------------- model routing ----------------------------


async def test_reviewer_routes_high_risk_to_opus_branch() -> None:
    """High-risk PRs MUST cause the factory to be invoked with RiskLevel.high."""
    received: list[RiskLevel | None] = []

    def factory(risk: RiskLevel | None) -> Runnable[dict[str, Any], ReviewResult]:
        received.append(risk)
        return _fixed_chain(_baseline_review())

    node = make_reviewer_node(
        github_client=_RecordingClient(),  # type: ignore[arg-type]
        chain_factory=factory,
    )
    await node(
        _state(triage=TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.high))
    )

    assert received == [RiskLevel.high]


async def test_reviewer_passes_none_when_triage_missing() -> None:
    received: list[RiskLevel | None] = []

    def factory(risk: RiskLevel | None) -> Runnable[dict[str, Any], ReviewResult]:
        received.append(risk)
        return _fixed_chain(_baseline_review())

    node = make_reviewer_node(
        github_client=_RecordingClient(),  # type: ignore[arg-type]
        chain_factory=factory,
    )
    await node(_state(triage=None))

    assert received == [None]


# ---------------------------- truncation ----------------------------


async def test_reviewer_caps_inline_comments() -> None:
    many = [
        InlineComment(path="x.py", line=i, body=f"comment {i}", severity=Severity.nit)
        for i in range(1, 31)
    ]
    big_review = ReviewResult(
        overall_comment="big",
        inline_comments=many,
        approval=ApprovalLevel.comment,
    )
    node = make_reviewer_node(
        github_client=_RecordingClient(),  # type: ignore[arg-type]
        chain_factory=lambda _risk: _fixed_chain(big_review),
        max_inline_comments=20,
    )
    update = await node(_state())

    review = update["review"]
    assert len(review.inline_comments) == 20
    assert "10 additional inline comment(s) suppressed" in review.overall_comment


async def test_reviewer_does_not_touch_overall_when_under_cap() -> None:
    review = _baseline_review()
    node = make_reviewer_node(
        github_client=_RecordingClient(),  # type: ignore[arg-type]
        chain_factory=lambda _risk: _fixed_chain(review),
    )
    update = await node(_state())
    assert update["review"].overall_comment == "LGTM with minor nits."


# ---------------------------- errors ----------------------------


async def test_reviewer_propagates_github_api_error() -> None:
    client = _RecordingClient(raise_exc=GitHubAPIError("503 boom"))
    node = make_reviewer_node(
        github_client=client,  # type: ignore[arg-type]
        chain_factory=lambda _risk: _fixed_chain(_baseline_review()),
    )
    with pytest.raises(GitHubAPIError):
        await node(_state())


async def test_reviewer_propagates_chain_failure() -> None:
    async def boom(_inputs: dict[str, Any]) -> ReviewResult:
        raise RuntimeError("model rejected schema after 2 retries")

    node = make_reviewer_node(
        github_client=_RecordingClient(),  # type: ignore[arg-type]
        chain_factory=lambda _risk: RunnableLambda(boom),
    )
    with pytest.raises(RuntimeError):
        await node(_state())

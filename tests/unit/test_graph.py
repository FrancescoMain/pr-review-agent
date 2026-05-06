"""End-to-end graph tests with all dependencies mocked.

Routes covered:

1. ``triage.should_skip == False`` + critic accepts: triage → gatherer
   → reviewer → critic → publisher. Final state has triage,
   gathered_context, review, critic_verdict, and final_comment; the
   publisher sees the (possibly drop-filtered) review.
2. ``triage.should_skip == True``: triage → publisher. Gatherer,
   reviewer and critic must NOT be visited.
3. Critic loop: critic returns ``revise`` once → reviewer runs again,
   critic accepts, publisher fires. The reviewer node is called twice.
4. Critic loop with retry cap: critic always says ``revise`` → after
   MAX_REVIEW_RETRIES=1 the route forces publisher anyway.

All nodes are fakes; no Anthropic, no GitHub, no checkout.
"""

from typing import Any

from langchain_core.runnables import RunnableLambda

from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.models import (
    ApprovalLevel,
    ChangeType,
    CriticVerdict,
    GatheredContext,
    InlineComment,
    ReviewResult,
    RiskLevel,
    Severity,
    TriageDecision,
    VerdictKind,
)
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.nodes.triage import make_triage_node
from pr_review_agent.agent.state import AgentState


class _RecordingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        self.posted.append({"repo": repo, "pr": pr_number, "body": body})

    async def post_pr_review(
        self,
        *,
        installation_id: int,
        repo: str,
        pr_number: int,
        commit_id: str,
        body: str,
        event: str,
        comments: list[dict[str, Any]],
    ) -> None:
        self.reviews.append(
            {
                "repo": repo,
                "pr": pr_number,
                "body": body,
                "event": event,
                "comments": comments,
            }
        )


def _fake_gatherer(ctx: GatheredContext) -> Any:
    async def gatherer_node(_state: AgentState) -> dict[str, Any]:
        return {
            "gathered_context": ctx,
            "gatherer_messages": [],
            "tool_calls_used": 2,
        }

    return gatherer_node


def _fake_reviewer(review: ReviewResult) -> Any:
    """Reviewer that returns the same review every time it's called.

    Tracks how many times it ran so tests can verify retry behaviour.
    Increments retry_count on subsequent invocations the way the real
    reviewer node does.
    """
    invocations: list[int] = []

    async def reviewer_node(state: AgentState) -> dict[str, Any]:
        invocations.append(1)
        update: dict[str, Any] = {"review": review}
        if state.get("critic_verdict") is not None:
            update["retry_count"] = int(state.get("retry_count") or 0) + 1
        return update

    reviewer_node.invocations = invocations  # type: ignore[attr-defined]
    return reviewer_node


def _fake_critic(verdict: CriticVerdict) -> Any:
    invocations: list[int] = []

    async def critic_node(state: AgentState) -> dict[str, Any]:
        invocations.append(1)
        return {
            "critic_verdict": verdict,
            "retry_count": int(state.get("retry_count") or 0),
        }

    critic_node.invocations = invocations  # type: ignore[attr-defined]
    return critic_node


async def _abort_node(_state: AgentState) -> dict[str, Any]:
    """A node that must NOT be called — fail loudly if the route lands here."""
    raise AssertionError("graph should not visit this node on the skip route")


def _base_input(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo": "francesco/playground",
        "pr_number": 7,
        "pr_title": "Fix race condition",
        "pr_body": "Adds a mutex around the cache",
        "installation_id": 99,
        "head_ref": "feat/race",
        "head_sha": "0" * 40,
    }
    payload.update(overrides)
    return payload


def _baseline_review() -> ReviewResult:
    return ReviewResult(
        overall_comment="Looks correct.",
        inline_comments=[
            InlineComment(
                path="cache.py", line=12, body="consider RLock", severity=Severity.suggestion
            )
        ],
        approval=ApprovalLevel.comment,
    )


async def test_graph_runs_full_pipeline_when_not_skipped() -> None:
    decision = TriageDecision(change_type=ChangeType.bugfix, risk_level=RiskLevel.high)
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    gathered = GatheredContext(summary="adds a mutex", relevant_files=["cache.py"])
    review = _baseline_review()
    client = _RecordingClient()
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_fake_gatherer(gathered),
        reviewer=_fake_reviewer(review),
        critic=_fake_critic(CriticVerdict(verdict=VerdictKind.accept)),
        publisher=make_publisher_node(client),  # type: ignore[arg-type]
    )

    final: Any = await graph.ainvoke(_base_input())

    assert final["triage"] == decision
    assert final["gathered_context"] == gathered
    assert final["review"] == review
    assert final["critic_verdict"].verdict == VerdictKind.accept
    review_body = client.reviews[0]["body"]
    assert "Looks correct." in review_body
    assert "cache.py:12" in review_body


async def test_graph_skips_gatherer_reviewer_critic_when_triage_says_skip() -> None:
    decision = TriageDecision(
        change_type=ChangeType.docs, risk_level=RiskLevel.low, should_skip=True
    )
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    client = _RecordingClient()
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_abort_node,
        reviewer=_abort_node,
        critic=_abort_node,
        publisher=make_publisher_node(client),  # type: ignore[arg-type]
    )

    final: Any = await graph.ainvoke(_base_input())

    assert final["triage"] == decision
    assert final.get("review") is None
    assert "Skipped review" in client.posted[0]["body"]


async def test_graph_loops_when_critic_revises_then_accepts() -> None:
    """First critic verdict is revise → reviewer reruns → second verdict is accept."""
    decision = TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.medium)
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    gathered = GatheredContext(summary="x", relevant_files=[])
    review = _baseline_review()

    # The fake critic flips its verdict between calls: revise first, accept second.
    verdicts = iter(
        [
            CriticVerdict(verdict=VerdictKind.revise, concerns=["drop the off-topic comment"]),
            CriticVerdict(verdict=VerdictKind.accept),
        ]
    )

    invocations: list[int] = []

    async def flipping_critic(state: AgentState) -> dict[str, Any]:
        invocations.append(1)
        return {
            "critic_verdict": next(verdicts),
            "retry_count": int(state.get("retry_count") or 0),
        }

    reviewer = _fake_reviewer(review)
    client = _RecordingClient()
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_fake_gatherer(gathered),
        reviewer=reviewer,
        critic=flipping_critic,
        publisher=make_publisher_node(client),  # type: ignore[arg-type]
    )

    final: Any = await graph.ainvoke(_base_input())

    # Reviewer ran twice; critic ran twice; second verdict is accept.
    assert len(reviewer.invocations) == 2  # type: ignore[attr-defined]
    assert len(invocations) == 2
    assert final["retry_count"] == 1
    assert final["critic_verdict"].verdict == VerdictKind.accept
    assert client.reviews, "publisher should have fired after critic accepted"


async def test_graph_force_publishes_after_retry_cap() -> None:
    """Critic always says revise → after MAX_REVIEW_RETRIES=1 the route forces publisher."""
    decision = TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.low)
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    gathered = GatheredContext(summary="x", relevant_files=[])
    review = _baseline_review()

    reviewer = _fake_reviewer(review)
    critic = _fake_critic(CriticVerdict(verdict=VerdictKind.revise, concerns=["always grumpy"]))
    client = _RecordingClient()
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_fake_gatherer(gathered),
        reviewer=reviewer,
        critic=critic,
        publisher=make_publisher_node(client),  # type: ignore[arg-type]
    )

    final: Any = await graph.ainvoke(_base_input())

    # Cap is 1 → reviewer runs initial + 1 retry = 2 times.
    assert len(reviewer.invocations) == 2  # type: ignore[attr-defined]
    assert len(critic.invocations) == 2  # type: ignore[attr-defined]
    assert final["retry_count"] == 1
    # Even though the critic never accepted, the publisher fired.
    assert client.reviews, "retry cap must force the publisher to run"

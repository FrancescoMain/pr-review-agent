"""End-to-end graph tests with all dependencies mocked.

Two routes through the graph:

1. ``triage.should_skip == False``: triage → context_gatherer → reviewer
   → publisher. Final state has ``triage``, ``gathered_context``,
   ``review``, and ``final_comment``; the publisher sees the review
   in its formatted comment.
2. ``triage.should_skip == True``: triage → publisher (gatherer +
   reviewer are bypassed). The publisher posts the "skipped" message
   and never sees ``review``.

All nodes are fakes; no Anthropic, no GitHub, no checkout.
"""

from typing import Any

from langchain_core.runnables import RunnableLambda

from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.models import (
    ApprovalLevel,
    ChangeType,
    GatheredContext,
    InlineComment,
    ReviewResult,
    RiskLevel,
    Severity,
    TriageDecision,
)
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.nodes.triage import make_triage_node
from pr_review_agent.agent.state import AgentState


class _RecordingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        self.posted.append({"repo": repo, "pr": pr_number, "body": body})


def _fake_gatherer(ctx: GatheredContext) -> Any:
    async def gatherer_node(_state: AgentState) -> dict[str, Any]:
        return {
            "gathered_context": ctx,
            "gatherer_messages": [],
            "tool_calls_used": 2,
        }

    return gatherer_node


def _fake_reviewer(review: ReviewResult) -> Any:
    async def reviewer_node(_state: AgentState) -> dict[str, Any]:
        return {"review": review}

    return reviewer_node


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


async def test_graph_runs_full_pipeline_when_not_skipped() -> None:
    decision = TriageDecision(change_type=ChangeType.bugfix, risk_level=RiskLevel.high)
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    gathered = GatheredContext(summary="adds a mutex", relevant_files=["cache.py"])
    review = ReviewResult(
        overall_comment="Looks correct.",
        inline_comments=[
            InlineComment(
                path="cache.py", line=12, body="consider RLock", severity=Severity.suggestion
            )
        ],
        approval=ApprovalLevel.comment,
    )
    client = _RecordingClient()
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_fake_gatherer(gathered),
        reviewer=_fake_reviewer(review),
        publisher=make_publisher_node(client),  # type: ignore[arg-type]
    )

    final: Any = await graph.ainvoke(_base_input())

    assert final["triage"] == decision
    assert final["gathered_context"] == gathered
    assert final["review"] == review
    posted_body = client.posted[0]["body"]
    assert "Looks correct." in posted_body
    assert "cache.py:12" in posted_body


async def test_graph_skips_gatherer_and_reviewer_when_triage_says_skip() -> None:
    decision = TriageDecision(
        change_type=ChangeType.docs, risk_level=RiskLevel.low, should_skip=True
    )
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    client = _RecordingClient()
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_abort_node,
        reviewer=_abort_node,
        publisher=make_publisher_node(client),  # type: ignore[arg-type]
    )

    final: Any = await graph.ainvoke(_base_input())

    assert final["triage"] == decision
    assert final.get("review") is None
    assert "Skipped review" in client.posted[0]["body"]

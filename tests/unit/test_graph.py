"""End-to-end graph test with all dependencies mocked.

Exercises the wiring between the three Week-2 nodes (triage →
context_gatherer → publisher): state flows from START to END, the
gatherer's output reaches the publisher, and the final state contains
``triage``, ``gathered_context`` and ``final_comment``. Each node is a
fake — no Anthropic, no GitHub, no checkout.
"""

from typing import Any

from langchain_core.runnables import RunnableLambda

from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.models import (
    ChangeType,
    GatheredContext,
    RiskLevel,
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


async def test_graph_runs_triage_gatherer_publisher() -> None:
    decision = TriageDecision(change_type=ChangeType.bugfix, risk_level=RiskLevel.high)
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    gathered = GatheredContext(summary="adds a mutex", relevant_files=["cache.py"])
    client = _RecordingClient()
    publisher = make_publisher_node(client)  # type: ignore[arg-type]
    graph: Any = build_graph(
        triage=triage,
        context_gatherer=_fake_gatherer(gathered),
        publisher=publisher,
    )

    final: Any = await graph.ainvoke(
        {
            "repo": "francesco/playground",
            "pr_number": 7,
            "pr_title": "Fix race condition",
            "pr_body": "Adds a mutex around the cache",
            "installation_id": 99,
            "head_ref": "feat/race",
            "head_sha": "0" * 40,
        }
    )

    assert final["triage"] == decision
    assert final["gathered_context"] == gathered
    assert final["tool_calls_used"] == 2
    assert "**bugfix**" in final["final_comment"]
    assert client.posted[0]["repo"] == "francesco/playground"
    assert client.posted[0]["pr"] == 7

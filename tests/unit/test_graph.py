"""End-to-end graph test with all dependencies mocked.

Exercises the wiring between triage and publisher: state flows from
START to END, the publisher consumes the triage decision, and the
final state contains both ``triage`` and ``final_comment``.
"""

from typing import Any

from langchain_core.runnables import RunnableLambda

from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.models import ChangeType, RiskLevel, TriageDecision
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.nodes.triage import make_triage_node


class _RecordingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        self.posted.append({"repo": repo, "pr": pr_number, "body": body})


async def test_graph_runs_triage_then_publisher() -> None:
    decision = TriageDecision(change_type=ChangeType.bugfix, risk_level=RiskLevel.high)
    triage = make_triage_node(RunnableLambda(lambda _inputs: decision))
    client = _RecordingClient()
    publisher = make_publisher_node(client)  # type: ignore[arg-type]
    graph: Any = build_graph(triage=triage, publisher=publisher)

    final: Any = await graph.ainvoke(
        {
            "repo": "francesco/playground",
            "pr_number": 7,
            "pr_title": "Fix race condition",
            "pr_body": "Adds a mutex around the cache",
            "installation_id": 99,
        }
    )

    assert final["triage"] == decision
    assert "**bugfix**" in final["final_comment"]
    assert client.posted[0]["repo"] == "francesco/playground"
    assert client.posted[0]["pr"] == 7

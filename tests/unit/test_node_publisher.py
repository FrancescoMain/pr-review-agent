"""Unit tests for the publisher node.

Verifies that the node turns the triage decision into the expected
hello-world comment and dispatches it through the GitHub client. The
client is replaced with a recording fake; we don't go over the wire.
"""

from typing import Any

import pytest

from pr_review_agent.agent.models import ChangeType, RiskLevel, TriageDecision
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.state import AgentState


class _RecordingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        self.posted.append(
            {
                "installation_id": installation_id,
                "repo": repo,
                "pr_number": pr_number,
                "body": body,
            }
        )


@pytest.fixture
def state() -> AgentState:
    return {
        "repo": "francesco/playground",
        "pr_number": 42,
        "installation_id": 99,
        "head_ref": "feat/x",
        "head_sha": "0" * 40,
        "triage": TriageDecision(change_type=ChangeType.docs, risk_level=RiskLevel.low),
    }


async def test_publisher_posts_to_correct_pr(state: AgentState) -> None:
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    await node(state)
    assert len(client.posted) == 1
    posted = client.posted[0]
    assert posted["repo"] == "francesco/playground"
    assert posted["pr_number"] == 42
    assert posted["installation_id"] == 99


async def test_publisher_includes_triage_in_comment(state: AgentState) -> None:
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(state)
    assert "**docs**" in update["final_comment"]
    assert "(low)" in update["final_comment"]


async def test_publisher_falls_back_when_triage_missing() -> None:
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(
        {
            "repo": "x/y",
            "pr_number": 1,
            "installation_id": 2,
            "head_ref": "feat/x",
            "head_sha": "0" * 40,
            "triage": None,
        }
    )
    assert "no triage" in update["final_comment"].lower()

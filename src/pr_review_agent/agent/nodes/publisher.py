"""Publisher node — posts the agent's verdict to the PR.

For Task 5 the verdict is a single hello-world comment that surfaces
the triage classification. In Week 2 this node will instead post the
structured review comments returned by the Reviewer / Critic.

Built via factory so tests can inject a fake ``GitHubClient``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.client import GitHubClient

PublisherNode = Callable[[AgentState], Awaitable[dict[str, Any]]]


def _format_hello_comment(state: AgentState) -> str:
    triage = state.get("triage")
    if triage is None:
        return "Hello from agent — no triage available."
    return (
        f"Hello from agent — triage classified this as "
        f"**{triage.change_type.value}** ({triage.risk_level.value})."
    )


def make_publisher_node(client: GitHubClient) -> PublisherNode:
    async def publisher_node(state: AgentState) -> dict[str, Any]:
        comment = _format_hello_comment(state)
        await client.post_pr_comment(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
            body=comment,
        )
        return {"final_comment": comment}

    return publisher_node

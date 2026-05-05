"""Triage node — classifies a PR into a ``TriageDecision``.

For Task 5 the node only sees the PR title and body; the diff isn't
fetched yet (that's the Context Gatherer in W2). The node is built via
``make_triage_node(chain)`` so tests inject a deterministic chain and
production wires a real ``ChatAnthropic.with_structured_output``.

Returns a partial state update — LangGraph merges it into ``AgentState``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.runnables import Runnable

from pr_review_agent.agent.models import TriageDecision
from pr_review_agent.agent.state import AgentState

TriageChain = Runnable[dict[str, Any], TriageDecision]
TriageNode = Callable[[AgentState], Awaitable[dict[str, Any]]]


def make_triage_node(chain: TriageChain) -> TriageNode:
    async def triage_node(state: AgentState) -> dict[str, Any]:
        decision = await chain.ainvoke(
            {
                "title": state.get("pr_title", ""),
                "body": state.get("pr_body") or "(no body provided)",
            }
        )
        tokens = dict(state.get("tokens_used", {}))
        tokens.setdefault("triage", 0)  # populated by callbacks in W2
        return {"triage": decision, "tokens_used": tokens}

    return triage_node

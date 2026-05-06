# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportArgumentType=false, reportMissingTypeArgument=false
"""LangGraph graph definition.

Topology after W2-Task4:

    triage ─┐
            ├─ should_skip=True ──→ publisher
            └─ otherwise ─────────→ context_gatherer ──→ reviewer ──→ publisher

The post-triage edge is conditional so PRs the triage marks as skip
go straight to a thin "skipped" comment from the publisher, without
spending tokens on the gatherer + reviewer. The Critic node lands in
W3 between reviewer and publisher with a retry edge.

Nodes are passed in pre-built so the graph stays free of provider
imports and tests can supply fakes. The return is typed ``Any`` because
LangGraph's ``CompiledStateGraph`` is generic over checkpointers and
node specs we don't constrain here; consumers ``await graph.ainvoke(...)``.
The first comment line locally degrades the LangGraph generics that
pyright cannot infer in strict mode.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from pr_review_agent.agent.nodes.publisher import PublisherNode
from pr_review_agent.agent.nodes.triage import TriageNode
from pr_review_agent.agent.state import AgentState

GathererNode = Callable[[AgentState], Awaitable[dict[str, Any]]]
ReviewerNode = Callable[[AgentState], Awaitable[dict[str, Any]]]


def _route_after_triage(state: AgentState) -> Literal["context_gatherer", "publisher"]:
    triage = state.get("triage")
    if triage is not None and triage.should_skip:
        return "publisher"
    return "context_gatherer"


def build_graph(
    *,
    triage: TriageNode,
    context_gatherer: GathererNode,
    reviewer: ReviewerNode,
    publisher: PublisherNode,
) -> Any:
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("triage", triage)
    graph.add_node("context_gatherer", context_gatherer)
    graph.add_node("reviewer", reviewer)
    graph.add_node("publisher", publisher)
    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage",
        _route_after_triage,
        {"context_gatherer": "context_gatherer", "publisher": "publisher"},
    )
    graph.add_edge("context_gatherer", "reviewer")
    graph.add_edge("reviewer", "publisher")
    graph.add_edge("publisher", END)
    return graph.compile()

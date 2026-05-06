# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportArgumentType=false, reportMissingTypeArgument=false
"""LangGraph graph definition.

Topology after W2-Task3: triage → context_gatherer → publisher.
The Reviewer + Critic land in W2-Task4 and W3 respectively; for now
the publisher continues to post the W1 hello-world comment, but it
already sees ``gathered_context`` in state so we can verify the
plumbing end-to-end.

Nodes are passed in pre-built so the graph stays free of provider
imports and tests can supply fakes. The return is typed ``Any`` because
LangGraph's ``CompiledStateGraph`` is generic over checkpointers and
node specs we don't constrain here; consumers ``await graph.ainvoke(...)``.
The first comment line locally degrades the LangGraph generics that
pyright cannot infer in strict mode.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from pr_review_agent.agent.nodes.publisher import PublisherNode
from pr_review_agent.agent.nodes.triage import TriageNode
from pr_review_agent.agent.state import AgentState

GathererNode = Callable[[AgentState], Awaitable[dict[str, Any]]]


def build_graph(
    *,
    triage: TriageNode,
    context_gatherer: GathererNode,
    publisher: PublisherNode,
) -> Any:
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("triage", triage)
    graph.add_node("context_gatherer", context_gatherer)
    graph.add_node("publisher", publisher)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "context_gatherer")
    graph.add_edge("context_gatherer", "publisher")
    graph.add_edge("publisher", END)
    return graph.compile()

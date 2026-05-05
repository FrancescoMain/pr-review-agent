# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportArgumentType=false, reportMissingTypeArgument=false
"""LangGraph graph definition.

Task 5: a 2-node hello-world. The full topology
(triage → context_gatherer → reviewer → critic → publisher with retry
edge) lands incrementally in Weeks 2 and 3. See SPEC.md §3.

Nodes are passed in pre-built so the graph stays free of provider
imports and tests can supply fakes. The return is typed ``Any`` because
LangGraph's ``CompiledStateGraph`` is generic over checkpointers and
node specs we don't constrain here; consumers ``await graph.ainvoke(...)``.
The first comment line locally degrades the LangGraph generics that
pyright cannot infer in strict mode.
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from pr_review_agent.agent.nodes.publisher import PublisherNode
from pr_review_agent.agent.nodes.triage import TriageNode
from pr_review_agent.agent.state import AgentState


def build_graph(*, triage: TriageNode, publisher: PublisherNode) -> Any:
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("triage", triage)
    graph.add_node("publisher", publisher)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "publisher")
    graph.add_edge("publisher", END)
    return graph.compile()

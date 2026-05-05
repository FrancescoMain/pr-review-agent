"""Shared agent state.

``AgentState`` is the typed dictionary threaded through every node of
the LangGraph graph. The keys produced by every webhook delivery (``repo``,
``pr_number``, ``installation_id``) are required; everything that nodes
fill in along the way is marked ``NotRequired``. The full schema in
SPEC.md §3 will be filled in incrementally as later tasks add nodes
(Context Gatherer, Reviewer, Critic).
"""

from typing import NotRequired, TypedDict

from pr_review_agent.agent.models import TriageDecision


class AgentState(TypedDict):
    repo: str
    pr_number: int
    installation_id: int
    pr_title: NotRequired[str]
    pr_body: NotRequired[str]
    triage: NotRequired[TriageDecision | None]
    final_comment: NotRequired[str | None]
    tokens_used: NotRequired[dict[str, int]]
    errors: NotRequired[list[str]]

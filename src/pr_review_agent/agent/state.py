"""Shared agent state.

``AgentState`` is the typed dictionary threaded through every node of
the LangGraph graph. The keys produced by every webhook delivery
(``repo``, ``pr_number``, ``installation_id``, plus the head ref/sha)
are required; everything that nodes fill in along the way is marked
``NotRequired``. The full schema in SPEC.md §3 will be filled in
incrementally as later tasks add nodes (Reviewer, Critic).
"""

from typing import NotRequired, TypedDict

from langchain_core.messages import BaseMessage

from pr_review_agent.agent.models import GatheredContext, ReviewResult, TriageDecision


class AgentState(TypedDict):
    repo: str
    pr_number: int
    installation_id: int
    head_ref: str
    head_sha: str
    pr_title: NotRequired[str]
    pr_body: NotRequired[str]
    triage: NotRequired[TriageDecision | None]
    gathered_context: NotRequired[GatheredContext | None]
    gatherer_messages: NotRequired[list[BaseMessage]]
    tool_calls_used: NotRequired[int]
    review: NotRequired[ReviewResult | None]
    raw_diff: NotRequired[str | None]
    final_comment: NotRequired[str | None]
    tokens_used: NotRequired[dict[str, int]]
    errors: NotRequired[list[str]]

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportArgumentType=false
"""Reviewer node — produces the structured ``ReviewResult``.

Pipeline-wise this sits between the Context Gatherer and the
Publisher. It does NOT use tool calling: the Gatherer has already done
the exploring. Instead it does a single round of
``with_structured_output(ReviewResult)`` against either Sonnet (the
default) or Opus (when triage flagged the PR as high-risk).

The Reviewer fetches the unified diff itself rather than relying on
the Gatherer to forward it: the diff can be large and the Gatherer's
job is to keep prompts small. One extra HTTP call is cheap.

We cap inline comments at ``MAX_INLINE_COMMENTS`` (post-LLM) — more
than that is almost always nitpicking and overwhelms the PR author.

Architecture: the heavy "build a prompt + pick a model" work is
factored out into ``make_default_review_chain_factory`` so tests inject
a ``RunnableLambda`` directly and never need an Anthropic API key.
The node itself is a thin glue layer that wires state → chain → state.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from pr_review_agent.agent.models import (
    ApprovalLevel,
    InlineComment,
    ReviewResult,
    RiskLevel,
)
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.client import GitHubClient

DEFAULT_OPUS_MODEL = "claude-opus-4-7"
DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"
MAX_INLINE_COMMENTS = 20

ReviewerNode = Callable[[AgentState], Awaitable[dict[str, Any]]]
ReviewChainFactory = Callable[[RiskLevel | None], Runnable[dict[str, Any], ReviewResult]]
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def make_reviewer_node(
    *,
    github_client: GitHubClient,
    chain_factory: ReviewChainFactory,
    max_inline_comments: int = MAX_INLINE_COMMENTS,
) -> ReviewerNode:
    """Build the Reviewer node.

    ``chain_factory(risk_level)`` returns the runnable to invoke for a
    given triage risk — production wires it via
    ``make_default_review_chain_factory``; tests pass a one-liner that
    returns a deterministic ``ReviewResult``. The model selection lives
    *inside* the factory so the node has no Anthropic-specific code.
    """

    async def reviewer_node(state: AgentState) -> dict[str, Any]:
        triage = state.get("triage")
        risk = triage.risk_level if triage is not None else None
        chain = chain_factory(risk)

        diff = await github_client.get_pr_diff(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
        )

        gathered = state.get("gathered_context")
        gathered_block = (
            f"summary: {gathered.summary}\n"
            f"relevant_files: {', '.join(gathered.relevant_files) or '(none)'}\n"
            f"notes: {gathered.notes or '(none)'}"
            if gathered is not None
            else "(no gathered context — gatherer didn't finish)"
        )
        linked_issues_block = (
            "\n".join(f"- #{i.number} ({i.state}): {i.title}" for i in gathered.linked_issues)
            if gathered is not None and gathered.linked_issues
            else "(none)"
        )

        result: ReviewResult = await chain.ainvoke(
            {
                "pr_number": state["pr_number"],
                "repo": state["repo"],
                "title": state.get("pr_title", ""),
                "change_type": triage.change_type.value if triage is not None else "unknown",
                "risk_level": triage.risk_level.value if triage is not None else "unknown",
                "review_depth": triage.review_depth.value if triage is not None else "standard",
                "gathered": gathered_block,
                "linked_issues": linked_issues_block,
                "diff": diff,
            }
        )

        return {"review": _truncate_inline(result, max_inline_comments)}

    return reviewer_node


def make_default_review_chain_factory(
    *,
    anthropic_api_key: str,
    opus_model: str = DEFAULT_OPUS_MODEL,
    sonnet_model: str = DEFAULT_SONNET_MODEL,
    system_prompt: str | None = None,
) -> ReviewChainFactory:
    """Production chain factory: routes high-risk to Opus, others to Sonnet."""
    if system_prompt is None:
        system_prompt = (_PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")

    prompt: Any = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            (
                "human",
                "PR #{pr_number} on {repo}\nTitle: {title}\n\n"
                "Triage: change_type={change_type}, risk={risk_level}, depth={review_depth}.\n\n"
                "Gathered context:\n{gathered}\n\n"
                "Linked issues:\n{linked_issues}\n\n"
                "Unified diff:\n```diff\n{diff}\n```",
            ),
        ]
    )

    def factory(risk_level: RiskLevel | None) -> Runnable[dict[str, Any], ReviewResult]:
        chosen = opus_model if risk_level == RiskLevel.high else sonnet_model
        model = ChatAnthropic(
            model_name=chosen,
            api_key=anthropic_api_key,  # type: ignore[arg-type]
            timeout=120.0,
            max_retries=2,
            stop=None,
        )
        structured: Any = model.with_structured_output(ReviewResult)
        return prompt | structured

    return factory


def _truncate_inline(result: ReviewResult, cap: int) -> ReviewResult:
    if len(result.inline_comments) <= cap:
        return result
    truncated: list[InlineComment] = list(result.inline_comments[:cap])
    suppressed = len(result.inline_comments) - cap
    note = (
        f"\n\n_(Note: {suppressed} additional inline comment(s) suppressed to keep "
        "this review focused. The reviewer flagged more than the per-PR cap.)_"
    )
    overall = result.overall_comment + note
    return ReviewResult(
        overall_comment=overall,
        inline_comments=truncated,
        approval=ApprovalLevel(result.approval),
    )

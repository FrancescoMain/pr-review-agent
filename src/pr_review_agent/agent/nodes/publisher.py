"""Publisher node — posts the agent's verdict to the PR.

In W2-Task4 the verdict has three shapes depending on what reached
this node:

1. ``review`` is set → format the ``ReviewResult`` as a single issue
   comment with the overall summary plus a bullet list of inline
   findings. This is transitional: W2-Task5 turns inline findings into
   real GitHub PR review comments via ``POST /pulls/{n}/reviews``.
2. ``triage.should_skip`` is True → post a brief "skipped" message and
   stop. The graph routes such PRs straight here from triage.
3. Fallback (W1 hello-world): post the triage classification only. This
   path stays for tests/dev where the Reviewer wasn't wired in.

Built via factory so tests can inject a fake ``GitHubClient``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pr_review_agent.agent.models import ReviewResult, Severity
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.client import GitHubClient

PublisherNode = Callable[[AgentState], Awaitable[dict[str, Any]]]

_SEVERITY_GLYPH: dict[Severity, str] = {
    Severity.blocker: "🛑",
    Severity.issue: "⚠️",
    Severity.suggestion: "💡",
    Severity.nit: "·",
}


def _format_review_comment(review: ReviewResult) -> str:
    parts: list[str] = [f"### Review — `{review.approval.value}`", "", review.overall_comment]
    if review.inline_comments:
        parts.append("")
        parts.append("**Inline findings:**")
        for c in review.inline_comments:
            glyph = _SEVERITY_GLYPH.get(c.severity, "·")
            parts.append(f"- {glyph} `{c.path}:{c.line}` — {c.body}")
    return "\n".join(parts)


def _format_skipped_comment(state: AgentState) -> str:
    triage = state.get("triage")
    classification = (
        f"**{triage.change_type.value}** ({triage.risk_level.value})"
        if triage is not None
        else "unclassified"
    )
    return (
        f"Skipped review — triage classified this PR as {classification} "
        "and marked it as not needing review."
    )


def _format_hello_comment(state: AgentState) -> str:
    triage = state.get("triage")
    if triage is None:
        return "Hello from agent — no triage available."
    return (
        f"Hello from agent — triage classified this as "
        f"**{triage.change_type.value}** ({triage.risk_level.value})."
    )


def _select_comment(state: AgentState) -> str:
    review = state.get("review")
    if review is not None:
        return _format_review_comment(review)
    triage = state.get("triage")
    if triage is not None and triage.should_skip:
        return _format_skipped_comment(state)
    return _format_hello_comment(state)


def make_publisher_node(client: GitHubClient) -> PublisherNode:
    async def publisher_node(state: AgentState) -> dict[str, Any]:
        comment = _select_comment(state)
        await client.post_pr_comment(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
            body=comment,
        )
        return {"final_comment": comment}

    return publisher_node

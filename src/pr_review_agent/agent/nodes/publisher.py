"""Publisher node — turns the agent's verdict into a real GitHub artefact.

After W2-Task5 the Publisher chooses one of three paths based on what
arrived in state:

1. ``review`` is set → publish a **PR review with inline comments**
   via ``POST /pulls/{n}/reviews``. Inline comments are validated
   against the unified diff (the Reviewer persisted ``raw_diff`` in
   state). Comments anchored to lines that the diff actually touched
   on the right side go through as inline; the rest are degraded to
   bullet points in the review body so the developer still sees them.
   If GitHub rejects the review (most commonly 422 on a stale anchor)
   the Publisher falls back to a single issue comment with the same
   formatted body — the review is delivered, just without the inline
   anchors.
2. ``triage.should_skip == True`` → post a brief "skipped" issue comment.
3. Fallback (W1 hello-world) — used when the Reviewer wasn't wired in.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from pr_review_agent.agent.models import (
    ApprovalLevel,
    InlineComment,
    ReviewResult,
    Severity,
)
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.diff_parser import parse_post_lines
from pr_review_agent.github.exceptions import GitHubAPIError

PublisherNode = Callable[[AgentState], Awaitable[dict[str, Any]]]

_log = structlog.get_logger(__name__)

_SEVERITY_GLYPH: dict[Severity, str] = {
    Severity.blocker: "🛑",
    Severity.issue: "⚠️",
    Severity.suggestion: "💡",
    Severity.nit: "·",
}

_APPROVAL_TO_EVENT: dict[ApprovalLevel, str] = {
    ApprovalLevel.approve: "APPROVE",
    ApprovalLevel.comment: "COMMENT",
    ApprovalLevel.request_changes: "REQUEST_CHANGES",
}


def _format_inline_body(comment: InlineComment) -> str:
    glyph = _SEVERITY_GLYPH.get(comment.severity, "·")
    return f"{glyph} **[{comment.severity.value}]** {comment.body}"


def _split_inline_by_anchorability(
    inline: list[InlineComment], allowed: dict[str, set[int]]
) -> tuple[list[InlineComment], list[InlineComment]]:
    """Return ``(anchorable, non_anchorable)`` lists preserving input order."""
    anchorable: list[InlineComment] = []
    non_anchorable: list[InlineComment] = []
    for c in inline:
        if c.line in allowed.get(c.path, set()):
            anchorable.append(c)
        else:
            non_anchorable.append(c)
    return anchorable, non_anchorable


def _format_review_body(review: ReviewResult, non_anchorable: list[InlineComment]) -> str:
    parts: list[str] = [f"### Review — `{review.approval.value}`", "", review.overall_comment]
    if non_anchorable:
        parts.append("")
        parts.append("**Comments not anchored to a changed line:**")
        for c in non_anchorable:
            parts.append(f"- `{c.path}:{c.line}` — {_format_inline_body(c)}")
    return "\n".join(parts)


def _format_issue_comment_body(review: ReviewResult) -> str:
    """Used both for the skip fallback and when Reviews API rejects the review."""
    parts: list[str] = [f"### Review — `{review.approval.value}`", "", review.overall_comment]
    if review.inline_comments:
        parts.append("")
        parts.append("**Inline findings:**")
        for c in review.inline_comments:
            parts.append(f"- `{c.path}:{c.line}` — {_format_inline_body(c)}")
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


def make_publisher_node(client: GitHubClient) -> PublisherNode:
    async def publisher_node(state: AgentState) -> dict[str, Any]:
        review = state.get("review")
        if review is not None:
            return await _publish_review(client, state, review)

        triage = state.get("triage")
        if triage is not None and triage.should_skip:
            comment = _format_skipped_comment(state)
        else:
            comment = _format_hello_comment(state)

        await client.post_pr_comment(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
            body=comment,
        )
        return {"final_comment": comment}

    return publisher_node


def _apply_critic_drops(review: ReviewResult, state: AgentState) -> ReviewResult:
    """Remove inline comments the Critic flagged (silently — trace lives in LangSmith).

    Match by ``(path, line, body)`` — the same identity the Critic uses
    when populating ``should_drop_inline``. Anything else passes through
    unchanged. We don't touch ``approval`` even if the count of
    blockers drops to zero; that's the Reviewer's call, not ours.
    """
    verdict = state.get("critic_verdict")
    if verdict is None or not verdict.should_drop_inline:
        return review
    drop_keys = {(c.path, c.line, c.body) for c in verdict.should_drop_inline}
    kept = [c for c in review.inline_comments if (c.path, c.line, c.body) not in drop_keys]
    if len(kept) == len(review.inline_comments):
        return review
    return review.model_copy(update={"inline_comments": kept})


async def _publish_review(
    client: GitHubClient, state: AgentState, review: ReviewResult
) -> dict[str, Any]:
    review = _apply_critic_drops(review, state)
    raw_diff = state.get("raw_diff") or ""
    allowed = parse_post_lines(raw_diff) if raw_diff else {}
    anchorable, non_anchorable = _split_inline_by_anchorability(review.inline_comments, allowed)

    body = _format_review_body(review, non_anchorable)
    api_comments: list[dict[str, Any]] = [
        {
            "path": c.path,
            "line": c.line,
            "side": "RIGHT",
            "body": _format_inline_body(c),
        }
        for c in anchorable
    ]
    event = _APPROVAL_TO_EVENT[review.approval]

    try:
        await client.post_pr_review(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
            commit_id=state["head_sha"],
            body=body,
            event=event,
            comments=api_comments,
        )
        return {"final_comment": body}
    except GitHubAPIError as exc:
        _log.warning(
            "review_api_failed_falling_back_to_issue_comment",
            repo=state["repo"],
            pr_number=state["pr_number"],
            error=str(exc),
        )
        fallback = _format_issue_comment_body(review)
        await client.post_pr_comment(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
            body=fallback,
        )
        return {"final_comment": fallback}

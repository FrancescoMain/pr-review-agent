"""Unit tests for the Publisher node.

Three publication paths to cover:

1. ``review`` present → ``post_pr_review`` (Reviews API) with inline
   comments split by anchorability (lines that the diff touches on
   the right side go inline, others end up as bullets in the body).
2. ``review`` present but Reviews API rejects (e.g. 422 on stale
   anchor) → fallback to a single issue comment with the same body.
3. ``review`` absent → either "skipped" or W1 hello-world via
   ``post_pr_comment``.

Client is a recording fake; no HTTP, no GitHub.
"""

from typing import Any

import pytest

from pr_review_agent.agent.models import (
    ApprovalLevel,
    ChangeType,
    InlineComment,
    ReviewResult,
    RiskLevel,
    Severity,
    TriageDecision,
)
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.exceptions import GitHubAPIError


class _RecordingClient:
    def __init__(self, *, review_raises: Exception | None = None) -> None:
        self.comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self._review_raises = review_raises

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        self.comments.append(
            {
                "installation_id": installation_id,
                "repo": repo,
                "pr_number": pr_number,
                "body": body,
            }
        )

    async def post_pr_review(
        self,
        *,
        installation_id: int,
        repo: str,
        pr_number: int,
        commit_id: str,
        body: str,
        event: str,
        comments: list[dict[str, Any]],
    ) -> None:
        if self._review_raises is not None:
            raise self._review_raises
        self.reviews.append(
            {
                "installation_id": installation_id,
                "repo": repo,
                "pr_number": pr_number,
                "commit_id": commit_id,
                "body": body,
                "event": event,
                "comments": comments,
            }
        )


_DIFF_X_Y = (
    "diff --git a/src/x.py b/src/x.py\n"
    "--- a/src/x.py\n"
    "+++ b/src/x.py\n"
    "@@ -1,2 +1,3 @@\n"
    " a\n"
    "+b\n"
    " c\n"
    "diff --git a/src/y.py b/src/y.py\n"
    "--- a/src/y.py\n"
    "+++ b/src/y.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old\n"
    "+new\n"
)


@pytest.fixture
def state() -> AgentState:
    return {
        "repo": "francesco/playground",
        "pr_number": 42,
        "installation_id": 99,
        "head_ref": "feat/x",
        "head_sha": "abc123" + "0" * 34,
        "triage": TriageDecision(change_type=ChangeType.docs, risk_level=RiskLevel.low),
    }


# ---------------------------- no-review paths ----------------------------


async def test_publisher_posts_to_correct_pr_when_no_review(state: AgentState) -> None:
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    await node(state)
    assert len(client.comments) == 1
    posted = client.comments[0]
    assert posted["repo"] == "francesco/playground"
    assert posted["pr_number"] == 42
    assert posted["installation_id"] == 99
    assert client.reviews == []


async def test_publisher_includes_triage_in_hello_comment(state: AgentState) -> None:
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(state)
    assert "**docs**" in update["final_comment"]
    assert "(low)" in update["final_comment"]


async def test_publisher_falls_back_when_triage_missing() -> None:
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(
        {
            "repo": "x/y",
            "pr_number": 1,
            "installation_id": 2,
            "head_ref": "feat/x",
            "head_sha": "0" * 40,
            "triage": None,
        }
    )
    assert "no triage" in update["final_comment"].lower()


async def test_publisher_emits_skipped_message_when_triage_says_skip() -> None:
    skip = TriageDecision(change_type=ChangeType.docs, risk_level=RiskLevel.low, should_skip=True)
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    skip_state: AgentState = {
        "repo": "x/y",
        "pr_number": 5,
        "installation_id": 1,
        "head_ref": "feat/x",
        "head_sha": "0" * 40,
        "triage": skip,
    }
    update = await node(skip_state)

    assert "Skipped review" in update["final_comment"]
    assert "**docs**" in update["final_comment"]
    assert client.reviews == []


# ---------------------------- review path: Reviews API ----------------------------


async def test_publisher_publishes_review_with_anchorable_inline(state: AgentState) -> None:
    state["raw_diff"] = _DIFF_X_Y
    state["review"] = ReviewResult(
        overall_comment="Looks good — small nits.",
        inline_comments=[
            # Anchorable: x.py line 2 is added in the diff.
            InlineComment(path="src/x.py", line=2, body="prefer const", severity=Severity.nit),
            # Non-anchorable: y.py line 99 is not in any hunk.
            InlineComment(
                path="src/y.py", line=99, body="off-by-one risk", severity=Severity.issue
            ),
        ],
        approval=ApprovalLevel.comment,
    )
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(state)

    assert len(client.reviews) == 1
    posted = client.reviews[0]
    assert posted["commit_id"] == state["head_sha"]
    assert posted["event"] == "COMMENT"
    # Anchored comment goes inline; non-anchored stays out.
    assert posted["comments"] == [
        {
            "path": "src/x.py",
            "line": 2,
            "side": "RIGHT",
            "body": "· **[nit]** prefer const",
        }
    ]
    # Non-anchored comment surfaces in the body so the developer still sees it.
    assert "Comments not anchored to a changed line" in posted["body"]
    assert "src/y.py:99" in posted["body"]
    # No issue comment was posted alongside.
    assert client.comments == []
    assert update["final_comment"] == posted["body"]


async def test_publisher_routes_approval_to_event(state: AgentState) -> None:
    state["raw_diff"] = _DIFF_X_Y
    state["review"] = ReviewResult(
        overall_comment="ship it",
        inline_comments=[],
        approval=ApprovalLevel.approve,
    )
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    await node(state)
    assert client.reviews[0]["event"] == "APPROVE"


async def test_publisher_routes_request_changes_to_event(state: AgentState) -> None:
    state["raw_diff"] = _DIFF_X_Y
    state["review"] = ReviewResult(
        overall_comment="please fix",
        inline_comments=[],
        approval=ApprovalLevel.request_changes,
    )
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    await node(state)
    assert client.reviews[0]["event"] == "REQUEST_CHANGES"


async def test_publisher_falls_back_to_issue_comment_on_review_api_error(
    state: AgentState,
) -> None:
    state["raw_diff"] = _DIFF_X_Y
    state["review"] = ReviewResult(
        overall_comment="overall",
        inline_comments=[
            InlineComment(path="src/x.py", line=2, body="nit", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    client = _RecordingClient(review_raises=GitHubAPIError("HTTP 422"))
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(state)

    # Review attempted, then issue-comment fallback fired.
    assert len(client.comments) == 1
    fallback_body = client.comments[0]["body"]
    assert "Review — `comment`" in fallback_body
    assert "src/x.py:2" in fallback_body  # all inline now in body
    assert update["final_comment"] == fallback_body


async def test_publisher_drops_all_inline_when_diff_is_missing(state: AgentState) -> None:
    """No raw_diff in state → no path is anchorable; everything degrades to body."""
    state["review"] = ReviewResult(
        overall_comment="review without diff",
        inline_comments=[
            InlineComment(path="src/x.py", line=2, body="nit", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    # raw_diff intentionally not set.
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    await node(state)

    posted = client.reviews[0]
    assert posted["comments"] == []
    assert "src/x.py:2" in posted["body"]


async def test_publisher_review_takes_priority_over_skip_flag(state: AgentState) -> None:
    """If review and should_skip are both set, the Reviews API path wins."""
    state["triage"] = TriageDecision(
        change_type=ChangeType.docs, risk_level=RiskLevel.low, should_skip=True
    )
    state["raw_diff"] = _DIFF_X_Y
    state["review"] = ReviewResult(
        overall_comment="actual review wins",
        inline_comments=[],
        approval=ApprovalLevel.approve,
    )
    client = _RecordingClient()
    node = make_publisher_node(client)  # type: ignore[arg-type]
    update = await node(state)
    assert "actual review wins" in update["final_comment"]
    assert "Skipped review" not in update["final_comment"]
    assert len(client.reviews) == 1
    assert client.comments == []

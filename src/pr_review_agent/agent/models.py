"""Domain models for the agent (separate from GitHub wire models).

These types describe the agent's *internal* contracts: triage decisions,
review comments, etc. Kept distinct from ``pr_review_agent.github.models``
which mirrors the GitHub API.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from pr_review_agent.agent.tools.models import LinkedIssue


class ChangeType(StrEnum):
    feature = "feature"
    bugfix = "bugfix"
    refactor = "refactor"
    docs = "docs"
    chore = "chore"
    test = "test"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class ReviewDepth(StrEnum):
    shallow = "shallow"
    standard = "standard"
    deep = "deep"


class TriageDecision(BaseModel):
    change_type: ChangeType = Field(description="Best-fit category for this PR")
    risk_level: RiskLevel = Field(description="Estimated blast radius / scope")
    review_depth: ReviewDepth = Field(
        default=ReviewDepth.standard,
        description="How thoroughly the reviewer node should look at the diff",
    )
    should_skip: bool = Field(
        default=False,
        description="True only for trivial changes that explicitly do not need review",
    )


class GatheredContext(BaseModel):
    """What the Context Gatherer hands off to the Reviewer.

    Filled by the model via the ``final_answer`` tool at the end of the
    gathering loop. Kept deliberately small so the Reviewer's prompt
    stays bounded; the raw diff and tool messages live in the graph
    state for trace/debug, not in the prompt.
    """

    summary: str = Field(description="Plain-English description of what the PR does")
    relevant_files: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths the gatherer found worth reading",
    )
    linked_issues: list[LinkedIssue] = Field(
        default_factory=list[LinkedIssue],
        description="Issues referenced via Closes/Fixes/Resolves and successfully fetched",
    )
    notes: str = Field(
        default="",
        description="Open questions or caveats the Reviewer should keep in mind",
    )


class Severity(StrEnum):
    nit = "nit"
    suggestion = "suggestion"
    issue = "issue"
    blocker = "blocker"


class ApprovalLevel(StrEnum):
    approve = "approve"
    comment = "comment"
    request_changes = "request_changes"


class InlineComment(BaseModel):
    path: str = Field(description="Repo-relative path of the file the comment refers to")
    line: int = Field(gt=0, description="1-indexed line number in the post-PR file")
    body: str = Field(description="Comment text — markdown allowed")
    severity: Severity = Field(default=Severity.suggestion)


class ReviewResult(BaseModel):
    """Structured output of the Reviewer node.

    The Publisher transforms this into either a single issue comment
    (W2-Task4 transitional behaviour) or a real GitHub PR review with
    inline annotations (W2-Task5).
    """

    overall_comment: str = Field(
        description="Markdown summary of the review — strengths, weaknesses, overall verdict"
    )
    inline_comments: list[InlineComment] = Field(
        default_factory=list[InlineComment],
        description="Per-line comments anchored to specific files and lines",
    )
    approval: ApprovalLevel = Field(
        default=ApprovalLevel.comment,
        description="Recommendation: approve / comment / request_changes",
    )

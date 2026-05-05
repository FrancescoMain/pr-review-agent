"""Domain models for the agent (separate from GitHub wire models).

These types describe the agent's *internal* contracts: triage decisions,
review comments, etc. Kept distinct from ``pr_review_agent.github.models``
which mirrors the GitHub API.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


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

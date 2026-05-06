"""Dataset schema + YAML loader for the eval harness.

A dataset is a list of ``EvalCase`` entries. Each entry pins a
specific PR (repo + pr_number + head_sha) and declares what an
attentive human reviewer would expect: the keywords any decent
review must surface (``must_flag``), the false positives the bot
must NOT emit (``must_not_flag``), and optional approval/skip
expectations.

YAML over JSON because the dataset is hand-edited; YAML supports
multi-line ``notes:`` and is line-diff-friendly.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from pr_review_agent.agent.models import ApprovalLevel, Severity

SeveritySelector = Literal[
    "any", "nit", "suggestion", "issue", "blocker", ">=suggestion", ">=issue", ">=blocker"
]


class MustFlagRule(BaseModel):
    """One thing the review is expected to flag.

    ``keyword`` is matched as a case-insensitive substring against
    every ``inline_comments[].body`` and against ``overall_comment``.
    ``severity`` is a selector (see ``SeveritySelector``); the rule
    passes when at least one matching comment has the requested
    severity bracket.
    """

    keyword: str = Field(min_length=1)
    severity: SeveritySelector = "any"


class MustNotFlagRule(BaseModel):
    """A keyword that, if it appears in the review, signals a false positive."""

    keyword: str = Field(min_length=1)


class ExpectedReview(BaseModel):
    must_flag: list[MustFlagRule] = Field(default_factory=list[MustFlagRule])
    must_not_flag: list[MustNotFlagRule] = Field(default_factory=list[MustNotFlagRule])
    expected_approval: ApprovalLevel | None = None
    expected_skip: bool | None = None


class EvalCase(BaseModel):
    id: str = Field(min_length=1)
    repo: str = Field(min_length=1, description="GitHub repo as owner/name")
    pr_number: int = Field(gt=0)
    head_sha: str = Field(min_length=40, description="Pinned commit SHA")
    head_ref: str = Field(default="eval", description="Branch label (informative only)")
    pr_title: str = ""
    pr_body: str = ""
    installation_id: int | None = Field(
        default=None,
        description="Falls back to Settings.github_default_installation_id when None",
    )
    expected: ExpectedReview = Field(default_factory=ExpectedReview)
    notes: str = ""


def load_dataset(path: Path) -> list[EvalCase]:
    """Read and validate ``dataset.yaml``. Raises Pydantic ValidationError on bad shape."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"dataset {path} must be a YAML list of cases, got {type(raw).__name__}")
    return [EvalCase.model_validate(entry) for entry in raw]


# Re-export Severity for callers that need to compare with rule selectors
# without reaching into agent.models.
__all__ = [
    "EvalCase",
    "ExpectedReview",
    "MustFlagRule",
    "MustNotFlagRule",
    "Severity",
    "SeveritySelector",
    "load_dataset",
]

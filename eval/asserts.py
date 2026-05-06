"""Rule-based checks comparing an ``ExpectedReview`` to the agent's actual review.

Each ``check_*`` function returns a ``list[str]``: empty means the
rule passed, populated lines are human-readable failure messages
ready for the markdown report.

Severity ordering mirrors ``Severity`` (nit < suggestion < issue <
blocker). The selector grammar in the dataset YAML maps as:

- ``"any"``        → match any severity
- exact name       → match only that severity (``"issue"`` matches issue)
- ``">=X"``        → match X and anything stricter
"""

from typing import cast

from eval.dataset import (
    ExpectedReview,
    MustFlagRule,
    MustNotFlagRule,
    SeveritySelector,
)
from pr_review_agent.agent.models import ApprovalLevel, ReviewResult, Severity, TriageDecision

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.nit: 0,
    Severity.suggestion: 1,
    Severity.issue: 2,
    Severity.blocker: 3,
}


def _severity_matches(comment_severity: Severity, selector: SeveritySelector) -> bool:
    if selector == "any":
        return True
    if selector.startswith(">="):
        target = selector[2:]
        try:
            min_rank = _SEVERITY_RANK[Severity(target)]
        except (ValueError, KeyError):
            return False
        return _SEVERITY_RANK[comment_severity] >= min_rank
    try:
        return comment_severity == Severity(selector)
    except ValueError:
        return False


def _haystack(review: ReviewResult) -> list[tuple[str, Severity | None]]:
    """Return all candidate text + severity tuples a rule may match.

    The overall comment has no severity (None matches any selector).
    """
    items: list[tuple[str, Severity | None]] = [(review.overall_comment, None)]
    for c in review.inline_comments:
        items.append((c.body, c.severity))
    return items


def check_must_flag(rules: list[MustFlagRule], review: ReviewResult) -> list[str]:
    """Each rule must find at least one matching (text, severity) pair."""
    failures: list[str] = []
    haystack = _haystack(review)
    for rule in rules:
        keyword_lower = rule.keyword.lower()
        matched = False
        for text, severity in haystack:
            if keyword_lower not in text.lower():
                continue
            if severity is None:
                # overall_comment: severity selector "any" passes; specific
                # selectors only pass when the overall comment carries the
                # keyword AND the selector is "any".
                if rule.severity == "any":
                    matched = True
                    break
                continue
            if _severity_matches(severity, cast(SeveritySelector, rule.severity)):
                matched = True
                break
        if not matched:
            failures.append(
                f"must_flag: no comment matched keyword={rule.keyword!r} severity={rule.severity!r}"
            )
    return failures


def check_must_not_flag(rules: list[MustNotFlagRule], review: ReviewResult) -> list[str]:
    """No rule's keyword may appear anywhere in the review."""
    failures: list[str] = []
    haystack = _haystack(review)
    for rule in rules:
        keyword_lower = rule.keyword.lower()
        for text, _severity in haystack:
            if keyword_lower in text.lower():
                failures.append(
                    f"must_not_flag: review contains forbidden keyword={rule.keyword!r}"
                )
                break
    return failures


def check_approval(expected: ApprovalLevel | None, review: ReviewResult) -> list[str]:
    if expected is None:
        return []
    if review.approval == expected:
        return []
    return [f"approval mismatch: expected={expected.value} actual={review.approval.value}"]


def check_skip(expected: bool | None, triage: TriageDecision | None) -> list[str]:
    if expected is None:
        return []
    actual = bool(triage is not None and triage.should_skip)
    if actual == expected:
        return []
    return [f"skip mismatch: expected={expected} actual={actual}"]


def evaluate_case(
    *,
    expected: ExpectedReview,
    review: ReviewResult | None,
    triage: TriageDecision | None,
) -> list[str]:
    """Run every checker and return the merged list of failures.

    When ``review`` is ``None`` (e.g. the agent skipped or aborted)
    we only run the skip check; the must_flag rules can't be
    evaluated and aren't credited as passes.
    """
    failures = check_skip(expected.expected_skip, triage)
    if review is None:
        if expected.must_flag:
            failures.append(
                f"no review produced; cannot check {len(expected.must_flag)} must_flag rule(s)"
            )
        return failures
    failures.extend(check_must_flag(expected.must_flag, review))
    failures.extend(check_must_not_flag(expected.must_not_flag, review))
    failures.extend(check_approval(expected.expected_approval, review))
    return failures

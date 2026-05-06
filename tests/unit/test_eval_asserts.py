"""Unit tests for the eval rule checkers.

The checkers are deterministic and should be robust to:
- case-insensitive substring match for keywords
- severity selectors ("any", exact, ">=X")
- absent triage (skip check)
- absent review (must_flag → reported as missing, must_not_flag → silent)
"""

from eval.asserts import (
    check_approval,
    check_must_flag,
    check_must_not_flag,
    check_skip,
    evaluate_case,
)
from eval.dataset import ExpectedReview, MustFlagRule, MustNotFlagRule

from pr_review_agent.agent.models import (
    ApprovalLevel,
    ChangeType,
    InlineComment,
    ReviewResult,
    RiskLevel,
    Severity,
    TriageDecision,
)


def _review(
    *,
    overall: str = "",
    inline: list[InlineComment] | None = None,
    approval: ApprovalLevel = ApprovalLevel.comment,
) -> ReviewResult:
    return ReviewResult(
        overall_comment=overall,
        inline_comments=inline or [],
        approval=approval,
    )


def test_must_flag_passes_with_keyword_in_inline_body() -> None:
    review = _review(
        inline=[
            InlineComment(
                path="x.py", line=1, body="Watch out for ZeroDivisionError", severity=Severity.issue
            )
        ]
    )
    rules = [MustFlagRule(keyword="ZeroDivisionError", severity="any")]
    assert check_must_flag(rules, review) == []


def test_must_flag_passes_via_overall_comment_for_any_selector() -> None:
    review = _review(overall="The test coverage is missing for the edge case")
    rules = [MustFlagRule(keyword="test", severity="any")]
    assert check_must_flag(rules, review) == []


def test_must_flag_overall_does_not_match_specific_severity() -> None:
    """A keyword in overall_comment shouldn't match a >=issue rule (no severity carrier)."""
    review = _review(overall="ZeroDivisionError mentioned in overall")
    rules = [MustFlagRule(keyword="ZeroDivisionError", severity=">=issue")]
    fails = check_must_flag(rules, review)
    assert len(fails) == 1
    assert "ZeroDivisionError" in fails[0]


def test_must_flag_geq_severity_passes_when_matched_at_higher_rank() -> None:
    review = _review(
        inline=[
            InlineComment(path="x.py", line=1, body="ZeroDivisionError", severity=Severity.blocker)
        ]
    )
    rules = [MustFlagRule(keyword="ZeroDivisionError", severity=">=issue")]
    assert check_must_flag(rules, review) == []


def test_must_flag_geq_severity_fails_when_only_matched_at_lower_rank() -> None:
    review = _review(
        inline=[InlineComment(path="x.py", line=1, body="ZeroDivisionError", severity=Severity.nit)]
    )
    rules = [MustFlagRule(keyword="ZeroDivisionError", severity=">=issue")]
    fails = check_must_flag(rules, review)
    assert len(fails) == 1


def test_must_flag_keyword_match_is_case_insensitive() -> None:
    review = _review(
        inline=[
            InlineComment(path="x.py", line=1, body="zerodivisionerror", severity=Severity.issue)
        ]
    )
    rules = [MustFlagRule(keyword="ZeroDivisionError", severity="any")]
    assert check_must_flag(rules, review) == []


def test_must_not_flag_fails_when_keyword_appears_anywhere() -> None:
    review = _review(
        inline=[
            InlineComment(path="x.py", line=1, body="this is a blocker", severity=Severity.blocker)
        ]
    )
    rules = [MustNotFlagRule(keyword="blocker")]
    assert len(check_must_not_flag(rules, review)) == 1


def test_must_not_flag_passes_when_keyword_absent() -> None:
    review = _review(overall="LGTM")
    rules = [MustNotFlagRule(keyword="ZeroDivisionError")]
    assert check_must_not_flag(rules, review) == []


def test_check_approval_pass_and_fail() -> None:
    review = _review(approval=ApprovalLevel.request_changes)
    assert check_approval(ApprovalLevel.request_changes, review) == []
    fails = check_approval(ApprovalLevel.approve, review)
    assert len(fails) == 1


def test_check_approval_skipped_when_expected_is_none() -> None:
    review = _review(approval=ApprovalLevel.comment)
    assert check_approval(None, review) == []


def test_check_skip_matches_triage() -> None:
    triage = TriageDecision(change_type=ChangeType.docs, risk_level=RiskLevel.low, should_skip=True)
    assert check_skip(True, triage) == []
    assert check_skip(False, triage) != []


def test_check_skip_handles_no_triage() -> None:
    assert check_skip(False, None) == []
    fails = check_skip(True, None)
    assert len(fails) == 1


def test_evaluate_case_combines_all_checks() -> None:
    review = _review(
        inline=[
            InlineComment(
                path="x.py", line=1, body="Unguarded ZeroDivisionError", severity=Severity.issue
            ),
            # Forbidden keyword present:
            InlineComment(path="x.py", line=2, body="hard blocker", severity=Severity.nit),
        ],
        approval=ApprovalLevel.comment,
    )
    expected = ExpectedReview(
        must_flag=[MustFlagRule(keyword="ZeroDivisionError", severity=">=issue")],
        must_not_flag=[MustNotFlagRule(keyword="blocker")],
        expected_approval=ApprovalLevel.request_changes,
        expected_skip=False,
    )
    failures = evaluate_case(expected=expected, review=review, triage=None)
    # Must_flag passes; must_not_flag fails; approval mismatch; skip ok.
    assert len(failures) == 2
    assert any("blocker" in f for f in failures)
    assert any("approval" in f for f in failures)


def test_evaluate_case_when_review_is_none_only_runs_skip_check() -> None:
    expected = ExpectedReview(
        must_flag=[MustFlagRule(keyword="x", severity="any")],
        expected_skip=True,
    )
    triage = TriageDecision(change_type=ChangeType.docs, risk_level=RiskLevel.low, should_skip=True)
    failures = evaluate_case(expected=expected, review=None, triage=triage)
    # Skip ✅; must_flag flagged as "no review produced".
    assert len(failures) == 1
    assert "no review produced" in failures[0]

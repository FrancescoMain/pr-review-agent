"""Unit tests for the eval dataset loader.

The loader is a thin Pydantic-validated YAML reader; the tests pin
the shape (required fields, defaults, validation errors) so a typo
in ``eval/dataset.yaml`` fails the eval at load time, not deep
inside the run.
"""

from pathlib import Path

import pytest
from eval.dataset import EvalCase, MustFlagRule, MustNotFlagRule, load_dataset
from pydantic import ValidationError


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "dataset.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_minimal_dataset(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: smoke
  repo: x/y
  pr_number: 1
  head_sha: "0000000000000000000000000000000000000000"
""",
    )
    cases = load_dataset(p)
    assert len(cases) == 1
    case = cases[0]
    assert isinstance(case, EvalCase)
    assert case.id == "smoke"
    # Defaults filled in:
    assert case.head_ref == "eval"
    assert case.installation_id is None
    assert case.expected.must_flag == []
    assert case.expected.must_not_flag == []
    assert case.expected.expected_skip is None
    assert case.notes == ""


def test_load_full_dataset_with_rules(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: bug-pr
  repo: francesco/playground
  pr_number: 2
  head_sha: "1234567890123456789012345678901234567890"
  pr_title: "feat: x"
  pr_body: "Body"
  installation_id: 42
  expected:
    expected_approval: request_changes
    expected_skip: false
    must_flag:
      - keyword: "ZeroDivisionError"
        severity: ">=issue"
      - keyword: "test"
    must_not_flag:
      - keyword: "blocker"
  notes: "Some notes"
""",
    )
    cases = load_dataset(p)
    assert len(cases) == 1
    case = cases[0]
    assert case.installation_id == 42
    assert case.expected.expected_approval is not None
    assert case.expected.expected_skip is False
    assert case.expected.must_flag == [
        MustFlagRule(keyword="ZeroDivisionError", severity=">=issue"),
        MustFlagRule(keyword="test", severity="any"),
    ]
    assert case.expected.must_not_flag == [MustNotFlagRule(keyword="blocker")]
    assert case.notes == "Some notes"


def test_load_empty_yaml_returns_empty_list(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "")
    assert load_dataset(p) == []


def test_load_rejects_non_list_root(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "id: not-a-list\n")
    with pytest.raises(ValueError, match="must be a YAML list"):
        load_dataset(p)


def test_load_rejects_short_sha(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: bad
  repo: x/y
  pr_number: 1
  head_sha: "abc"
""",
    )
    with pytest.raises(ValidationError):
        load_dataset(p)


def test_load_rejects_zero_pr_number(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: bad
  repo: x/y
  pr_number: 0
  head_sha: "0000000000000000000000000000000000000000"
""",
    )
    with pytest.raises(ValidationError):
        load_dataset(p)

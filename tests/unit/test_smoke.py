"""Sanity tests for the package scaffold.

Verifies the package is importable and exposes a sensible ``__version__``.
The point is to fail fast if the scaffold breaks (missing ``__init__``,
broken ``pyproject.toml``, etc.), not to test business logic.
"""

import re

import pr_review_agent


def test_package_importable() -> None:
    assert pr_review_agent is not None


def test_version_string_is_semver_like() -> None:
    version = pr_review_agent.__version__
    assert isinstance(version, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-.+].+)?", version), (
        f"__version__ {version!r} is not a semver-like string"
    )

"""Tests for ``pr_review_agent.config.Settings``.

Only checks the small subset of fields exposed in Task 2; new fields will be
added incrementally as the tasks that consume them land.
"""

import pytest

from pr_review_agent.config import Settings


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENVIRONMENT", "LOG_LEVEL", "COST_CAP_PER_PR_USD", "MAX_TOOL_CALLS_PER_NODE"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.cost_cap_per_pr_usd == 0.50
    assert settings.max_tool_calls_per_node == 15


def test_settings_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("COST_CAP_PER_PR_USD", "1.25")
    monkeypatch.setenv("MAX_TOOL_CALLS_PER_NODE", "30")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment == "production"
    assert settings.log_level == "WARNING"
    assert settings.cost_cap_per_pr_usd == 1.25
    assert settings.max_tool_calls_per_node == 30

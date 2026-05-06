"""Tests for ``pr_review_agent.config.Settings``.

Only checks the small subset of fields exposed in Task 2; new fields will be
added incrementally as the tasks that consume them land.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pr_review_agent.config import Settings


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENVIRONMENT", "LOG_LEVEL", "COST_CAP_PER_PR_USD", "MAX_TOOL_CALLS_PER_NODE"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.cost_cap_per_pr_usd == 0.50
    assert settings.max_tool_calls_per_node == 15


def test_settings_reads_env_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pem = tmp_path / "fake.pem"
    pem.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("COST_CAP_PER_PR_USD", "1.25")
    monkeypatch.setenv("MAX_TOOL_CALLS_PER_NODE", "30")
    monkeypatch.setenv("GITHUB_APP_ID", "987654")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "pr-review-agent-ci")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment == "production"
    assert settings.log_level == "WARNING"
    assert settings.cost_cap_per_pr_usd == 1.25
    assert settings.max_tool_calls_per_node == 30
    assert settings.github_app_id == 987654
    assert settings.langsmith_tracing is True
    assert settings.langsmith_project == "pr-review-agent-ci"


def test_langsmith_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.langsmith_tracing is False
    assert settings.langsmith_api_key.get_secret_value() == ""
    assert settings.langsmith_project == "pr-review-agent"


def test_database_url_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_url is None


def test_rate_limit_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GITHUB_RATE_LIMIT_FLOOR", "GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.github_rate_limit_floor == 100
    assert settings.github_rate_limit_max_wait_seconds == 60


def test_rate_limit_settings_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RATE_LIMIT_FLOOR", "500")
    monkeypatch.setenv("GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS", "30")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.github_rate_limit_floor == 500
    assert settings.github_rate_limit_max_wait_seconds == 30


def test_qdrant_settings_default_to_none_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.qdrant_url is None
    assert settings.qdrant_api_key.get_secret_value() == ""


def test_convention_doc_globs_have_sensible_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONVENTION_DOC_GLOBS", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "CLAUDE.md" in settings.convention_doc_globs
    assert "README.md" in settings.convention_doc_globs
    assert "docs/**/*.md" in settings.convention_doc_globs


def test_convention_recall_top_k_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVENTION_RECALL_TOP_K", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.convention_recall_top_k == 5

    monkeypatch.setenv("CONVENTION_RECALL_TOP_K", "10")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.convention_recall_top_k == 10


def test_database_url_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://pr_review:pr_review@localhost:5433/pr_review_agent"
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (
        settings.database_url == "postgresql://pr_review:pr_review@localhost:5433/pr_review_agent"
    )


def test_settings_validator_rejects_missing_credentials_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]

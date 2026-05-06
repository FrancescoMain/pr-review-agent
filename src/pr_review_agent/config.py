"""Application configuration.

Pydantic Settings reading from ``.env`` (see ``.env.example`` and SETUP.md §5).
Only the fields needed by the currently-implemented tasks live here; new
fields are added when the code that consumes them lands, to avoid premature
config surface area.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    cost_cap_per_pr_usd: float = 0.50
    max_tool_calls_per_node: int = 15

    # GitHub API rate-limit reactive guardrail (W3-Task3).
    # We never cross under `floor` requests remaining; if the reset is more
    # than `max_wait_seconds` away we abort the run instead of sleeping.
    github_rate_limit_floor: int = 100
    github_rate_limit_max_wait_seconds: int = 60

    github_webhook_secret: SecretStr = SecretStr("")
    github_app_id: int = 0
    github_app_private_key_path: Path | None = None
    anthropic_api_key: SecretStr = SecretStr("")

    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "pr-review-agent"

    # Postgres for run/cost persistence. None disables persistence with a warning;
    # the agent still runs, it just won't be observable through the cost tables.
    # Default points at the docker-compose container from W1-Task6 (host port 5433).
    database_url: str | None = None

    # Qdrant for the convention-memory subsystem (W3-Task4). None disables the
    # ingest CLI gracefully. Default in .env.example points at the docker-compose
    # qdrant service on port 6333. The api_key is empty for local dev and is
    # filled when pointing at hosted Qdrant cloud.
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr = SecretStr("")
    convention_doc_globs: list[str] = [
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
        "README.rst",
        "CONTRIBUTING.md",
        ".editorconfig",
        "docs/**/*.md",
    ]

    @model_validator(mode="after")
    def _require_credentials_outside_dev(self) -> Self:
        if self.environment != "development":
            if self.github_app_id <= 0:
                raise ValueError("github_app_id must be a positive integer outside development")
            if self.github_app_private_key_path is None:
                raise ValueError("github_app_private_key_path must be set outside development")
            if not self.anthropic_api_key.get_secret_value():
                raise ValueError("anthropic_api_key must be set outside development")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

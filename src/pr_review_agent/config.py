"""Application configuration.

Pydantic Settings reading from ``.env`` (see ``.env.example`` and SETUP.md §5).
Only the fields needed by the currently-implemented tasks live here; new
fields are added when the code that consumes them lands, to avoid premature
config surface area.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
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

    github_webhook_secret: SecretStr = SecretStr("")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

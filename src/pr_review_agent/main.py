# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""FastAPI application entry point.

Wires routers, configures structured logging, and — at startup —
composes the long-lived dependencies the agent needs (httpx pool,
GitHub auth/client, optional Postgres pool, runner) into
``app.state``. The webhook handler reads them from there via
``request.app.state`` so we have one connection pool and one in-memory
token cache per process.

Exception handlers map domain errors to safe HTTP responses without
leaking internals (signature failures are always a generic 401).
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal

import asyncpg
import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pr_review_agent.agent.runner import AgentRunner, make_default_runner
from pr_review_agent.config import Settings, get_settings
from pr_review_agent.db import apply_migrations
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.exceptions import WebhookSignatureError
from pr_review_agent.health import router as health_router
from pr_review_agent.observability.logging import configure_logging
from pr_review_agent.webhook import router as webhook_router

_log = structlog.get_logger(__name__)


def _enable_langsmith_tracing(settings: Settings) -> None:
    """LangChain reads LANGSMITH_* directly from os.environ; pydantic-settings
    only loads the values into our Settings object, so we propagate them here
    when tracing is enabled. We use setdefault so an explicit shell export
    still wins over the .env value.
    """
    if not settings.langsmith_tracing:
        return
    api_key = settings.langsmith_api_key.get_secret_value()
    if not api_key:
        _log.warning("LangSmith tracing requested but LANGSMITH_API_KEY is empty")
        return
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    _log.info("LangSmith tracing enabled", project=settings.langsmith_project)


def _build_runner(
    settings: Settings,
    http: httpx.AsyncClient,
    db_pool: asyncpg.Pool | None,  # type: ignore[type-arg]
) -> AgentRunner | None:
    if settings.github_app_id <= 0:
        _log.warning("agent runner disabled: GITHUB_APP_ID not set")
        return None
    if settings.github_app_private_key_path is None:
        _log.warning("agent runner disabled: GITHUB_APP_PRIVATE_KEY_PATH not set")
        return None
    if not settings.github_app_private_key_path.exists():
        _log.warning(
            "agent runner disabled: GitHub App private key file not found",
            path=str(settings.github_app_private_key_path),
        )
        return None
    if not settings.anthropic_api_key.get_secret_value():
        _log.warning("agent runner disabled: ANTHROPIC_API_KEY not set")
        return None
    pem = settings.github_app_private_key_path.read_text(encoding="utf-8")
    auth = GitHubAppAuth(app_id=settings.github_app_id, private_key=pem, http_client=http)
    github_client = GitHubClient(auth=auth, http_client=http)
    return make_default_runner(
        anthropic_api_key=settings.anthropic_api_key.get_secret_value(),
        github_client=github_client,
        github_auth=auth,
        db_pool=db_pool,
        cost_cap_usd=Decimal(str(settings.cost_cap_per_pr_usd)),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_logs=settings.environment != "development",
    )
    _enable_langsmith_tracing(settings)

    db_pool: asyncpg.Pool | None = None  # type: ignore[type-arg]
    if settings.database_url:
        try:
            db_pool = await asyncpg.create_pool(settings.database_url)
            assert db_pool is not None  # narrow for type checkers
            await apply_migrations(db_pool)
            _log.info("database pool ready and migrations applied")
        except Exception:
            _log.exception("failed to initialise database pool; persistence disabled")
            db_pool = None
    else:
        _log.warning("DATABASE_URL not set: agent runs will not be persisted")

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            app.state.http_client = http
            app.state.db_pool = db_pool
            app.state.agent_runner = _build_runner(settings, http, db_pool)
            yield
    finally:
        if db_pool is not None:
            await db_pool.close()


app = FastAPI(
    title="PR Review Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(webhook_router)


@app.exception_handler(WebhookSignatureError)
async def _signature_error_handler(  # pyright: ignore[reportUnusedFunction]
    _request: Request, _exc: WebhookSignatureError
) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "invalid signature"})

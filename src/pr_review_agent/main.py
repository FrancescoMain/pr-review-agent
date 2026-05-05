"""FastAPI application entry point.

Wires routers, configures structured logging, and — at startup —
composes the long-lived dependencies the agent needs (httpx pool,
GitHub auth/client, runner) into ``app.state``. The webhook handler
reads them from there via ``request.app.state`` so we have one
connection pool and one in-memory token cache per process.

Exception handlers map domain errors to safe HTTP responses without
leaking internals (signature failures are always a generic 401).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pr_review_agent.agent.runner import AgentRunner, make_default_runner
from pr_review_agent.config import Settings, get_settings
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.exceptions import WebhookSignatureError
from pr_review_agent.health import router as health_router
from pr_review_agent.observability.logging import configure_logging
from pr_review_agent.webhook import router as webhook_router

_log = structlog.get_logger(__name__)


def _build_runner(settings: Settings, http: httpx.AsyncClient) -> AgentRunner | None:
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
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_logs=settings.environment != "development",
    )
    async with httpx.AsyncClient(timeout=30.0) as http:
        app.state.http_client = http
        app.state.agent_runner = _build_runner(settings, http)
        yield


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

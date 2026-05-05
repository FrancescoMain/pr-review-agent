"""FastAPI application entry point.

Wires the health and webhook routers, configures structured logging at
startup, and registers exception handlers that map domain errors to
appropriate HTTP responses without leaking internals (e.g. signature
problems are always reported as a generic 401, never disclosing whether
the header was missing, malformed, or just wrong).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pr_review_agent.config import get_settings
from pr_review_agent.github.exceptions import WebhookSignatureError
from pr_review_agent.health import router as health_router
from pr_review_agent.observability.logging import configure_logging
from pr_review_agent.webhook import router as webhook_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_logs=settings.environment != "development",
    )
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

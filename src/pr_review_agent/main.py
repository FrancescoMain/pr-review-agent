"""FastAPI application entry point.

Wires the health and webhook routers and configures structured logging at
startup. The lifespan hook is intentionally empty for now; database, Qdrant
and HTTP-client pools will be initialised here in later tasks (see SPEC.md
§5 and §8).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pr_review_agent.config import get_settings
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

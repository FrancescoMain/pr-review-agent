"""Correlation-ID utilities for structured logs and LangSmith metadata.

Every webhook delivery gets a stable identifier we can grep in
production logs and filter on in LangSmith. We use the
``X-GitHub-Delivery`` header GitHub already produces (UUID-shaped, one
per delivery, retried with the same value) when available, and fall
back to a freshly minted ``uuid4`` for tests and manual replays.

Propagation is via ``structlog.contextvars`` so every log line emitted
under the bound scope carries ``correlation_id`` automatically. The
binding is per-asyncio-Task: when the FastAPI ``BackgroundTask`` fires
on a different task, the request handler's binding does NOT leak in.
``bind_correlation_id`` is therefore called from inside the background
wrapper, not from the route.
"""

import uuid

import structlog


def bind_correlation_id(*, request_id: str | None) -> str:
    """Bind ``correlation_id`` on the current contextvar scope and return it.

    ``request_id`` is the upstream identifier (``X-GitHub-Delivery`` for
    real deliveries). When it's ``None`` or empty we generate a uuid so
    every run has *some* identifier, even tests and manual replays.
    """
    cid = request_id if request_id else uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def clear_correlation() -> None:
    """Drop ``correlation_id`` from the current contextvar scope.

    Called after the agent run completes (success or failure) so the
    next task on this asyncio loop doesn't inherit a stale id.
    """
    structlog.contextvars.unbind_contextvars("correlation_id")

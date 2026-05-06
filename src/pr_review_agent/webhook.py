"""GitHub webhook router.

Pipeline for an incoming delivery:
  1. read raw body and the ``X-Hub-Signature-256`` header;
  2. verify the HMAC-SHA256 signature against the configured secret;
  3. parse JSON, returning 400 if it isn't valid;
  4. for events other than ``pull_request`` return 202 ignored
     (GitHub sends ``ping`` on App install, ``installation`` on add/remove,
     etc.; we acknowledge them so GitHub stops retrying);
  5. validate the payload with the Pydantic model, returning 422 on shape
     mismatch;
  6. **idempotency check** (W3-Task2): if the ``X-GitHub-Delivery`` of
     this request has already been seen — there's an ``agent_runs`` row
     with that ``correlation_id`` — return 202 with
     ``status='duplicate'`` and skip the dispatch. Protects against
     GitHub's automatic redeliveries (timeout, 5xx) creating duplicate
     runs and double-spending tokens. If the DB is unreachable the
     check is skipped with a warning — availability beats idempotency.
  7. schedule the agent run as a FastAPI ``BackgroundTask`` and return
     202 immediately. The agent receives a fully-typed ``AgentState``;
     errors in the background task are caught and logged via structlog
     so they never propagate to the GitHub delivery.

Every delivery binds a ``correlation_id`` (the ``X-GitHub-Delivery``
header from GitHub, or a uuid fallback) on structlog's contextvars
scope. The 202 response echoes ``delivery_id`` so the caller can
copy-paste it to grep logs / filter LangSmith traces. The background
task re-binds the correlation explicitly — contextvars normally
propagate into ``asyncio.create_task``, but we're defensive: a
mistakenly-shared task or a future framework change shouldn't silently
drop our id.

The 202 is the contract GitHub expects: acknowledge fast, do the work
asynchronously.
"""

import json
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import ValidationError

from pr_review_agent.agent.runner import AgentRunner
from pr_review_agent.agent.state import AgentState
from pr_review_agent.config import Settings, get_settings
from pr_review_agent.db import find_run_by_correlation_id
from pr_review_agent.github.models import PullRequestEvent
from pr_review_agent.github.signatures import verify_signature
from pr_review_agent.observability.correlation import (
    bind_correlation_id,
    clear_correlation,
)

router = APIRouter(tags=["webhook"])

_log = structlog.get_logger(__name__)


async def _run_agent_safely(runner: AgentRunner, state: AgentState, *, correlation_id: str) -> None:
    bind_correlation_id(request_id=correlation_id)
    try:
        await runner(state)
    except Exception:
        _log.exception(
            "agent run failed",
            repo=state.get("repo"),
            pr_number=state.get("pr_number"),
            installation_id=state.get("installation_id"),
        )
    finally:
        clear_correlation()


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    delivery_id = bind_correlation_id(request_id=request.headers.get("X-GitHub-Delivery"))

    body = await request.body()
    verify_signature(
        body=body,
        signature_header=request.headers.get("X-Hub-Signature-256"),
        secret=settings.github_webhook_secret.get_secret_value(),
    )

    event = request.headers.get("X-GitHub-Event", "")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="body is not valid JSON") from exc

    if event != "pull_request":
        return {
            "status": "ignored",
            "reason": f"event not handled: {event or '<missing>'}",
            "delivery_id": delivery_id,
        }

    try:
        pr_event = PullRequestEvent.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    state: AgentState = {
        "repo": pr_event.repository.full_name,
        "pr_number": pr_event.number,
        "pr_title": pr_event.pull_request.title,
        "pr_body": pr_event.pull_request.body or "",
        "installation_id": pr_event.installation.id,
        "head_ref": pr_event.pull_request.head.ref,
        "head_sha": pr_event.pull_request.head.sha,
        "tokens_used": {},
        "errors": [],
    }

    # Idempotency: skip dispatch if we've already seen this exact delivery.
    # Only meaningful when GitHub provided X-GitHub-Delivery (not the uuid
    # fallback path) AND we have a DB pool to query.
    upstream_delivery = request.headers.get("X-GitHub-Delivery")
    db_pool = getattr(request.app.state, "db_pool", None)
    if upstream_delivery and db_pool is not None:
        try:
            existing_run_id = await find_run_by_correlation_id(
                db_pool, correlation_id=upstream_delivery
            )
        except Exception:
            _log.warning(
                "idempotency_check_failed; dispatching anyway",
                delivery_id=delivery_id,
                exc_info=True,
            )
            existing_run_id = None
        if existing_run_id is not None:
            _log.info(
                "duplicate delivery; skipping dispatch",
                delivery_id=delivery_id,
                first_seen_run_id=existing_run_id,
                repo=state["repo"],
                pr_number=state["pr_number"],
            )
            return {
                "status": "duplicate",
                "pr": pr_event.number,
                "repo": pr_event.repository.full_name,
                "installation_id": pr_event.installation.id,
                "delivery_id": delivery_id,
                "first_seen_run_id": existing_run_id,
            }

    runner: AgentRunner | None = getattr(request.app.state, "agent_runner", None)
    if runner is None:
        _log.warning(
            "agent runner not configured; skipping dispatch",
            repo=state["repo"],
            pr_number=state["pr_number"],
        )
    else:
        background_tasks.add_task(_run_agent_safely, runner, state, correlation_id=delivery_id)

    return {
        "status": "accepted",
        "pr": pr_event.number,
        "repo": pr_event.repository.full_name,
        "installation_id": pr_event.installation.id,
        "delivery_id": delivery_id,
    }

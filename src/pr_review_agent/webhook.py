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
  6. schedule the agent run as a FastAPI ``BackgroundTask`` and return
     202 immediately. The agent receives a fully-typed ``AgentState``;
     errors in the background task are caught and logged via structlog
     so they never propagate to the GitHub delivery.

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
from pr_review_agent.github.models import PullRequestEvent
from pr_review_agent.github.signatures import verify_signature

router = APIRouter(tags=["webhook"])

_log = structlog.get_logger(__name__)


async def _run_agent_safely(runner: AgentRunner, state: AgentState) -> None:
    try:
        await runner(state)
    except Exception:
        _log.exception(
            "agent run failed",
            repo=state.get("repo"),
            pr_number=state.get("pr_number"),
            installation_id=state.get("installation_id"),
        )


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
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
        return {"status": "ignored", "reason": f"event not handled: {event or '<missing>'}"}

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

    runner: AgentRunner | None = getattr(request.app.state, "agent_runner", None)
    if runner is None:
        _log.warning(
            "agent runner not configured; skipping dispatch",
            repo=state["repo"],
            pr_number=state["pr_number"],
        )
    else:
        background_tasks.add_task(_run_agent_safely, runner, state)

    return {
        "status": "accepted",
        "pr": pr_event.number,
        "repo": pr_event.repository.full_name,
        "installation_id": pr_event.installation.id,
    }

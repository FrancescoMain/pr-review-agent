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
  6. return 202 with a tiny envelope. Background dispatch into the
     LangGraph agent lands in Task 4.

The request is intentionally synchronous up to the 202 — GitHub expects
a fast ack and the heavy work will be enqueued, not awaited inline.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from pr_review_agent.config import Settings, get_settings
from pr_review_agent.github.models import PullRequestEvent
from pr_review_agent.github.signatures import verify_signature

router = APIRouter(tags=["webhook"])


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str | int]:
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

    return {
        "status": "accepted",
        "pr": pr_event.number,
        "repo": pr_event.repository.full_name,
    }

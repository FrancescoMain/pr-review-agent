"""GitHub webhook router.

Task 2 only wires the routing surface: ``POST /webhook/github`` accepts any
JSON body and returns ``202 Accepted``. ``X-Hub-Signature-256`` verification
arrives in Task 3; background dispatch into the LangGraph agent lands in
Task 4. The 202 contract is what the publisher of GitHub deliveries expects:
acknowledge fast, do the work asynchronously.
"""

from fastapi import APIRouter, Request, status

router = APIRouter(tags=["webhook"])


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request) -> dict[str, str]:
    _ = await request.body()
    return {"status": "accepted"}

"""Health-check router.

Exposes ``GET /health`` for uptime probes and the Bruno collection. Returns
a stable, minimal payload — kept boring on purpose so deployment platforms
and dashboards can rely on it without parsing logic.
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Repository functions for the ``agent_runs`` table.

Three operations the runner calls per agent run:

- ``record_run_started`` inserts a row with ``status='running'`` and
  returns the new ``id`` so we can update it later by primary key.
- ``record_run_finished`` updates the row with the final
  ``status``, totals, ``per_model`` JSON, and ``finished_at``.
- ``record_run_failed`` updates the row with ``status='failure'`` and
  the truncated error message.

Queries are written in plain SQL (asyncpg). The shape of arguments
is captured by Pydantic-friendly dicts so tests can assert exactly
what reaches the DB.
"""

import json
from decimal import Decimal
from typing import Any

import asyncpg


async def record_run_started(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    correlation_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> int:
    async with pool.acquire() as conn:
        row_id: int = await conn.fetchval(
            """
            INSERT INTO agent_runs
                (correlation_id, repo, pr_number, head_sha, status)
            VALUES ($1, $2, $3, $4, 'running')
            RETURNING id
            """,
            correlation_id,
            repo,
            pr_number,
            head_sha,
        )
        return row_id


async def record_run_finished(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    run_id: int,
    status: str,
    triage_change_type: str | None,
    triage_risk_level: str | None,
    skipped: bool,
    tool_calls_used: int,
    tokens_input: int,
    tokens_output: int,
    cost_usd: Decimal,
    per_model: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs SET
                status              = $2,
                finished_at         = now(),
                triage_change_type  = $3,
                triage_risk_level   = $4,
                skipped             = $5,
                tool_calls_used     = $6,
                tokens_input        = $7,
                tokens_output       = $8,
                cost_usd            = $9,
                per_model           = $10::jsonb
            WHERE id = $1
            """,
            run_id,
            status,
            triage_change_type,
            triage_risk_level,
            skipped,
            tool_calls_used,
            tokens_input,
            tokens_output,
            cost_usd,
            json.dumps(per_model),
        )


async def find_run_by_correlation_id(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    correlation_id: str,
) -> int | None:
    """Return the ``agent_runs.id`` of any run with this correlation id, or ``None``.

    Used by the webhook handler to drop duplicate ``X-GitHub-Delivery``
    redeliveries before they trigger a second agent run. Hits any row
    regardless of status: a still-running, failed, or finished run all
    count as "already seen this delivery".
    """
    async with pool.acquire() as conn:
        row_id: int | None = await conn.fetchval(
            "SELECT id FROM agent_runs WHERE correlation_id = $1 LIMIT 1",
            correlation_id,
        )
        return row_id


async def record_run_failed(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    run_id: int,
    error: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs SET
                status      = 'failure',
                finished_at = now(),
                error       = $2
            WHERE id = $1
            """,
            run_id,
            error[:500],
        )

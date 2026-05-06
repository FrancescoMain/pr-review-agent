# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false
"""Unit tests for ``record_run_*`` against a fake asyncpg pool.

We don't spin up a real Postgres in unit tests — instead we hand in
a stub pool whose ``acquire()`` returns a stub connection that records
every ``execute`` / ``fetchval`` it sees. This catches changes to the
SQL string and parameter ordering, which is what we actually want to
freeze (the queries are short and explicit, so changes here are a
behaviour change).
"""

from decimal import Decimal
from typing import Any

import pytest

from pr_review_agent.db.runs import (
    record_run_failed,
    record_run_finished,
    record_run_started,
)


class _FakeConn:
    def __init__(self, fetchval_returns: Any = 1) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchvals: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchval_returns = fetchval_returns

    async def execute(self, sql: str, *params: Any) -> None:
        self.executes.append((sql, params))

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self.fetchvals.append((sql, params))
        return self._fetchval_returns


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakePool:
    def __init__(self, *, fetchval_returns: Any = 1) -> None:
        self.conn = _FakeConn(fetchval_returns=fetchval_returns)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


@pytest.fixture
def pool() -> _FakePool:
    return _FakePool(fetchval_returns=42)


async def test_record_run_started_returns_id_and_inserts_with_running_status(
    pool: _FakePool,
) -> None:
    run_id = await record_run_started(
        pool,  # type: ignore[arg-type]
        correlation_id="abc",
        repo="x/y",
        pr_number=7,
        head_sha="deadbeef",
    )
    assert run_id == 42
    assert len(pool.conn.fetchvals) == 1
    sql, params = pool.conn.fetchvals[0]
    assert "INSERT INTO agent_runs" in sql
    assert "'running'" in sql
    assert "RETURNING id" in sql
    assert params == ("abc", "x/y", 7, "deadbeef")


async def test_record_run_finished_updates_all_fields(pool: _FakePool) -> None:
    await record_run_finished(
        pool,  # type: ignore[arg-type]
        run_id=42,
        status="success",
        triage_change_type="bugfix",
        triage_risk_level="medium",
        skipped=False,
        tool_calls_used=4,
        tokens_input=1234,
        tokens_output=567,
        cost_usd=Decimal("0.012345"),
        per_model={"claude-sonnet-4-6": {"input": 1234, "output": 567}},
    )
    assert len(pool.conn.executes) == 1
    sql, params = pool.conn.executes[0]
    assert "UPDATE agent_runs" in sql
    assert "$10::jsonb" in sql
    # Param order MUST match the SQL: id, status, change_type, risk, skipped,
    # tool_calls, tokens_in, tokens_out, cost, per_model.
    assert params[0] == 42
    assert params[1] == "success"
    assert params[2] == "bugfix"
    assert params[3] == "medium"
    assert params[4] is False
    assert params[5] == 4
    assert params[6] == 1234
    assert params[7] == 567
    assert params[8] == Decimal("0.012345")
    assert '"claude-sonnet-4-6"' in params[9]


async def test_record_run_finished_handles_skipped_state(pool: _FakePool) -> None:
    await record_run_finished(
        pool,  # type: ignore[arg-type]
        run_id=42,
        status="skipped",
        triage_change_type="docs",
        triage_risk_level="low",
        skipped=True,
        tool_calls_used=0,
        tokens_input=12,
        tokens_output=6,
        cost_usd=Decimal("0.000054"),
        per_model={},
    )
    _, params = pool.conn.executes[0]
    assert params[1] == "skipped"
    assert params[4] is True


async def test_record_run_failed_truncates_long_error(pool: _FakePool) -> None:
    long_msg = "x" * 1000
    await record_run_failed(
        pool,  # type: ignore[arg-type]
        run_id=42,
        error=long_msg,
    )
    assert len(pool.conn.executes) == 1
    sql, params = pool.conn.executes[0]
    assert "UPDATE agent_runs" in sql
    assert "'failure'" in sql
    assert params[0] == 42
    assert params[1] == long_msg[:500]
    assert len(params[1]) == 500

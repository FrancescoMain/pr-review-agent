# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false
"""Unit tests for the runner's cost-cap abort path.

We don't go through ``make_default_runner`` here — building the full
graph requires Anthropic and a real GitHub App. Instead we test the
``_abort_for_cost_cap`` helper directly: given a ``CostCapExceeded``,
the runner must (a) post a clear abort comment via the GitHub client,
(b) record ``status='aborted_cost'`` on the run row with the partial
totals, (c) tolerate a missing or failing DB without raising.
"""

from decimal import Decimal
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from pr_review_agent.agent.cost_callback import CostTrackingCallback
from pr_review_agent.agent.exceptions import CostCapExceeded
from pr_review_agent.agent.models import ChangeType, RiskLevel, TriageDecision
from pr_review_agent.agent.runner import _abort_for_cost_cap
from pr_review_agent.agent.state import AgentState


class _RecordingClient:
    def __init__(self, *, raise_on_post: Exception | None = None) -> None:
        self.posted: list[dict[str, Any]] = []
        self._raise = raise_on_post

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        if self._raise is not None:
            raise self._raise
        self.posted.append(
            {
                "installation_id": installation_id,
                "repo": repo,
                "pr_number": pr_number,
                "body": body,
            }
        )


class _FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *params: Any) -> None:
        self.executes.append((sql, params))


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _state_with_triage() -> AgentState:
    return {
        "repo": "francesco/playground",
        "pr_number": 42,
        "installation_id": 99,
        "head_ref": "feat/x",
        "head_sha": "deadbeef" * 5,
        "triage": TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.medium),
        "tool_calls_used": 4,
    }


def _seed_callback_with_some_cost() -> CostTrackingCallback:
    cb = CostTrackingCallback()
    msg = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        response_metadata={"model_name": "claude-sonnet-4-6"},
    )
    result = LLMResult(
        generations=[[ChatGeneration(message=msg)]],
        llm_output={"model_name": "claude-sonnet-4-6"},
    )
    cb.on_llm_end(result, run_id=uuid4())
    return cb


async def test_abort_posts_comment_and_records_aborted_cost() -> None:
    client = _RecordingClient()
    pool = _FakePool()
    cb = _seed_callback_with_some_cost()
    exc = CostCapExceeded(current_cost=Decimal("0.012345"), cap=Decimal("0.010000"))

    await _abort_for_cost_cap(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=pool,  # type: ignore[arg-type]
        run_id=42,
        cost_cb=cb,
    )

    # 1. Issue comment posted with the cost numbers.
    assert len(client.posted) == 1
    body = client.posted[0]["body"]
    assert "Review aborted" in body
    assert "0.012345" in body
    assert "0.010000" in body

    # 2. DB updated to status='aborted_cost' with partial totals + per_model.
    assert len(pool.conn.executes) == 1
    sql, params = pool.conn.executes[0]
    assert "UPDATE agent_runs" in sql
    assert params[0] == 42
    assert params[1] == "aborted_cost"
    # triage_change_type, triage_risk_level
    assert params[2] == "feature"
    assert params[3] == "medium"
    # skipped is False
    assert params[4] is False
    # tool_calls_used reflects the partial state
    assert params[5] == 4
    # tokens propagated from cost_cb totals
    assert params[6] == 1000
    assert params[7] == 500
    # per_model JSONB serialised as string with the sonnet entry
    assert "claude-sonnet-4-6" in params[9]


async def test_abort_does_not_raise_if_comment_post_fails() -> None:
    """If the abort comment can't be posted (rare, e.g. token revoked), still
    update the DB and let the runner return."""

    client = _RecordingClient(raise_on_post=RuntimeError("token revoked"))
    pool = _FakePool()
    cb = _seed_callback_with_some_cost()
    exc = CostCapExceeded(current_cost=Decimal("0.10"), cap=Decimal("0.05"))

    await _abort_for_cost_cap(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=pool,  # type: ignore[arg-type]
        run_id=42,
        cost_cb=cb,
    )

    assert client.posted == []
    # DB write still happened so the run is recorded as aborted_cost.
    assert len(pool.conn.executes) == 1
    assert pool.conn.executes[0][1][1] == "aborted_cost"


async def test_abort_tolerates_missing_db_pool() -> None:
    """Without a pool, only the comment is posted; no exception."""

    client = _RecordingClient()
    cb = _seed_callback_with_some_cost()
    exc = CostCapExceeded(current_cost=Decimal("0.10"), cap=Decimal("0.05"))

    await _abort_for_cost_cap(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=None,
        run_id=None,
        cost_cb=cb,
    )

    assert len(client.posted) == 1
    assert "Review aborted" in client.posted[0]["body"]


async def test_abort_message_contains_actionable_hint() -> None:
    """The abort message must tell the user what to do next."""

    client = _RecordingClient()
    cb = _seed_callback_with_some_cost()
    exc = CostCapExceeded(current_cost=Decimal("0.10"), cap=Decimal("0.05"))

    await _abort_for_cost_cap(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=None,
        run_id=None,
        cost_cb=cb,
    )

    body = client.posted[0]["body"]
    assert "COST_CAP_PER_PR_USD" in body

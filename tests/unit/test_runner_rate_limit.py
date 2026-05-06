# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false
"""Unit tests for the runner's rate-limit abort path.

Mirrors ``test_runner_cost_cap.py``: we test the
``_abort_for_rate_limit`` helper directly with stubs, not through
``make_default_runner`` (which would need the full LangChain stack).
The helper must (a) post a clear comment with the retry-after when
GitHub provided one, (b) record ``status='aborted_rate_limit'`` with
the partial cost totals, (c) tolerate missing pool / failing comment
post.
"""

from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from pr_review_agent.agent.cost_callback import CostTrackingCallback
from pr_review_agent.agent.models import ChangeType, RiskLevel, TriageDecision
from pr_review_agent.agent.runner import _abort_for_rate_limit
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.exceptions import GitHubRateLimitError


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


async def test_abort_posts_comment_with_retry_after_and_records_aborted_rate_limit() -> None:
    client = _RecordingClient()
    pool = _FakePool()
    cb = _seed_callback_with_some_cost()
    exc = GitHubRateLimitError("HTTP 403 with Retry-After=120", retry_after_seconds=120)

    await _abort_for_rate_limit(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=pool,  # type: ignore[arg-type]
        run_id=42,
        cost_cb=cb,
    )

    assert len(client.posted) == 1
    body = client.posted[0]["body"]
    assert "Review aborted" in body
    assert "rate limit" in body.lower()
    assert "120s" in body  # retry-after surfaced

    # DB write — UPDATE row 42 with status='aborted_rate_limit'.
    assert len(pool.conn.executes) == 1
    sql, params = pool.conn.executes[0]
    assert "UPDATE agent_runs" in sql
    assert params[0] == 42
    assert params[1] == "aborted_rate_limit"
    assert params[2] == "feature"
    assert params[3] == "medium"
    assert params[4] is False
    assert params[5] == 4  # tool_calls_used
    assert params[6] == 1000
    assert params[7] == 500


async def test_abort_message_omits_retry_blurb_when_no_retry_after() -> None:
    client = _RecordingClient()
    cb = _seed_callback_with_some_cost()
    exc = GitHubRateLimitError(
        "rate limit floor reached, reset is 600s away",
        retry_after_seconds=None,
    )

    await _abort_for_rate_limit(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=None,
        run_id=None,
        cost_cb=cb,
    )

    body = client.posted[0]["body"]
    assert "Review aborted" in body
    assert "rate limit" in body.lower()
    assert "Retry in" not in body  # no specific number to give


async def test_abort_does_not_raise_if_comment_post_fails() -> None:
    """If posting the abort comment trips the rate limit again, swallow it."""
    client = _RecordingClient(raise_on_post=GitHubRateLimitError("HTTP 403"))
    pool = _FakePool()
    cb = _seed_callback_with_some_cost()
    exc = GitHubRateLimitError("HTTP 403", retry_after_seconds=60)

    # Must not raise.
    await _abort_for_rate_limit(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=pool,  # type: ignore[arg-type]
        run_id=42,
        cost_cb=cb,
    )
    assert client.posted == []
    # DB row still written so the run is observable as aborted.
    assert pool.conn.executes[0][1][1] == "aborted_rate_limit"


async def test_abort_tolerates_missing_db_pool() -> None:
    client = _RecordingClient()
    cb = _seed_callback_with_some_cost()
    exc = GitHubRateLimitError("HTTP 429", retry_after_seconds=30)

    await _abort_for_rate_limit(
        github_client=client,  # type: ignore[arg-type]
        state=_state_with_triage(),
        exc=exc,
        db_pool=None,
        run_id=None,
        cost_cb=cb,
    )

    assert len(client.posted) == 1
    assert "rate limit" in client.posted[0]["body"].lower()

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Production runner — wires Anthropic + GitHub client into the graph.

The static parts of the graph (triage chain, publisher, the Sonnet
model used by the gatherer) are built once. Per-run we open a
``RepoCheckout`` for the head SHA, build the GitHub-side and
filesystem tools bound to that PR's context, wire them into the
gatherer, compose the graph, and invoke. The checkout's tmpdir is
cleaned up on the way out, success or failure.

Persistence (W2-Task7): if a Postgres pool is provided, the runner
inserts an ``agent_runs`` row at the start of each invocation,
attaches a ``CostTrackingCallback`` to the graph config, and updates
the row at the end with the final status, totals, and per-model
cost. When the pool is ``None`` the runner still attaches the cost
callback (so you can tail logs and see token counts) but skips DB
writes — useful for development without docker-compose up.

Tests don't go through here; they assemble the graph directly with
fake nodes. The pyright pragma at the top mutes partial-unknown
warnings on langchain/langgraph generics that we don't control; our
own contracts (``AgentRunner``, ``AgentState``) stay strict.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

from pr_review_agent.agent.cost_callback import CostTrackingCallback
from pr_review_agent.agent.exceptions import CostCapExceeded
from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.models import TriageDecision
from pr_review_agent.agent.nodes.context_gatherer import make_context_gatherer_node
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.nodes.reviewer import (
    make_default_review_chain_factory,
    make_reviewer_node,
)
from pr_review_agent.agent.nodes.triage import make_triage_node
from pr_review_agent.agent.state import AgentState
from pr_review_agent.agent.tools import (
    PRContext,
    RepoCheckout,
    make_filesystem_tools,
    make_github_tools,
)
from pr_review_agent.db import (
    record_run_failed,
    record_run_finished,
    record_run_started,
)
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient

_log = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_TRIAGE_MODEL = "claude-haiku-4-5-20251001"
_GATHERER_MODEL = "claude-sonnet-4-6"

AgentRunner = Callable[[AgentState], Awaitable[AgentState]]


def make_default_runner(
    *,
    anthropic_api_key: str,
    github_client: GitHubClient,
    github_auth: GitHubAppAuth,
    db_pool: asyncpg.Pool | None = None,  # type: ignore[type-arg]
    cost_cap_usd: Decimal | None = None,
) -> AgentRunner:
    triage_system_prompt = (_PROMPTS_DIR / "triage.md").read_text(encoding="utf-8")
    prompt: Any = ChatPromptTemplate.from_messages(
        [
            ("system", triage_system_prompt),
            ("human", "Title: {title}\n\nBody:\n{body}"),
        ]
    )
    triage_llm = ChatAnthropic(
        model_name=_TRIAGE_MODEL,
        api_key=anthropic_api_key,  # type: ignore[arg-type]
        timeout=30.0,
        max_retries=2,
        stop=None,
    )
    structured: Any = triage_llm.with_structured_output(TriageDecision)
    triage_chain: Any = prompt | structured
    triage = make_triage_node(triage_chain)

    publisher = make_publisher_node(github_client)

    review_chain_factory = make_default_review_chain_factory(
        anthropic_api_key=anthropic_api_key,
    )
    reviewer = make_reviewer_node(
        github_client=github_client,
        chain_factory=review_chain_factory,
    )

    gatherer_llm = ChatAnthropic(
        model_name=_GATHERER_MODEL,
        api_key=anthropic_api_key,  # type: ignore[arg-type]
        timeout=60.0,
        max_retries=2,
        stop=None,
    )

    async def run(state: AgentState) -> AgentState:
        ctx = PRContext(
            repo=state["repo"],
            pr_number=state["pr_number"],
            installation_id=state["installation_id"],
            head_ref=state["head_ref"],
            head_sha=state["head_sha"],
        )
        pr_body = state.get("pr_body") or ""
        cid = structlog.contextvars.get_contextvars().get("correlation_id")

        run_id: int | None = None
        if db_pool is not None and cid is not None:
            try:
                run_id = await record_run_started(
                    db_pool,
                    correlation_id=cid,
                    repo=state["repo"],
                    pr_number=state["pr_number"],
                    head_sha=state["head_sha"],
                )
            except Exception:
                _log.exception("could not insert agent_runs row; continuing without persistence")

        cost_cb = CostTrackingCallback(cost_cap_usd=cost_cap_usd)

        try:
            async with RepoCheckout(ctx=ctx, auth=github_auth) as checkout:
                github_tools = make_github_tools(
                    ctx=ctx,
                    client=github_client,
                    pr_body_provider=lambda: pr_body,
                )
                filesystem_tools = make_filesystem_tools(checkout.root)
                gatherer = make_context_gatherer_node(
                    model=gatherer_llm,
                    repo_tools=[*github_tools, *filesystem_tools],
                )
                graph: Any = build_graph(
                    triage=triage,
                    context_gatherer=gatherer,
                    reviewer=reviewer,
                    publisher=publisher,
                )
                config: dict[str, Any] = {"callbacks": [cost_cb]}
                if cid is not None:
                    config["metadata"] = {"correlation_id": cid}
                    config["tags"] = [f"correlation:{cid}"]
                result: Any = await graph.ainvoke(state, config=config)
        except CostCapExceeded as exc:
            await _abort_for_cost_cap(
                github_client=github_client,
                state=state,
                exc=exc,
                db_pool=db_pool,
                run_id=run_id,
                cost_cb=cost_cb,
            )
            return state
        except Exception as exc:
            if db_pool is not None and run_id is not None:
                try:
                    await record_run_failed(
                        db_pool,
                        run_id=run_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    _log.exception("could not write failure row to agent_runs")
            raise

        final_state = AgentState(**result)
        if db_pool is not None and run_id is not None:
            try:
                await _persist_finished(db_pool, run_id, final_state, cost_cb)
            except Exception:
                _log.exception("could not write finished row to agent_runs")
        return final_state

    return run


async def _abort_for_cost_cap(
    *,
    github_client: GitHubClient,
    state: AgentState,
    exc: CostCapExceeded,
    db_pool: asyncpg.Pool | None,  # type: ignore[type-arg]
    run_id: int | None,
    cost_cb: CostTrackingCallback,
) -> None:
    """Graceful abort: post a brief comment and record status='aborted_cost'.

    Does not re-raise: from the runner's perspective, the abort is a
    successful termination of the run — the agent decided to stop.
    """
    body = (
        f"🛑 Review aborted: cost cap reached at ${exc.current_cost:.6f} "
        f"(cap: ${exc.cap:.6f}). "
        "Re-run after raising COST_CAP_PER_PR_USD or splitting the PR."
    )
    try:
        await github_client.post_pr_comment(
            installation_id=state["installation_id"],
            repo=state["repo"],
            pr_number=state["pr_number"],
            body=body,
        )
    except Exception:
        _log.exception("could not post cost-cap abort comment")

    _log.warning(
        "cost_cap.exceeded",
        repo=state["repo"],
        pr_number=state["pr_number"],
        current_cost=str(exc.current_cost),
        cap=str(exc.cap),
    )

    if db_pool is None or run_id is None:
        return
    triage = state.get("triage")
    totals = cost_cb.totals()
    try:
        await record_run_finished(
            db_pool,
            run_id=run_id,
            status="aborted_cost",
            triage_change_type=triage.change_type.value if triage is not None else None,
            triage_risk_level=triage.risk_level.value if triage is not None else None,
            skipped=False,
            tool_calls_used=int(state.get("tool_calls_used") or 0),
            tokens_input=int(totals["tokens_input"]),
            tokens_output=int(totals["tokens_output"]),
            cost_usd=totals["cost_usd"],
            per_model=totals["per_model"],
        )
    except Exception:
        _log.exception("could not write aborted_cost row to agent_runs")


async def _persist_finished(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    run_id: int,
    state: AgentState,
    cost_cb: CostTrackingCallback,
) -> None:
    triage = state.get("triage")
    skipped = bool(triage is not None and triage.should_skip)
    status = "skipped" if skipped else "success"
    totals = cost_cb.totals()
    await record_run_finished(
        pool,
        run_id=run_id,
        status=status,
        triage_change_type=triage.change_type.value if triage is not None else None,
        triage_risk_level=triage.risk_level.value if triage is not None else None,
        skipped=skipped,
        tool_calls_used=int(state.get("tool_calls_used") or 0),
        tokens_input=int(totals["tokens_input"]),
        tokens_output=int(totals["tokens_output"]),
        cost_usd=totals["cost_usd"],
        per_model=totals["per_model"],
    )

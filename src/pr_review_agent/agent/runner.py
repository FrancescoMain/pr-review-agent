# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Production runner — wires Anthropic + GitHub client into the graph.

The static parts of the graph (triage chain, publisher, the Sonnet
model used by the gatherer) are built once. Per-run we open a
``RepoCheckout`` for the head SHA, build the GitHub-side and
filesystem tools bound to that PR's context, wire them into the
gatherer, compose the graph, and invoke. The checkout's tmpdir is
cleaned up on the way out, success or failure.

Tests don't go through here; they assemble the graph directly with
fake nodes. The pyright pragma at the top mutes partial-unknown
warnings on langchain/langgraph generics that we don't control; our
own contracts (``AgentRunner``, ``AgentState``) stay strict.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

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
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_TRIAGE_MODEL = "claude-haiku-4-5-20251001"
_GATHERER_MODEL = "claude-sonnet-4-6"

AgentRunner = Callable[[AgentState], Awaitable[AgentState]]


def make_default_runner(
    *,
    anthropic_api_key: str,
    github_client: GitHubClient,
    github_auth: GitHubAppAuth,
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
            result: Any = await graph.ainvoke(state)
            return AgentState(**result)

    return run

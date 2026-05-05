# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Production runner — wires Anthropic + GitHub client into the graph.

Exposes ``make_default_runner`` which the FastAPI lifespan calls once
at startup, capturing the Anthropic API key and the GitHub client into
a closure. Tests don't go through here; they assemble the graph
directly with fake nodes. The pyright pragma at the top mutes
partial-unknown warnings on langchain/langgraph generics that we don't
control; our own contracts (``AgentRunner``, ``AgentState``) stay strict.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.models import TriageDecision
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.agent.nodes.triage import make_triage_node
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.client import GitHubClient

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_TRIAGE_MODEL = "claude-haiku-4-5-20251001"

AgentRunner = Callable[[AgentState], Awaitable[AgentState]]


def make_default_runner(*, anthropic_api_key: str, github_client: GitHubClient) -> AgentRunner:
    triage_system_prompt = (_PROMPTS_DIR / "triage.md").read_text(encoding="utf-8")
    # langchain/langgraph type hints are partially-unknown to pyright in strict mode;
    # we degrade these locally to Any to keep our own contracts strict.
    prompt: Any = ChatPromptTemplate.from_messages(  # pyright: ignore[reportUnknownMemberType]
        [
            ("system", triage_system_prompt),
            ("human", "Title: {title}\n\nBody:\n{body}"),
        ]
    )
    llm = ChatAnthropic(
        model_name=_TRIAGE_MODEL,
        api_key=anthropic_api_key,  # type: ignore[arg-type]
        timeout=30.0,
        max_retries=2,
        stop=None,
    )
    structured: Any = llm.with_structured_output(  # pyright: ignore[reportUnknownMemberType]
        TriageDecision
    )
    triage_chain: Any = prompt | structured

    triage = make_triage_node(triage_chain)
    publisher = make_publisher_node(github_client)
    graph: Any = build_graph(triage=triage, publisher=publisher)

    async def run(state: AgentState) -> AgentState:
        result: Any = await graph.ainvoke(state)
        return AgentState(**result)

    return run

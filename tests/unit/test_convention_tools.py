# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for ``make_convention_tools``.

The factory produces a single ``recall_conventions`` tool that closes
over a ``ConventionStore`` plus a ``repo`` and ``top_k``. The LLM
sees only ``query`` — the rest must not be tunable from the model
side. Tests use a fake store to avoid spinning up Qdrant.
"""

from typing import Any

import pytest
from langchain_core.tools import BaseTool

from pr_review_agent.agent.memory.store import ConventionMatch
from pr_review_agent.agent.tools import make_convention_tools


class _FakeStore:
    def __init__(self, *, results: list[ConventionMatch]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def query_conventions(
        self, *, repo: str, query: str, top_k: int = 5
    ) -> list[ConventionMatch]:
        self.calls.append({"repo": repo, "query": query, "top_k": top_k})
        return list(self._results)


def _find_tool(tools: list[BaseTool], name: str) -> BaseTool:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


@pytest.fixture
def matches() -> list[ConventionMatch]:
    return [
        ConventionMatch(path="CLAUDE.md", chunk_index=0, text="use uv", score=0.91),
        ConventionMatch(
            path="CLAUDE.md",
            chunk_index=1,
            text="commits in conventional form",
            score=0.83,
        ),
    ]


async def test_factory_exposes_single_tool(matches: list[ConventionMatch]) -> None:
    store = _FakeStore(results=matches)
    tools = make_convention_tools(store=store, repo="x/y", top_k=3)  # type: ignore[arg-type]
    assert [t.name for t in tools] == ["recall_conventions"]


async def test_tool_invokes_store_with_closured_repo_and_top_k(
    matches: list[ConventionMatch],
) -> None:
    store = _FakeStore(results=matches)
    tools = make_convention_tools(store=store, repo="francesco/playground", top_k=4)  # type: ignore[arg-type]
    tool = _find_tool(tools, "recall_conventions")

    result = await tool.ainvoke({"query": "how do I commit?"})

    # Result preserves the store's matches.
    assert result == matches
    # The LLM only chose `query`; repo and top_k came from closure.
    assert store.calls == [
        {"repo": "francesco/playground", "query": "how do I commit?", "top_k": 4}
    ]


async def test_tool_returns_empty_list_when_store_returns_empty() -> None:
    """No matches from the store → empty list reaches the LLM untouched."""
    store = _FakeStore(results=[])
    tools = make_convention_tools(store=store, repo="x/y", top_k=5)  # type: ignore[arg-type]
    tool = _find_tool(tools, "recall_conventions")

    result = await tool.ainvoke({"query": "no idea"})
    assert result == []


async def test_tool_argument_schema_only_exposes_query(matches: list[ConventionMatch]) -> None:
    """The model's tool schema must NOT advertise repo/top_k."""
    store = _FakeStore(results=matches)
    tools = make_convention_tools(store=store, repo="x/y", top_k=5)  # type: ignore[arg-type]
    tool = _find_tool(tools, "recall_conventions")

    schema_fields = set(tool.args_schema.model_fields.keys())  # type: ignore[union-attr]
    assert schema_fields == {"query"}

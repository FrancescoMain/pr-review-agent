# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUntypedFunctionDecorator=false
"""Unit tests for the Context Gatherer node.

We script a fake ``BaseChatModel`` that emits a deterministic sequence
of ``AIMessage`` with the right ``tool_calls`` payload to drive the
sub-graph through every branch we care about: happy path with a couple
of tool calls before ``final_answer``; recovery from a ``ToolPathError``
that the model can retry; the hard cap on tool calls; an
``AIMessage`` with no tool calls ending the loop early; and the abort
path on a fatal infrastructure error.
"""

from collections.abc import Iterable, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool

from pr_review_agent.agent.models import ChangeType, GatheredContext, RiskLevel, TriageDecision
from pr_review_agent.agent.nodes.context_gatherer import make_context_gatherer_node
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.exceptions import GitHubAPIError, ToolPathError


class _ScriptedChatModel(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` whose ``bind_tools`` is a no-op.

    The real upstream raises ``NotImplementedError`` because tool binding
    is provider-specific. For our tests we don't need the schema sent to
    a model — the responses are scripted — so binding can be a passthrough.
    """

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        return self


def _ai(tool_calls: Iterable[dict[str, Any]]) -> BaseMessage:
    return AIMessage(content="", tool_calls=list(tool_calls))


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id}


def _state() -> AgentState:
    return {
        "repo": "francesco/playground",
        "pr_number": 7,
        "installation_id": 99,
        "head_ref": "feat/x",
        "head_sha": "0" * 40,
        "pr_title": "Add dark mode",
        "pr_body": "Implements toggle. Closes #1.",
        "triage": TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.medium),
    }


def _build_repo_tools(*, raise_on_diff: Exception | None = None) -> list[BaseTool]:
    """Build deterministic stand-ins for the 5 production tools."""

    @tool
    async def get_pr_diff() -> str:
        """Return the unified diff of the PR."""
        if raise_on_diff is not None:
            raise raise_on_diff
        return "diff --git a/x b/x"

    @tool
    async def get_linked_issues() -> list[dict[str, Any]]:
        """Return linked issues."""
        return [{"number": 1, "title": "old bug", "state": "open", "body": ""}]

    @tool
    async def read_file(path: str) -> str:
        """Read a file from the PR head."""
        if path == "missing.py":
            raise ToolPathError(f"file not found: {path!r}")
        return f"# contents of {path}\n"

    @tool
    async def list_directory(path: str) -> list[str]:
        """List a directory."""
        return ["a.py", "b.py"]

    @tool
    async def search_code(query: str, file_pattern: str | None = None) -> list[dict[str, Any]]:
        """Search code via git grep."""
        return [{"path": "x.py", "line": 1, "text": query}]

    return [get_pr_diff, get_linked_issues, read_file, list_directory, search_code]


# ---------------------------- happy path ----------------------------


async def test_gatherer_produces_gathered_context_via_final_answer() -> None:
    model = _ScriptedChatModel(
        responses=[
            _ai([_tc("get_pr_diff", {}, "c1")]),
            _ai([_tc("get_linked_issues", {}, "c2")]),
            _ai(
                [
                    _tc(
                        "final_answer",
                        {
                            "summary": "Adds a dark-mode toggle.",
                            "relevant_files": ["src/theme.ts"],
                            "notes": "",
                        },
                        "c3",
                    )
                ]
            ),
        ]
    )
    node = make_context_gatherer_node(
        model=model, repo_tools=_build_repo_tools(), system_prompt="test"
    )

    update = await node(_state())

    ctx = update["gathered_context"]
    assert isinstance(ctx, GatheredContext)
    assert ctx.summary == "Adds a dark-mode toggle."
    assert ctx.relevant_files == ["src/theme.ts"]
    assert update["tool_calls_used"] == 3


async def test_gatherer_recovers_from_tool_path_error() -> None:
    """A ToolPathError is surfaced as a ToolMessage so the model can retry."""
    model = _ScriptedChatModel(
        responses=[
            _ai([_tc("read_file", {"path": "missing.py"}, "c1")]),
            _ai([_tc("read_file", {"path": "real.py"}, "c2")]),
            _ai(
                [
                    _tc(
                        "final_answer",
                        {"summary": "ok", "relevant_files": ["real.py"], "notes": ""},
                        "c3",
                    )
                ]
            ),
        ]
    )
    node = make_context_gatherer_node(
        model=model, repo_tools=_build_repo_tools(), system_prompt="test"
    )

    update = await node(_state())

    assert isinstance(update["gathered_context"], GatheredContext)
    assert update["tool_calls_used"] == 3
    # The error must have been recorded as a ToolMessage in the trace.
    contents = [getattr(m, "content", "") for m in update["gatherer_messages"]]
    assert any("file not found" in c for c in contents)


# ---------------------------- caps and exits ----------------------------


async def test_gatherer_force_ends_at_max_tool_calls() -> None:
    """If the model keeps calling tools past the cap, the loop terminates without context."""
    looping = [_ai([_tc("get_pr_diff", {}, f"c{i}")]) for i in range(20)]
    model = _ScriptedChatModel(responses=looping)
    node = make_context_gatherer_node(
        model=model, repo_tools=_build_repo_tools(), system_prompt="test", max_tool_calls=3
    )

    update = await node(_state())

    assert update["gathered_context"] is None
    assert update["tool_calls_used"] >= 3


async def test_gatherer_ends_when_model_emits_no_tool_calls() -> None:
    """A bare AIMessage without tool_calls means the model gave up; the loop ends."""
    bare: BaseMessage = AIMessage(content="I have nothing more to do.")
    model = _ScriptedChatModel(responses=[_ai([_tc("get_pr_diff", {}, "c1")]), bare])
    node = make_context_gatherer_node(
        model=model, repo_tools=_build_repo_tools(), system_prompt="test"
    )

    update = await node(_state())

    assert update["gathered_context"] is None
    assert update["tool_calls_used"] == 1


# ---------------------------- abort paths ----------------------------


async def test_gatherer_propagates_github_api_error() -> None:
    """Infra errors abort the run; the model is not allowed to retry."""
    model = _ScriptedChatModel(responses=[_ai([_tc("get_pr_diff", {}, "c1")])])
    node = make_context_gatherer_node(
        model=model,
        repo_tools=_build_repo_tools(raise_on_diff=GitHubAPIError("boom")),
        system_prompt="test",
    )

    with pytest.raises(GitHubAPIError):
        await node(_state())


async def test_gatherer_rejects_invalid_final_answer_and_keeps_looping() -> None:
    """If the model passes garbage to final_answer, we surface validation and let it retry."""
    model = _ScriptedChatModel(
        responses=[
            _ai([_tc("final_answer", {"summary": 123, "relevant_files": "not a list"}, "c1")]),
            _ai(
                [
                    _tc(
                        "final_answer",
                        {"summary": "fixed", "relevant_files": [], "notes": ""},
                        "c2",
                    )
                ]
            ),
        ]
    )
    node = make_context_gatherer_node(
        model=model, repo_tools=_build_repo_tools(), system_prompt="test"
    )

    update = await node(_state())

    ctx = update["gathered_context"]
    assert isinstance(ctx, GatheredContext)
    assert ctx.summary == "fixed"
    contents = [getattr(m, "content", "") for m in update["gatherer_messages"]]
    assert any("final_answer rejected" in c for c in contents)


async def test_gatherer_handles_unknown_tool_gracefully() -> None:
    """A model that hallucinates a tool name gets a ToolMessage and can recover."""
    model = _ScriptedChatModel(
        responses=[
            _ai([_tc("definitely_not_a_tool", {}, "c1")]),
            _ai(
                [
                    _tc(
                        "final_answer",
                        {"summary": "skipped", "relevant_files": [], "notes": ""},
                        "c2",
                    )
                ]
            ),
        ]
    )
    node = make_context_gatherer_node(
        model=model, repo_tools=_build_repo_tools(), system_prompt="test"
    )

    update = await node(_state())

    assert isinstance(update["gathered_context"], GatheredContext)
    contents = [getattr(m, "content", "") for m in update["gatherer_messages"]]
    assert any("unknown tool" in c for c in contents)


# ---------------------------- recall_conventions integration ----------------------------


async def test_gatherer_can_call_recall_conventions_and_see_matches() -> None:
    """The recall_conventions tool roundtrips: the LLM sees the matches as a JSON tool message.

    We add a fake recall tool to the gatherer's repo_tools and script
    the model to call it, then close with final_answer. The point is
    that the JSON the gatherer hands back to the model contains the
    matches' text — i.e. the Reviewer downstream will have access to
    project-specific guidance via the gatherer's transcript.
    """
    from pr_review_agent.agent.memory.store import ConventionMatch

    @tool
    async def recall_conventions(query: str) -> list[ConventionMatch]:
        """Fake convention recall."""
        del query
        return [
            ConventionMatch(
                path="CLAUDE.md",
                chunk_index=0,
                text="use `uv` for dependencies",
                score=0.92,
            ),
        ]

    model = _ScriptedChatModel(
        responses=[
            _ai([_tc("recall_conventions", {"query": "deps?"}, "c1")]),
            _ai(
                [
                    _tc(
                        "final_answer",
                        {
                            "summary": "respects uv convention",
                            "relevant_files": ["CLAUDE.md"],
                            "notes": "",
                        },
                        "c2",
                    )
                ]
            ),
        ]
    )
    repo_tools = [*_build_repo_tools(), recall_conventions]
    node = make_context_gatherer_node(model=model, repo_tools=repo_tools, system_prompt="test")

    update = await node(_state())

    assert isinstance(update["gathered_context"], GatheredContext)
    contents = [getattr(m, "content", "") for m in update["gatherer_messages"]]
    # The recall tool message must surface the chunk text to the model.
    assert any("use `uv` for dependencies" in c for c in contents)

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUntypedFunctionDecorator=false, reportArgumentType=false, reportUnknownArgumentType=false, reportMissingTypeArgument=false
"""Context Gatherer node — pre-Reviewer information collection.

The Gatherer is itself a small LangGraph **sub-graph** with two nodes
(model step + tool step) and a conditional edge. We hide the loop
behind ``make_context_gatherer_node`` so the outer graph sees the
Gatherer as a single function with ``AgentState → partial AgentState``
shape. SPEC.md §3 describes this node as the one that uses ``ToolNode``
and is allowed up to 15 tool calls; we implement a custom tool step
because we need to (a) intercept the ``final_answer`` tool to pull the
``GatheredContext`` out of the model's args, (b) translate
``ToolPathError`` into a ``ToolMessage`` so the model can recover, and
(c) fail fast on ``GitHubAPIError`` / ``RepoCloneError`` /
``GitHubAuthError`` (those mean the run is doomed, retrying won't
help).

The Gatherer never does its own LLM bookkeeping for cost — token
counts arrive in W2-Task7 via callbacks. The hard cap on tool calls is
the only guardrail today.
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from pr_review_agent.agent.models import GatheredContext
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.exceptions import (
    GitHubAPIError,
    GitHubAuthError,
    RepoCloneError,
)

DEFAULT_MAX_TOOL_CALLS = 15
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
_FINAL_ANSWER_TOOL_NAME = "final_answer"


class _GathererState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_calls_used: int
    gathered_context: GatheredContext | None


GathererNode = Callable[[AgentState], Awaitable[dict[str, Any]]]


def _build_final_answer_tool() -> BaseTool:
    @tool
    async def final_answer(summary: str, relevant_files: list[str], notes: str = "") -> str:
        """Call when you have gathered enough context.

        - summary: 2-4 sentences explaining what the PR does.
        - relevant_files: repo-relative paths the Reviewer should focus on.
        - notes: caveats or open questions; empty string is fine.
        """
        return "context recorded"

    return final_answer


def make_context_gatherer_node(
    *,
    model: BaseChatModel,
    repo_tools: Sequence[BaseTool],
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    system_prompt: str | None = None,
) -> GathererNode:
    """Build the Context Gatherer node.

    ``model`` is a ``BaseChatModel`` (typically ``ChatAnthropic`` with
    Sonnet) — already constructed, NOT yet bound to tools. ``repo_tools``
    are the 5 production tools (``get_pr_diff``, ``get_linked_issues``,
    ``read_file``, ``list_directory``, ``search_code``). The node binds
    them together with a synthetic ``final_answer`` tool that closes
    the gathering loop. ``system_prompt`` is loaded from
    ``prompts/context_gatherer.md`` if not provided — kept overridable
    so tests can shrink it.
    """
    final_answer = _build_final_answer_tool()
    all_tools: list[BaseTool] = [*repo_tools, final_answer]
    bound_model = model.bind_tools(all_tools)
    tool_lookup: dict[str, BaseTool] = {t.name: t for t in repo_tools}

    if system_prompt is None:
        system_prompt = (_PROMPTS_DIR / "context_gatherer.md").read_text(encoding="utf-8")

    async def model_step(state: _GathererState) -> dict[str, Any]:
        response: Any = await bound_model.ainvoke(state["messages"])
        return {"messages": [response]}

    async def tool_step(state: _GathererState) -> dict[str, Any]:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        out_messages: list[BaseMessage] = []
        new_context: GatheredContext | None = None
        calls_inc = 0

        for call in last.tool_calls:
            name: str = call["name"]
            args: dict[str, Any] = call["args"]
            tool_call_id: str = call["id"] or ""

            calls_inc += 1

            if name == _FINAL_ANSWER_TOOL_NAME:
                try:
                    new_context = GatheredContext(**args)
                    content = "context recorded"
                except ValidationError as exc:
                    content = f"final_answer rejected: {exc.errors(include_url=False)}"
                out_messages.append(ToolMessage(content=content, tool_call_id=tool_call_id))
                continue

            tool_obj = tool_lookup.get(name)
            if tool_obj is None:
                out_messages.append(
                    ToolMessage(
                        content=f"unknown tool: {name!r}",
                        tool_call_id=tool_call_id,
                    )
                )
                continue

            try:
                raw = await tool_obj.ainvoke(args)
            except (GitHubAPIError, GitHubAuthError, RepoCloneError):
                # Hard infra failure — abort the run, do not let the model retry.
                raise
            except Exception as exc:  # surface tool-contract errors to model as ToolMessage
                out_messages.append(ToolMessage(content=f"error: {exc}", tool_call_id=tool_call_id))
                continue

            out_messages.append(
                ToolMessage(content=_stringify_tool_result(raw), tool_call_id=tool_call_id)
            )

        return {
            "messages": out_messages,
            "tool_calls_used": state["tool_calls_used"] + calls_inc,
            **({"gathered_context": new_context} if new_context is not None else {}),
        }

    def should_continue(state: _GathererState) -> str:
        if state.get("gathered_context") is not None:
            return END
        if state["tool_calls_used"] >= max_tool_calls:
            return END
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and not last.tool_calls:
            return END
        return "tool_step"

    graph: StateGraph = StateGraph(_GathererState)
    graph.add_node("model_step", model_step)
    graph.add_node("tool_step", tool_step)
    graph.add_edge(START, "model_step")
    graph.add_conditional_edges(
        "model_step",
        should_continue,
        {END: END, "tool_step": "tool_step"},
    )
    graph.add_edge("tool_step", "model_step")
    compiled: Any = graph.compile()

    async def gatherer_node(state: AgentState) -> dict[str, Any]:
        title = state.get("pr_title", "")
        body = state.get("pr_body") or "(no body)"
        triage = state.get("triage")
        if triage is not None:
            triage_blurb = (
                f"Triage classified the PR as {triage.change_type.value} "
                f"({triage.risk_level.value} risk)."
            )
        else:
            triage_blurb = "No triage decision available."
        initial_messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    f"PR #{state['pr_number']} on {state['repo']}.\n"
                    f"Title: {title}\n\n"
                    f"Body:\n{body}\n\n"
                    f"{triage_blurb}\n\n"
                    "Begin by calling get_pr_diff()."
                )
            ),
        ]
        sub_state: _GathererState = {
            "messages": initial_messages,
            "tool_calls_used": 0,
            "gathered_context": None,
        }
        result: Any = await compiled.ainvoke(sub_state)

        return {
            "gathered_context": result.get("gathered_context"),
            "gatherer_messages": list(result["messages"]),
            "tool_calls_used": result.get("tool_calls_used", 0),
        }

    return gatherer_node


def _stringify_tool_result(raw: Any) -> str:
    """Convert a tool's return value into a string the model can read.

    Pydantic models go through ``.model_dump()``; lists of pydantic
    models become a JSON list; primitives and strings come back as-is.
    Falls back to ``repr`` on anything exotic so we never blow up here.
    """
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "model_dump"):
        return json.dumps(raw.model_dump(), default=str)  # type: ignore[no-any-return]
    if isinstance(raw, list) and raw and hasattr(raw[0], "model_dump"):
        return json.dumps([item.model_dump() for item in raw], default=str)
    try:
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return repr(raw)

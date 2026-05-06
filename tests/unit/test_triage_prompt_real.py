"""Regression test: triage chain with the real ``ChatPromptTemplate``.

The Task-5 unit tests for the triage node use a ``RunnableLambda`` as
fake chain, which bypasses ``ChatPromptTemplate`` entirely. That's why
the curly-brace bug (commit 507074d) slipped through pytest and was
only caught on a real PR. This test wires the *actual* prompt file
loaded the same way the production runner does, pipes it to a fake
chat model, and verifies the chain invokes cleanly with just the
``title`` / ``body`` keys the node passes. Any future re-introduction
of an unescaped ``{...}`` placeholder inside ``triage.md`` will fail
this test before it reaches an LLM.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "pr_review_agent"
    / "agent"
    / "prompts"
    / "triage.md"
)


def _build_prompt() -> ChatPromptTemplate:
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    return ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", "Title: {title}\n\nBody:\n{body}"),
        ]
    )


def test_triage_prompt_only_has_title_and_body_variables() -> None:
    prompt = _build_prompt()
    assert set(prompt.input_variables) == {"title", "body"}, (
        "triage.md leaked an extra placeholder — likely an unescaped '{...}' "
        "in the prompt body. ChatPromptTemplate would then demand that key at invoke time."
    )


async def test_triage_chain_invokes_with_body_containing_braces() -> None:
    """Body with literal ``{`` / ``}`` must not be re-parsed as a template."""
    prompt = _build_prompt()
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    chain: Any = prompt | fake_llm

    result = await chain.ainvoke(
        {
            "title": "Add JSON example to README",
            "body": 'sample payload: {"foo": {"bar": 1}}',
        }
    )

    assert isinstance(result, AIMessage)


async def test_triage_chain_raises_on_missing_input_variables() -> None:
    """Sanity check: if the node forgot to pass title or body, the chain fails fast."""
    prompt = _build_prompt()
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    chain: Any = prompt | fake_llm
    with pytest.raises(KeyError):
        await chain.ainvoke({"title": "only title, no body"})

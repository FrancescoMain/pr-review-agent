"""Unit tests for the triage node.

We inject a fake chain so the test never reaches Anthropic; the chain
just yields a pre-baked ``TriageDecision``. What we verify is the
contract: the node reads title/body from state, calls the chain, and
returns the right partial-state update for LangGraph to merge.
"""

from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda

from pr_review_agent.agent.models import ChangeType, ReviewDepth, RiskLevel, TriageDecision
from pr_review_agent.agent.nodes.triage import make_triage_node
from pr_review_agent.agent.state import AgentState


def _fake_chain(decision: TriageDecision) -> RunnableLambda[dict[str, Any], TriageDecision]:
    return RunnableLambda(lambda _inputs: decision)


@pytest.fixture
def baseline_decision() -> TriageDecision:
    return TriageDecision(
        change_type=ChangeType.feature,
        risk_level=RiskLevel.medium,
        review_depth=ReviewDepth.standard,
        should_skip=False,
    )


def _base_state(**overrides: Any) -> AgentState:
    state: AgentState = {
        "repo": "x/y",
        "pr_number": 1,
        "installation_id": 1,
        "head_ref": "feat/x",
        "head_sha": "0" * 40,
        "pr_title": "Add dark mode",
        "pr_body": "Implements toggle",
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


async def test_triage_returns_structured_decision(baseline_decision: TriageDecision) -> None:
    node = make_triage_node(_fake_chain(baseline_decision))
    update = await node(_base_state())
    assert update["triage"] == baseline_decision


async def test_triage_handles_empty_body(baseline_decision: TriageDecision) -> None:
    captured: dict[str, Any] = {}

    async def _capture(inputs: dict[str, Any]) -> TriageDecision:
        captured.update(inputs)
        return baseline_decision

    node = make_triage_node(RunnableLambda(_capture))
    await node(_base_state(pr_body=""))
    assert captured["body"] == "(no body provided)"


async def test_triage_seeds_token_counter(baseline_decision: TriageDecision) -> None:
    node = make_triage_node(_fake_chain(baseline_decision))
    update = await node(_base_state())
    assert "triage" in update["tokens_used"]

"""LangChain callback that captures per-model token usage.

The Reviewer / Gatherer / Triage all eventually invoke a chat model
through LangChain. ``BaseCallbackHandler.on_llm_end`` fires once per
LLM invocation with an ``LLMResult`` whose ``llm_output`` and
``generations[*][*].generation_info``/``message.usage_metadata``
contain the token counts. We accumulate ``(input, output)`` per
model_id; ``totals()`` aggregates and computes USD cost via
``cost_table.compute_cost_usd``.

We avoid relying on ``llm_output["token_usage"]`` alone: for
``with_structured_output`` (used by triage and reviewer) the token
counts live on each ``AIMessage.usage_metadata`` instead. We probe
both locations and prefer ``usage_metadata`` when present.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportAttributeAccessIssue=false

from decimal import Decimal
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

from pr_review_agent.agent.cost_table import compute_cost_usd


class CostTrackingCallback(BaseCallbackHandler):
    """Accumulate token usage per model across an entire graph run.

    Threadsafety: we don't share callbacks across runs in production —
    the runner builds a fresh callback per invocation — so the state
    here is intentionally simple (no locks).
    """

    def __init__(self) -> None:
        # {model_id: {"input": int, "output": int}}
        self._per_model: dict[str, dict[str, int]] = {}

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        model_id = self._extract_model_id(response)
        in_tokens, out_tokens = self._extract_tokens(response)
        if in_tokens == 0 and out_tokens == 0:
            return None
        bucket = self._per_model.setdefault(model_id or "unknown", {"input": 0, "output": 0})
        bucket["input"] += in_tokens
        bucket["output"] += out_tokens
        return None

    def totals(self) -> dict[str, Any]:
        """Aggregate counts and compute per-model and overall cost.

        Shape:
        ``{
            "tokens_input": int,
            "tokens_output": int,
            "cost_usd": Decimal,
            "per_model": {model_id: {"input": int, "output": int, "cost_usd": str}}
        }``

        ``cost_usd`` for ``per_model`` is a string so it round-trips
        through JSONB without losing decimals.
        """
        tokens_input = 0
        tokens_output = 0
        per_model_out: dict[str, dict[str, Any]] = {}
        total_cost = Decimal("0.000000")
        for model_id, counts in self._per_model.items():
            cost = compute_cost_usd(
                model_id,
                input_tokens=counts["input"],
                output_tokens=counts["output"],
            )
            tokens_input += counts["input"]
            tokens_output += counts["output"]
            total_cost += cost
            per_model_out[model_id] = {
                "input": counts["input"],
                "output": counts["output"],
                "cost_usd": str(cost),
            }
        return {
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "cost_usd": total_cost.quantize(Decimal("0.000001")),
            "per_model": per_model_out,
        }

    @staticmethod
    def _extract_model_id(response: LLMResult) -> str | None:
        if response.llm_output is not None:
            model = response.llm_output.get("model_name") or response.llm_output.get("model")
            if isinstance(model, str):
                return model
        # Fall back to per-generation metadata.
        for gen_list in response.generations:
            for gen in gen_list:
                if hasattr(gen, "message") and isinstance(gen.message, AIMessage):
                    md = gen.message.response_metadata or {}
                    model = md.get("model_name") or md.get("model")
                    if isinstance(model, str):
                        return model
        return None

    @staticmethod
    def _extract_tokens(response: LLMResult) -> tuple[int, int]:
        # Preferred path: AIMessage.usage_metadata (LangChain >=0.2 norm).
        in_total = 0
        out_total = 0
        found_any = False
        for gen_list in response.generations:
            for gen in gen_list:
                if hasattr(gen, "message") and isinstance(gen.message, AIMessage):
                    usage = gen.message.usage_metadata or {}
                    if usage:
                        in_total += int(usage.get("input_tokens", 0) or 0)
                        out_total += int(usage.get("output_tokens", 0) or 0)
                        found_any = True
        if found_any:
            return in_total, out_total
        # Legacy fallback: llm_output["token_usage"].
        if response.llm_output is not None:
            usage = response.llm_output.get("token_usage", {})
            if isinstance(usage, dict):
                return (
                    int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
                    int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
                )
        return 0, 0

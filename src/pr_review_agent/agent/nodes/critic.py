# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportArgumentType=false
"""Critic node — QA pass on the Reviewer's draft.

Two layers run for every Critic invocation:

1. **Deterministic line-number validation** — for every inline comment
   in the draft, we check that ``(path, line)`` is actually a
   right-side line in the diff. Mismatches go straight into
   ``should_drop_inline`` even if the LLM wouldn't have caught them.
2. **LLM judgement** — Haiku (fast and cheap; same model as triage)
   reviews tone, severity calibration, and scope; produces concerns
   and may add to ``should_drop_inline`` or propose a rewrite of the
   overall comment.

The two layers are merged: the LLM's verdict wins unless the
deterministic layer dropped more than half of the inline list, in
which case the Critic forces ``revise``.

The chain factory pattern mirrors the Reviewer: production wires
``make_default_critic_chain_factory`` (Haiku); tests inject a
``RunnableLambda`` so we don't need an Anthropic key.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from pr_review_agent.agent.models import (
    CriticVerdict,
    InlineComment,
    VerdictKind,
)
from pr_review_agent.agent.state import AgentState
from pr_review_agent.github.diff_parser import parse_post_lines

DEFAULT_CRITIC_MODEL = "claude-haiku-4-5-20251001"
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

CriticNode = Callable[[AgentState], Awaitable[dict[str, Any]]]
CriticChainFactory = Callable[[], Runnable[dict[str, Any], CriticVerdict]]


def _hallucinated_anchors(
    inline: list[InlineComment], allowed: dict[str, set[int]]
) -> list[InlineComment]:
    """Return inline comments whose (path, line) is not on the right side of the diff."""
    return [c for c in inline if c.line not in allowed.get(c.path, set())]


def _merge_drop_lists(
    deterministic: list[InlineComment], llm_proposed: list[InlineComment]
) -> list[InlineComment]:
    """Union the two drop lists by (path, line, body) identity, preserving order."""
    seen: set[tuple[str, int, str]] = set()
    merged: list[InlineComment] = []
    for c in [*deterministic, *llm_proposed]:
        key = (c.path, c.line, c.body)
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    return merged


def make_critic_node(*, chain_factory: CriticChainFactory) -> CriticNode:
    """Build the Critic node.

    The chain factory takes no args (the model is fixed Haiku in
    production); tests pass a thunk that returns a ``RunnableLambda``
    with a pre-baked ``CriticVerdict``.
    """

    async def critic_node(state: AgentState) -> dict[str, Any]:
        review = state.get("review")
        if review is None:
            # Defensive: the graph shouldn't route to Critic without a review,
            # but if it does, there's nothing to QA — accept.
            return {
                "critic_verdict": CriticVerdict(verdict=VerdictKind.accept),
                "retry_count": int(state.get("retry_count") or 0),
            }

        # Deterministic pre-pass: drop any inline whose anchor isn't in the diff.
        raw_diff = state.get("raw_diff") or ""
        allowed = parse_post_lines(raw_diff) if raw_diff else {}
        deterministic_drops = _hallucinated_anchors(review.inline_comments, allowed)

        chain = chain_factory()
        triage = state.get("triage")
        gathered = state.get("gathered_context")
        gathered_block = (
            f"summary: {gathered.summary}\nrelevant_files: {', '.join(gathered.relevant_files)}"
            if gathered is not None
            else "(none)"
        )
        draft_block = (
            f"approval: {review.approval.value}\n"
            f"overall:\n{review.overall_comment}\n\n"
            "inline:\n"
            + "\n".join(
                f"- {c.severity.value} `{c.path}:{c.line}` — {c.body}"
                for c in review.inline_comments
            )
        )

        llm_verdict: CriticVerdict = await chain.ainvoke(
            {
                "repo": state["repo"],
                "pr_number": state["pr_number"],
                "title": state.get("pr_title", ""),
                "change_type": triage.change_type.value if triage is not None else "unknown",
                "risk_level": triage.risk_level.value if triage is not None else "unknown",
                "gathered": gathered_block,
                "diff": raw_diff,
                "draft": draft_block,
            }
        )

        merged_drops = _merge_drop_lists(deterministic_drops, llm_verdict.should_drop_inline)
        verdict = llm_verdict.verdict
        concerns = list(llm_verdict.concerns)

        # Hard rule: dropping more than half forces a revise regardless of the LLM's call.
        total_inline = len(review.inline_comments)
        if total_inline > 0 and len(merged_drops) * 2 > total_inline:
            verdict = VerdictKind.revise
            if not any("inline" in c for c in concerns):
                concerns.append(
                    f"More than half of the inline comments were dropped "
                    f"({len(merged_drops)} of {total_inline}); the draft needs to be redone."
                )

        # If the deterministic pass dropped anything but verdict was accept,
        # surface a concern so retries don't lose context.
        elif deterministic_drops and verdict == VerdictKind.accept:
            concerns.append(
                f"{len(deterministic_drops)} inline comment(s) had anchors not in the diff "
                "and were silently dropped before publish."
            )

        merged = CriticVerdict(
            verdict=verdict,
            concerns=concerns,
            should_drop_inline=merged_drops,
            revised_overall_comment=llm_verdict.revised_overall_comment,
        )
        return {
            "critic_verdict": merged,
            "retry_count": int(state.get("retry_count") or 0),
        }

    return critic_node


def make_default_critic_chain_factory(
    *,
    anthropic_api_key: str,
    model: str = DEFAULT_CRITIC_MODEL,
    system_prompt: str | None = None,
) -> CriticChainFactory:
    """Production chain factory: Haiku + structured output on ``CriticVerdict``."""
    if system_prompt is None:
        system_prompt = (_PROMPTS_DIR / "critic.md").read_text(encoding="utf-8")

    prompt: Any = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            (
                "human",
                "PR #{pr_number} on {repo}\nTitle: {title}\n\n"
                "Triage: change_type={change_type}, risk={risk_level}.\n\n"
                "Gathered context:\n{gathered}\n\n"
                "Reviewer draft:\n{draft}\n\n"
                "Unified diff:\n```diff\n{diff}\n```",
            ),
        ]
    )

    def factory() -> Runnable[dict[str, Any], CriticVerdict]:
        llm = ChatAnthropic(
            model_name=model,
            api_key=anthropic_api_key,  # type: ignore[arg-type]
            timeout=30.0,
            max_retries=2,
            stop=None,
        )
        structured: Any = llm.with_structured_output(CriticVerdict)
        return prompt | structured

    return factory

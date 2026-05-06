# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportArgumentType=false
"""LLM-as-judge for the eval harness.

Rule-based asserts catch the obvious cases. The judge fills the gap
where a human reviewer would say "yeah this matches the spirit of
what I'd want, even if the keyword didn't match exactly". A short
Haiku call reads ``expected.notes`` (the human description) plus the
actual review JSON and emits an integer score in [1, 5] with a
one-sentence justification.

Skippable via the CLI's ``--no-judge`` so the harness runs offline
and zero-cost in CI.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from pr_review_agent.agent.models import ReviewResult

DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class JudgeVerdict(BaseModel):
    score: int = Field(ge=1, le=5, description="1=useless, 5=excellent")
    rationale: str = Field(default="", description="One sentence justification")


JudgeChainFactory = Callable[[], Runnable[dict[str, Any], JudgeVerdict]]


_DEFAULT_PROMPT = """\
You are a senior code reviewer judging the work of an automated PR-review agent.

You will see:
- the PR's title + body
- a human-written description of what an attentive reviewer should catch ("expected notes")
- the agent's actual review (overall + inline comments + approval)

Score the agent's review on a 1-5 scale:
- 5 — caught everything important, matched the expected severity, no false positives
- 4 — caught the main issue, minor miss or one slightly inflated comment
- 3 — caught some but missed an obvious one, OR added 1-2 false positives
- 2 — missed something important AND has a false positive
- 1 — useless: missed everything important or hallucinated extensively

Output a structured JudgeVerdict with `score` (integer 1-5) and `rationale` (one sentence).
"""


def make_default_judge_chain_factory(
    *,
    anthropic_api_key: str,
    model: str = DEFAULT_JUDGE_MODEL,
    system_prompt: str = _DEFAULT_PROMPT,
) -> JudgeChainFactory:
    prompt: Any = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            (
                "human",
                "PR title: {title}\n\nPR body:\n{body}\n\n"
                "Expected notes:\n{notes}\n\n"
                "Agent's actual review (JSON):\n{actual_review}",
            ),
        ]
    )

    def factory() -> Runnable[dict[str, Any], JudgeVerdict]:
        llm = ChatAnthropic(
            model_name=model,
            api_key=anthropic_api_key,  # type: ignore[arg-type]
            timeout=30.0,
            max_retries=2,
            stop=None,
        )
        structured: Any = llm.with_structured_output(JudgeVerdict)
        return prompt | structured

    return factory


async def judge_review(
    *,
    chain_factory: JudgeChainFactory,
    title: str,
    body: str,
    notes: str,
    review: ReviewResult,
) -> JudgeVerdict:
    chain = chain_factory()
    return await chain.ainvoke(
        {
            "title": title,
            "body": body,
            "notes": notes,
            "actual_review": review.model_dump_json(indent=2),
        }
    )


# Pyright wants _PROMPTS_DIR used; keeps the symbol available if we move
# the prompt to a file in the future without breaking imports.
_ = _PROMPTS_DIR

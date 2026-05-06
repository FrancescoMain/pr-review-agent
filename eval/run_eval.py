# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""CLI entrypoint for the eval harness.

Loads ``eval/dataset.yaml``, runs each case through the real agent
pipeline (with a ``CapturingGitHubClient`` that swallows the publish
calls), runs the rule checks, optionally invokes the LLM judge, and
writes a markdown report under ``eval/reports/<isodate>/``.

Run as::

    uv run python -m eval.run_eval --filter pr-001 --no-judge

The harness relies on ``.env`` being set: at minimum
``ANTHROPIC_API_KEY``, ``GITHUB_APP_ID``,
``GITHUB_APP_PRIVATE_KEY_PATH``, and
``GITHUB_DEFAULT_INSTALLATION_ID`` (or per-case ``installation_id``
in the dataset).
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog

from eval.asserts import evaluate_case
from eval.dataset import EvalCase, load_dataset
from eval.judge import (
    JudgeChainFactory,
    JudgeVerdict,
    judge_review,
    make_default_judge_chain_factory,
)
from pr_review_agent.agent.models import ReviewResult, TriageDecision
from pr_review_agent.agent.runner import make_default_runner
from pr_review_agent.agent.state import AgentState
from pr_review_agent.config import get_settings
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient

_log = structlog.get_logger(__name__)


@dataclass
class CaseResult:
    case: EvalCase
    failures: list[str] = field(default_factory=list)
    review: ReviewResult | None = None
    triage: TriageDecision | None = None
    judge: JudgeVerdict | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and self.error is None


class _CapturingGitHubClient(GitHubClient):
    """A GitHubClient that records publish calls instead of issuing them.

    Read-side verbs (``get_pr_diff``, ``get_issue``) go through the
    real implementation so the agent sees real data. Publish-side
    verbs (``post_pr_comment``, ``post_pr_review``) are recorded.
    """

    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        http_client: httpx.AsyncClient,
        rate_limit_floor: int = 100,
        rate_limit_max_wait_seconds: int = 60,
    ) -> None:
        super().__init__(
            auth=auth,
            http_client=http_client,
            rate_limit_floor=rate_limit_floor,
            rate_limit_max_wait_seconds=rate_limit_max_wait_seconds,
        )
        self.posted_comments: list[dict[str, Any]] = []
        self.posted_reviews: list[dict[str, Any]] = []

    async def post_pr_comment(  # type: ignore[override]
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        self.posted_comments.append(
            {
                "installation_id": installation_id,
                "repo": repo,
                "pr_number": pr_number,
                "body": body,
            }
        )

    async def post_pr_review(  # type: ignore[override]
        self,
        *,
        installation_id: int,
        repo: str,
        pr_number: int,
        commit_id: str,
        body: str,
        event: str,
        comments: list[dict[str, Any]],
    ) -> None:
        self.posted_reviews.append(
            {
                "installation_id": installation_id,
                "repo": repo,
                "pr_number": pr_number,
                "commit_id": commit_id,
                "body": body,
                "event": event,
                "comments": comments,
            }
        )


def _resolve_installation_id(case: EvalCase, default: int | None) -> int:
    if case.installation_id is not None:
        return case.installation_id
    if default is None:
        raise ValueError(
            f"case {case.id!r} has no installation_id and GITHUB_DEFAULT_INSTALLATION_ID is not set"
        )
    return default


async def _run_one_case(
    case: EvalCase,
    *,
    judge_factory: JudgeChainFactory | None,
) -> CaseResult:
    settings = get_settings()
    started = time.perf_counter()
    try:
        installation_id = _resolve_installation_id(case, settings.github_default_installation_id)
    except ValueError as exc:
        return CaseResult(case=case, error=str(exc))

    if not settings.anthropic_api_key.get_secret_value():
        return CaseResult(case=case, error="ANTHROPIC_API_KEY not set")
    if (
        settings.github_app_id <= 0
        or settings.github_app_private_key_path is None
        or not settings.github_app_private_key_path.exists()
    ):
        return CaseResult(case=case, error="GitHub App credentials not configured")

    pem = settings.github_app_private_key_path.read_text(encoding="utf-8")

    async with httpx.AsyncClient(timeout=30.0) as http:
        auth = GitHubAppAuth(app_id=settings.github_app_id, private_key=pem, http_client=http)
        capturing_client = _CapturingGitHubClient(
            auth=auth,
            http_client=http,
            rate_limit_floor=settings.github_rate_limit_floor,
            rate_limit_max_wait_seconds=settings.github_rate_limit_max_wait_seconds,
        )
        runner = make_default_runner(
            anthropic_api_key=settings.anthropic_api_key.get_secret_value(),
            github_client=capturing_client,
            github_auth=auth,
            db_pool=None,  # eval harness intentionally skips persistence
            cost_cap_usd=Decimal(str(settings.cost_cap_per_pr_usd)),
            convention_store=None,
            convention_recall_top_k=settings.convention_recall_top_k,
        )

        state: AgentState = {
            "repo": case.repo,
            "pr_number": case.pr_number,
            "installation_id": installation_id,
            "head_ref": case.head_ref,
            "head_sha": case.head_sha,
            "pr_title": case.pr_title,
            "pr_body": case.pr_body,
            "tokens_used": {},
            "errors": [],
        }

        try:
            final_state = await runner(state)
        except Exception as exc:
            return CaseResult(
                case=case,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=time.perf_counter() - started,
            )

    review: ReviewResult | None = final_state.get("review")
    triage: TriageDecision | None = final_state.get("triage")
    failures = evaluate_case(expected=case.expected, review=review, triage=triage)

    judge_verdict: JudgeVerdict | None = None
    if judge_factory is not None and review is not None:
        try:
            judge_verdict = await judge_review(
                chain_factory=judge_factory,
                title=case.pr_title,
                body=case.pr_body,
                notes=case.notes,
                review=review,
            )
        except Exception as exc:
            _log.warning("judge_failed", case_id=case.id, error=str(exc))

    return CaseResult(
        case=case,
        failures=failures,
        review=review,
        triage=triage,
        judge=judge_verdict,
        elapsed_seconds=time.perf_counter() - started,
    )


def render_report(results: list[CaseResult]) -> str:
    """Render a markdown report from the per-case results."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    avg_runtime = sum(r.elapsed_seconds for r in results) / total if total else 0.0
    judge_scores = [r.judge.score for r in results if r.judge is not None]
    avg_judge = sum(judge_scores) / len(judge_scores) if judge_scores else None

    lines: list[str] = [
        "# Eval report",
        "",
        f"- Cases: **{total}**, passed: **{passed}**, failed: **{total - passed}**",
        f"- Avg runtime: **{avg_runtime:.1f}s**",
    ]
    if avg_judge is not None:
        lines.append(f"- Avg judge score: **{avg_judge:.2f} / 5** (n={len(judge_scores)})")
    lines.extend(
        [
            "",
            "| id | result | runtime | judge | summary |",
            "|---|---|---|---|---|",
        ]
    )
    for r in results:
        status = "✅" if r.passed else "❌"
        judge_cell = f"{r.judge.score}/5" if r.judge else "—"
        if r.error is not None:
            summary = f"error: {r.error}"
        elif r.failures:
            summary = f"{len(r.failures)} rule(s) failed"
        else:
            summary = "all rules passed"
        lines.append(
            f"| `{r.case.id}` | {status} | {r.elapsed_seconds:.1f}s | {judge_cell} | {summary} |"
        )

    lines.extend(["", "## Per-case detail", ""])
    for r in results:
        lines.append(f"### `{r.case.id}` — {r.case.repo}#{r.case.pr_number}")
        lines.append("")
        if r.error:
            lines.extend([f"**Error:** {r.error}", ""])
        if r.failures:
            lines.append("**Failures:**")
            lines.extend(f"- {f}" for f in r.failures)
            lines.append("")
        if r.judge is not None:
            lines.append(f"**Judge:** {r.judge.score}/5 — {r.judge.rationale or '(no rationale)'}")
            lines.append("")
        if r.review is not None:
            lines.append("**Review (truncated):**")
            lines.append("```json")
            review_dump = r.review.model_dump()
            # Truncate long bodies for the report.
            for c in review_dump.get("inline_comments", []):
                if isinstance(c.get("body"), str) and len(c["body"]) > 200:
                    c["body"] = c["body"][:200] + "…"
            if (
                isinstance(review_dump.get("overall_comment"), str)
                and len(review_dump["overall_comment"]) > 400
            ):
                review_dump["overall_comment"] = review_dump["overall_comment"][:400] + "…"
            lines.append(json.dumps(review_dump, indent=2))
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _select_cases(cases: Iterable[EvalCase], filter_id: str | None) -> list[EvalCase]:
    if filter_id is None:
        return list(cases)
    selected = [c for c in cases if c.id == filter_id]
    if not selected:
        raise SystemExit(f"no case matched --filter {filter_id!r}")
    return selected


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the PR-Review-Agent eval suite")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("eval/dataset.yaml"),
        help="Path to the YAML dataset (default: eval/dataset.yaml)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: eval/reports/<isodate>/)",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Run only the case with this id",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the LLM-as-judge step (saves cost)",
    )
    args = parser.parse_args(argv)

    cases = _select_cases(load_dataset(args.dataset), args.filter)
    settings = get_settings()
    judge_factory: JudgeChainFactory | None = None
    if not args.no_judge:
        api_key = settings.anthropic_api_key.get_secret_value()
        if not api_key:
            _log.warning("judge.disabled_no_anthropic_key")
        else:
            judge_factory = make_default_judge_chain_factory(anthropic_api_key=api_key)

    results: list[CaseResult] = []
    for case in cases:
        _log.info("eval.run_case", case_id=case.id)
        result = await _run_one_case(case, judge_factory=judge_factory)
        results.append(result)

    out_dir = args.out or Path("eval/reports") / time.strftime("%Y-%m-%dT%H-%M-%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.md"
    report_path.write_text(render_report(results), encoding="utf-8")
    _log.info("eval.report_written", path=str(report_path))

    failed = sum(1 for r in results if not r.passed)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover — entry point
    raise SystemExit(main())

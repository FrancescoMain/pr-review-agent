# pyright: reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""GitHub-side tools for the Context Gatherer.

W2-Task1 introduces the two read-side tools that don't need filesystem
access: ``get_pr_diff`` returns the unified diff, ``get_linked_issues``
parses ``Closes/Fixes/Resolves #N`` references from the PR body and
fetches each issue. They are bound to a specific PR via a
``PRContext`` captured in closure — the LLM never gets to choose which
PR/repo to read, only what to do with the data.

Tools are produced by ``make_github_tools(ctx, client)``: it returns a
list of ``BaseTool`` ready to hand to ``ChatAnthropic.bind_tools(...)``
or to a LangGraph ``ToolNode``. Async-first, errors stay as explicit
``GitHubAPIError`` / ``GitHubNotFoundError``; the latter is swallowed
inside ``get_linked_issues`` so a stale ``Closes #42`` reference does
not poison the whole gathering step.

The pyright pragma at the top mutes the partially-unknown types coming
out of ``@tool`` (a langchain decorator we don't control); our public
surface (``make_github_tools``) is fully typed.
"""

import re
from collections.abc import Callable

import structlog
from langchain_core.tools import BaseTool, tool

from pr_review_agent.agent.tools.models import LinkedIssue, PRContext
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.exceptions import GitHubNotFoundError

_log = structlog.get_logger(__name__)

# GitHub's own closing-keyword set, case-insensitive, same-repo references only.
# https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_LINK_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
    re.IGNORECASE,
)


def parse_linked_issue_numbers(body: str | None) -> list[int]:
    """Extract issue numbers from a PR body, deduped and ordered by first occurrence."""
    if not body:
        return []
    seen: dict[int, None] = {}
    for match in _LINK_RE.finditer(body):
        seen.setdefault(int(match.group(1)), None)
    return list(seen.keys())


def make_github_tools(
    *, ctx: PRContext, client: GitHubClient, pr_body_provider: Callable[[], str | None]
) -> list[BaseTool]:
    """Build the GitHub-side tools bound to a single PR.

    ``pr_body_provider`` is a callable so the body can come from agent
    state at the moment the tool runs (the body may be edited between
    webhook delivery and tool invocation, but for W2 we just close over
    the value seen at dispatch).
    """

    @tool
    async def get_pr_diff() -> str:
        """Return the unified diff of the pull request under review.

        The output is the raw diff text as served by GitHub
        (``application/vnd.github.diff``). Use it to reason about which
        files and lines changed.
        """
        return await client.get_pr_diff(
            installation_id=ctx.installation_id,
            repo=ctx.repo,
            pr_number=ctx.pr_number,
        )

    @tool
    async def get_linked_issues() -> list[LinkedIssue]:
        """Return the issues linked from the PR body via Closes/Fixes/Resolves.

        Issues that no longer exist (404) are skipped silently. If the
        body has no closing keywords the result is an empty list.
        """
        numbers = parse_linked_issue_numbers(pr_body_provider())
        results: list[LinkedIssue] = []
        for number in numbers:
            try:
                payload = await client.get_issue(
                    installation_id=ctx.installation_id,
                    repo=ctx.repo,
                    issue_number=number,
                )
            except GitHubNotFoundError:
                _log.info(
                    "linked_issue.skipped_404",
                    repo=ctx.repo,
                    issue_number=number,
                )
                continue
            results.append(_to_linked_issue(payload))
        return results

    return [get_pr_diff, get_linked_issues]


def _to_linked_issue(payload: dict[str, object]) -> LinkedIssue:
    number = payload.get("number")
    title = payload.get("title")
    state = payload.get("state")
    body = payload.get("body") or ""
    if not isinstance(number, int) or not isinstance(title, str) or not isinstance(state, str):
        raise ValueError(
            f"unexpected issue payload shape: number={type(number).__name__}, "
            f"title={type(title).__name__}, state={type(state).__name__}"
        )
    if not isinstance(body, str):
        body = ""
    return LinkedIssue(number=number, title=title, body=body, state=state)

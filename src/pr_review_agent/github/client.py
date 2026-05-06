"""Thin async client for the GitHub REST API.

The client owns no state of its own beyond an in-memory snapshot of
the latest ``X-RateLimit-*`` headers GitHub returned. Composes a
``GitHubAppAuth`` (which manages installation tokens) with an
externally-supplied ``httpx.AsyncClient`` so tests can mock it via
``respx`` and the FastAPI lifespan can share one connection pool.

Verbs grow alongside the agent: ``post_pr_comment`` (W1) for issue
comments; ``get_pr_diff`` and ``get_issue`` (W2-Task1) for the
gatherer; ``post_pr_review`` (W2-Task5) to publish a real PR review
with inline annotations.

W3-Task3 wires a reactive rate-limit guardrail. Every response is
read for ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``; before
each verb, if the most recent ``Remaining`` is below ``floor`` we
either sleep until the reset (when within ``max_wait_seconds``) or
raise ``GitHubRateLimitError`` so the runner can abort gracefully.
HTTP 403/429 with a ``Retry-After`` header surface the same exception
with the suggested wait propagated. We never auto-retry — the runner
posts an abort comment and the user re-runs after the cooldown.
"""

import asyncio
import contextlib
import time
from typing import Any, cast

import httpx
import structlog

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.exceptions import (
    GitHubAPIError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)

_GITHUB_API = "https://api.github.com"
_DEFAULT_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_log = structlog.get_logger(__name__)


class GitHubClient:
    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        http_client: httpx.AsyncClient,
        rate_limit_floor: int = 100,
        rate_limit_max_wait_seconds: int = 60,
    ) -> None:
        self._auth = auth
        self._http = http_client
        self._floor = rate_limit_floor
        self._max_wait = rate_limit_max_wait_seconds
        self._rate_remaining: int | None = None
        self._rate_reset_epoch: int | None = None

    async def _auth_headers(self, installation_id: int) -> dict[str, str]:
        token = await self._auth.get_installation_token(installation_id)
        return {**_DEFAULT_HEADERS, "Authorization": f"token {token.token}"}

    # ---------------------------- rate-limit guardrail ----------------------------

    def _record_rate_limit(self, response: httpx.Response) -> None:
        """Snapshot the rate-limit state from a response's headers."""
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None:
            with contextlib.suppress(ValueError):
                self._rate_remaining = int(remaining)
        if reset is not None:
            with contextlib.suppress(ValueError):
                self._rate_reset_epoch = int(reset)

    async def _maybe_wait_for_reset(self) -> None:
        """Pre-flight check: sleep or raise if we're under the floor."""
        if self._rate_remaining is None or self._rate_reset_epoch is None:
            return
        if self._rate_remaining > self._floor:
            return
        wait = max(0, self._rate_reset_epoch - int(time.time()))
        if wait <= self._max_wait:
            _log.info(
                "rate_limit.sleeping_until_reset",
                remaining=self._rate_remaining,
                wait_seconds=wait + 1,
            )
            await asyncio.sleep(wait + 1)
            # Optimistic reset: assume we're back at full quota until next response confirms.
            self._rate_remaining = None
            self._rate_reset_epoch = None
            return
        raise GitHubRateLimitError(
            f"rate limit floor reached ({self._rate_remaining} <= {self._floor}); "
            f"reset is {wait}s away (> {self._max_wait}s max wait)",
            retry_after_seconds=wait,
        )

    @staticmethod
    def _check_for_rate_limit_error(response: httpx.Response) -> None:
        """Raise ``GitHubRateLimitError`` for 403/429 with a Retry-After header.

        Plain 403s without Retry-After (e.g. permission denied) fall
        through to the caller's regular 4xx handling.
        """
        if response.status_code in (403, 429):
            retry_after = response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    seconds = int(retry_after)
                except ValueError:
                    seconds = None
                raise GitHubRateLimitError(
                    f"GitHub returned HTTP {response.status_code} with Retry-After={retry_after}",
                    retry_after_seconds=seconds,
                )

    # ---------------------------- verbs ----------------------------

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        await self._maybe_wait_for_reset()
        url = f"{_GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        headers = await self._auth_headers(installation_id)
        response = await self._http.post(url, headers=headers, json={"body": body})
        self._record_rate_limit(response)
        self._check_for_rate_limit_error(response)
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to post PR comment to {repo}#{pr_number}: HTTP {response.status_code}"
            )

    async def get_pr_diff(self, *, installation_id: int, repo: str, pr_number: int) -> str:
        await self._maybe_wait_for_reset()
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        headers = await self._auth_headers(installation_id)
        # Override Accept to ask GitHub for the unified diff representation.
        headers["Accept"] = "application/vnd.github.diff"
        response = await self._http.get(url, headers=headers)
        self._record_rate_limit(response)
        self._check_for_rate_limit_error(response)
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to fetch diff for {repo}#{pr_number}: HTTP {response.status_code}"
            )
        return response.text

    async def get_issue(
        self, *, installation_id: int, repo: str, issue_number: int
    ) -> dict[str, object]:
        await self._maybe_wait_for_reset()
        url = f"{_GITHUB_API}/repos/{repo}/issues/{issue_number}"
        headers = await self._auth_headers(installation_id)
        response = await self._http.get(url, headers=headers)
        self._record_rate_limit(response)
        self._check_for_rate_limit_error(response)
        if response.status_code == 404:
            raise GitHubNotFoundError(f"{repo}#{issue_number} not found")
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to fetch {repo}#{issue_number}: HTTP {response.status_code}"
            )
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError(
                f"unexpected payload shape for {repo}#{issue_number}: {type(payload).__name__}"
            )
        return cast(dict[str, object], payload)

    async def post_pr_review(
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
        """Publish a PR review with inline annotations.

        ``event`` is one of ``"COMMENT"`` / ``"APPROVE"`` / ``"REQUEST_CHANGES"``.
        ``comments`` items shape (see GitHub Reviews API):
            ``{"path": str, "line": int, "side": "RIGHT", "body": str}``.

        4xx (notably 422 when an inline anchor doesn't match the diff)
        surfaces as ``GitHubAPIError`` so the Publisher can fall back
        to a plain issue comment without retrying.
        """
        await self._maybe_wait_for_reset()
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        headers = await self._auth_headers(installation_id)
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
            "comments": comments,
        }
        response = await self._http.post(url, headers=headers, json=payload)
        self._record_rate_limit(response)
        self._check_for_rate_limit_error(response)
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to post review to {repo}#{pr_number}: HTTP {response.status_code}"
            )

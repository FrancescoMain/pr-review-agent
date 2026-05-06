"""Thin async client for the GitHub REST API.

The client owns no state of its own; it composes a ``GitHubAppAuth``
(which manages installation tokens) with an externally-supplied
``httpx.AsyncClient`` so tests can mock it via ``respx`` and the
FastAPI lifespan can share one connection pool.

Verbs grow alongside the agent. W1 added ``post_pr_comment`` for the
hello-world publisher. W2-Task1 adds two read-side verbs used by the
GitHub-side tools the Context Gatherer will call: ``get_pr_diff``
returns the raw unified diff, ``get_issue`` fetches a single issue by
number. Filesystem-style verbs (read_file, list_directory, search_code)
arrive in W2-Task2.
"""

from typing import cast

import httpx

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.exceptions import GitHubAPIError, GitHubNotFoundError

_GITHUB_API = "https://api.github.com"
_DEFAULT_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubClient:
    def __init__(self, *, auth: GitHubAppAuth, http_client: httpx.AsyncClient) -> None:
        self._auth = auth
        self._http = http_client

    async def _auth_headers(self, installation_id: int) -> dict[str, str]:
        token = await self._auth.get_installation_token(installation_id)
        return {**_DEFAULT_HEADERS, "Authorization": f"token {token.token}"}

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        url = f"{_GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        headers = await self._auth_headers(installation_id)
        response = await self._http.post(url, headers=headers, json={"body": body})
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to post PR comment to {repo}#{pr_number}: HTTP {response.status_code}"
            )

    async def get_pr_diff(self, *, installation_id: int, repo: str, pr_number: int) -> str:
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        headers = await self._auth_headers(installation_id)
        # Override Accept to ask GitHub for the unified diff representation.
        headers["Accept"] = "application/vnd.github.diff"
        response = await self._http.get(url, headers=headers)
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to fetch diff for {repo}#{pr_number}: HTTP {response.status_code}"
            )
        return response.text

    async def get_issue(
        self, *, installation_id: int, repo: str, issue_number: int
    ) -> dict[str, object]:
        url = f"{_GITHUB_API}/repos/{repo}/issues/{issue_number}"
        headers = await self._auth_headers(installation_id)
        response = await self._http.get(url, headers=headers)
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

"""Thin async client for the GitHub REST API.

For Task 5 the only operation we expose is ``post_pr_comment``: the
Publisher node uses it to drop the "Hello from agent" comment on the
target PR. The client owns no state of its own; it composes a
``GitHubAppAuth`` (which manages installation tokens) with an
externally-supplied ``httpx.AsyncClient`` (so tests can mock it via
``respx`` and so the FastAPI lifespan can share one connection pool).

More verbs (read file, list directory, search code) will arrive in
Week 2 alongside the Context Gatherer.
"""

import httpx

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.exceptions import GitHubAPIError

_GITHUB_API = "https://api.github.com"
_DEFAULT_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubClient:
    def __init__(self, *, auth: GitHubAppAuth, http_client: httpx.AsyncClient) -> None:
        self._auth = auth
        self._http = http_client

    async def post_pr_comment(
        self, *, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        token = await self._auth.get_installation_token(installation_id)
        url = f"{_GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        response = await self._http.post(
            url,
            headers={
                **_DEFAULT_HEADERS,
                "Authorization": f"token {token.token}",
            },
            json={"body": body},
        )
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"failed to post PR comment to {repo}#{pr_number}: HTTP {response.status_code}"
            )

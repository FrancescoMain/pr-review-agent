"""GitHub App authentication: JWT and installation tokens.

The flow is documented at
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app

The App signs a short-lived JWT (RS256, max 10 minutes per GitHub's rules)
with its private key. That JWT is exchanged for an *installation token* —
a short-lived bearer credential scoped to a single installation, valid for
roughly one hour.

This module owns:
  - ``build_jwt``: produce the App-level JWT;
  - ``GitHubAppAuth.get_installation_token``: read-through cache that
    returns a still-valid installation token, refreshing it via the
    GitHub API when it gets within five minutes of expiry. An
    ``asyncio.Lock`` per installation_id prevents thundering-herd
    refreshes when concurrent webhook deliveries hit at once.

The cache is intentionally in-memory: we run as a single process today
(see SPEC §5). When we move to multiple workers in W4 we will swap this
for a Postgres-backed store; the public surface here is designed so the
swap is local.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from pr_review_agent.github.exceptions import GitHubAPIError, GitHubAuthError

_JWT_TTL_SECONDS = 9 * 60  # under GitHub's 10-minute hard limit
_JWT_CLOCK_SKEW_SECONDS = 60  # iat backdate to absorb clock drift
_REFRESH_SAFETY_MARGIN = timedelta(minutes=5)
_INSTALLATIONS_TOKEN_URL = (
    "https://api.github.com/app/installations/{installation_id}/access_tokens"
)
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime  # UTC


class GitHubAppAuth:
    def __init__(self, *, app_id: int, private_key: str, http_client: httpx.AsyncClient) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._http = http_client
        self._cache: dict[int, InstallationToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def build_jwt(self) -> str:
        now = int(datetime.now(UTC).timestamp())
        payload = {
            "iat": now - _JWT_CLOCK_SKEW_SECONDS,
            "exp": now + _JWT_TTL_SECONDS,
            "iss": str(self._app_id),
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> InstallationToken:
        cached = self._cache.get(installation_id)
        if cached is not None and self._still_fresh(cached):
            return cached
        lock = self._locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(installation_id)
            if cached is not None and self._still_fresh(cached):
                return cached
            new_token = await self._exchange(installation_id)
            self._cache[installation_id] = new_token
            return new_token

    @staticmethod
    def _still_fresh(token: InstallationToken) -> bool:
        return token.expires_at - datetime.now(UTC) > _REFRESH_SAFETY_MARGIN

    async def _exchange(self, installation_id: int) -> InstallationToken:
        url = _INSTALLATIONS_TOKEN_URL.format(installation_id=installation_id)
        headers = {
            **_GITHUB_HEADERS,
            "Authorization": f"Bearer {self.build_jwt()}",
        }
        response = await self._http.post(url, headers=headers)
        if response.status_code in (401, 403):
            raise GitHubAuthError(
                f"GitHub rejected JWT exchange for installation {installation_id}: "
                f"HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"GitHub token exchange failed for installation {installation_id}: "
                f"HTTP {response.status_code}"
            )
        data = response.json()
        return InstallationToken(
            token=data["token"],
            expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")),
        )

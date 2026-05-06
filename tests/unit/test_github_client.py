# pyright: reportPrivateUsage=false
"""Unit tests for ``GitHubClient.post_pr_comment``.

Mocks both the installation-token exchange and the comments endpoint
with ``respx``, so we cover the contract end-to-end (auth header
includes the installation token, body is JSON-encoded, 4xx/5xx surface
as ``GitHubAPIError``).
"""

import json
import time as _time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.exceptions import GitHubAPIError, GitHubRateLimitError

APP_ID = 1
INSTALLATION_ID = 99
REPO = "francesco/playground"
PR_NUMBER = 42


@pytest.fixture(scope="module")
def private_pem() -> str:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def respx_mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def _token_response() -> httpx.Response:
    expires = datetime.now(UTC) + timedelta(seconds=3600)
    return httpx.Response(
        201,
        json={
            "token": "ghs_installation",
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        },
    )


def _build_client(http: httpx.AsyncClient, pem: str) -> GitHubClient:
    auth = GitHubAppAuth(app_id=APP_ID, private_key=pem, http_client=http)
    return GitHubClient(auth=auth, http_client=http)


async def test_post_pr_comment_uses_installation_token(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    comments = respx_mock.post(
        f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    ).mock(return_value=httpx.Response(201, json={"id": 1, "body": "hi"}))

    client = _build_client(http_client, private_pem)
    await client.post_pr_comment(
        installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
    )

    request = comments.calls.last.request
    assert request.headers["Authorization"] == "token ghs_installation"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert b'"body":"hi"' in request.content


async def test_post_pr_comment_raises_on_5xx(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.post(f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments").mock(
        return_value=httpx.Response(503, json={"message": "Service Unavailable"})
    )

    client = _build_client(http_client, private_pem)
    with pytest.raises(GitHubAPIError):
        await client.post_pr_comment(
            installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
        )


async def test_post_pr_review_sends_reviews_payload(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    reviews = respx_mock.post(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/reviews"
    ).mock(return_value=httpx.Response(200, json={"id": 12345}))

    client = _build_client(http_client, private_pem)
    await client.post_pr_review(
        installation_id=INSTALLATION_ID,
        repo=REPO,
        pr_number=PR_NUMBER,
        commit_id="abc",
        body="overall body",
        event="COMMENT",
        comments=[{"path": "x.py", "line": 2, "side": "RIGHT", "body": "nit"}],
    )

    request = reviews.calls.last.request
    payload = json.loads(request.content)
    assert payload["commit_id"] == "abc"
    assert payload["event"] == "COMMENT"
    assert payload["body"] == "overall body"
    assert payload["comments"] == [{"path": "x.py", "line": 2, "side": "RIGHT", "body": "nit"}]
    assert request.headers["Authorization"] == "token ghs_installation"


async def test_post_pr_review_raises_on_422(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """422 (e.g. inline anchor not in diff) surfaces as GitHubAPIError so the
    Publisher can decide to fall back to a plain issue comment."""
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.post(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(422, json={"message": "Unprocessable Entity"})
    )

    client = _build_client(http_client, private_pem)
    with pytest.raises(GitHubAPIError):
        await client.post_pr_review(
            installation_id=INSTALLATION_ID,
            repo=REPO,
            pr_number=PR_NUMBER,
            commit_id="abc",
            body="x",
            event="COMMENT",
            comments=[],
        )


# ---------------------------- rate-limit guardrail ----------------------------


async def test_client_records_rate_limit_headers_after_call(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """After any verb, the client must remember the X-RateLimit-* state."""
    reset = int(_time.time()) + 3600
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.post(f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments").mock(
        return_value=httpx.Response(
            201,
            json={"id": 1},
            headers={"x-ratelimit-remaining": "4500", "x-ratelimit-reset": str(reset)},
        )
    )

    client = _build_client(http_client, private_pem)
    await client.post_pr_comment(
        installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
    )
    assert client._rate_remaining == 4500
    assert client._rate_reset_epoch == reset


async def test_client_sleeps_when_under_floor_and_reset_is_close(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """Pre-flight: under floor + reset within max_wait → sleep, then proceed."""
    near_reset = int(_time.time()) + 5  # 5s away
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.post(f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    client = _build_client(http_client, private_pem)
    # Seed the client as if a previous call had returned "we're at the floor".
    client._rate_remaining = 50
    client._rate_reset_epoch = near_reset

    with patch("pr_review_agent.github.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        await client.post_pr_comment(
            installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
        )
    sleep_mock.assert_awaited_once()
    # The sleep argument is the wait + 1; allow for a 1-2s slack from the test clock.
    await_args = sleep_mock.await_args
    assert await_args is not None
    waited = await_args.args[0]
    assert 4 <= waited <= 7


async def test_client_raises_when_under_floor_and_reset_is_far_away(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """Pre-flight: under floor + reset > max_wait → raise GitHubRateLimitError."""
    far_reset = int(_time.time()) + 600  # 10 min away
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())

    client = _build_client(http_client, private_pem)
    client._rate_remaining = 50
    client._rate_reset_epoch = far_reset

    with pytest.raises(GitHubRateLimitError) as exc_info:
        await client.post_pr_comment(
            installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
        )
    # Suggested retry-after is ~the wait we computed.
    assert exc_info.value.retry_after_seconds is not None
    assert exc_info.value.retry_after_seconds > 100


async def test_client_raises_on_403_with_retry_after(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """Reactive: 403 with Retry-After is the secondary rate-limit path."""
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.post(f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments").mock(
        return_value=httpx.Response(403, headers={"retry-after": "120"})
    )

    client = _build_client(http_client, private_pem)
    with pytest.raises(GitHubRateLimitError) as exc_info:
        await client.post_pr_comment(
            installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
        )
    assert exc_info.value.retry_after_seconds == 120


async def test_client_raises_on_429_with_retry_after(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """Some routes return 429 instead of 403 — same path."""
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.get(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}").mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"})
    )

    client = _build_client(http_client, private_pem)
    with pytest.raises(GitHubRateLimitError) as exc_info:
        await client.get_pr_diff(installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER)
    assert exc_info.value.retry_after_seconds == 30


async def test_client_403_without_retry_after_is_plain_api_error(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """403 without Retry-After is a permission/auth issue, not rate limit."""
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.post(f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )

    client = _build_client(http_client, private_pem)
    with pytest.raises(GitHubAPIError) as exc_info:
        await client.post_pr_comment(
            installation_id=INSTALLATION_ID, repo=REPO, pr_number=PR_NUMBER, body="hi"
        )
    # Must NOT be a GitHubRateLimitError.
    assert not isinstance(exc_info.value, GitHubRateLimitError)

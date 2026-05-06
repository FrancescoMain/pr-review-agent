"""Unit tests for ``GitHubClient.post_pr_comment``.

Mocks both the installation-token exchange and the comments endpoint
with ``respx``, so we cover the contract end-to-end (auth header
includes the installation token, body is JSON-encoded, 4xx/5xx surface
as ``GitHubAPIError``).
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.exceptions import GitHubAPIError

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

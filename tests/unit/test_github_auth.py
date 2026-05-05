"""Unit tests for GitHub App authentication: JWT and installation tokens.

Covers the full surface of ``GitHubAppAuth`` in isolation: JWT shape,
installation-token exchange, caching, refresh-on-near-expiry, error
mapping, and concurrent access. The HTTP layer is mocked with ``respx``.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.exceptions import GitHubAPIError, GitHubAuthError

APP_ID = 12345
INSTALLATION_ID = 99


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[str, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def auth(rsa_keypair: tuple[str, str], http_client: httpx.AsyncClient) -> GitHubAppAuth:
    private_pem, _ = rsa_keypair
    return GitHubAppAuth(app_id=APP_ID, private_key=private_pem, http_client=http_client)


@pytest.fixture
def respx_mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def _token_response(token: str = "ghs_test", *, ttl_seconds: int = 3600) -> httpx.Response:
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    return httpx.Response(
        201,
        json={
            "token": token,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        },
    )


def test_jwt_has_required_claims_and_valid_signature(
    auth: GitHubAppAuth, rsa_keypair: tuple[str, str]
) -> None:
    _, public_pem = rsa_keypair
    token = auth.build_jwt()
    decoded = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert decoded["iss"] == str(APP_ID)
    now = int(time.time())
    assert decoded["iat"] <= now
    assert decoded["exp"] > now


def test_jwt_exp_within_github_ten_minute_limit(auth: GitHubAppAuth) -> None:
    token = auth.build_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["exp"] - decoded["iat"] <= 10 * 60


async def test_get_installation_token_calls_github_with_jwt(
    auth: GitHubAppAuth, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response("ghs_alpha"))

    token = await auth.get_installation_token(INSTALLATION_ID)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"].startswith("Bearer ")
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert token.token == "ghs_alpha"
    assert token.expires_at > datetime.now(UTC)


async def test_get_installation_token_caches_within_ttl(
    auth: GitHubAppAuth, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response("ghs_cached"))

    first = await auth.get_installation_token(INSTALLATION_ID)
    second = await auth.get_installation_token(INSTALLATION_ID)

    assert route.call_count == 1
    assert first.token == second.token == "ghs_cached"


async def test_get_installation_token_refreshes_when_near_expiry(
    auth: GitHubAppAuth, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(
        side_effect=[
            _token_response("ghs_first", ttl_seconds=120),  # expires in 2 min, below 5min margin
            _token_response("ghs_second", ttl_seconds=3600),
        ]
    )

    first = await auth.get_installation_token(INSTALLATION_ID)
    second = await auth.get_installation_token(INSTALLATION_ID)

    assert route.call_count == 2
    assert first.token == "ghs_first"
    assert second.token == "ghs_second"


async def test_get_installation_token_raises_auth_error_on_401(
    auth: GitHubAppAuth, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))

    with pytest.raises(GitHubAuthError):
        await auth.get_installation_token(INSTALLATION_ID)


async def test_get_installation_token_raises_api_error_on_5xx(
    auth: GitHubAppAuth, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=httpx.Response(503, json={"message": "Service Unavailable"}))

    with pytest.raises(GitHubAPIError):
        await auth.get_installation_token(INSTALLATION_ID)


async def test_concurrent_requests_share_one_exchange(
    auth: GitHubAppAuth, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response("ghs_concurrent"))

    results = await asyncio.gather(
        *(auth.get_installation_token(INSTALLATION_ID) for _ in range(8))
    )

    assert route.call_count == 1
    assert all(r.token == "ghs_concurrent" for r in results)

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for the GitHub-side agent tools.

Covers ``make_github_tools`` end-to-end against a respx-mocked GitHub
API: ``get_pr_diff`` returns the diff body and surfaces 5xx as
``GitHubAPIError``; ``get_linked_issues`` parses the closing keywords,
fetches each issue, skips 404s, and dedupes references. We also test
the parser in isolation because it is the piece most likely to drift.

The pragma at the top mutes pyright's ``ainvoke`` partial-unknown
warnings on ``BaseTool`` (a langchain surface we don't control).
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.tools import BaseTool

from pr_review_agent.agent.tools import (
    LinkedIssue,
    PRContext,
    make_github_tools,
    parse_linked_issue_numbers,
)
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
    async with httpx.AsyncClient() as c:
        yield c


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


def _ctx() -> PRContext:
    return PRContext(
        repo=REPO,
        pr_number=PR_NUMBER,
        installation_id=INSTALLATION_ID,
        head_ref="feat/test",
        head_sha="0" * 40,
    )


def _find_tool(tools: list[BaseTool], name: str) -> BaseTool:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool '{name}' not in {[t.name for t in tools]}")


def _issue_payload(
    number: int, *, title: str, state: str = "open", body: str = ""
) -> dict[str, object]:
    return {"number": number, "title": title, "state": state, "body": body}


# ---------------------------- parser ----------------------------


def test_parse_recognises_all_closing_keywords() -> None:
    body = (
        "Closes #1\nclosed #2\nfix #3\nFixes #4\nfixed #5\nresolve #6\nResolves #7\nRESOLVED #8\n"
    )
    assert parse_linked_issue_numbers(body) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_parse_dedupes_preserving_order() -> None:
    assert parse_linked_issue_numbers("Closes #5 and fixes #3, resolves #5") == [5, 3]


def test_parse_handles_none_and_empty() -> None:
    assert parse_linked_issue_numbers(None) == []
    assert parse_linked_issue_numbers("") == []


def test_parse_ignores_unrelated_hashes() -> None:
    assert parse_linked_issue_numbers("see #1 for context") == []
    assert parse_linked_issue_numbers("hashtag #yolo and PR #99") == []


# ---------------------------- get_pr_diff ----------------------------


async def test_get_pr_diff_returns_unified_diff(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    diff_text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    pr_route = respx_mock.get(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}").mock(
        return_value=httpx.Response(200, text=diff_text)
    )

    client = _build_client(http_client, private_pem)
    tools = make_github_tools(ctx=_ctx(), client=client, pr_body_provider=lambda: None)
    result = await _find_tool(tools, "get_pr_diff").ainvoke({})

    assert result == diff_text
    assert pr_route.calls.last.request.headers["Accept"] == "application/vnd.github.diff"
    assert pr_route.calls.last.request.headers["Authorization"] == "token ghs_installation"


async def test_get_pr_diff_raises_on_5xx(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.get(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}").mock(
        return_value=httpx.Response(503)
    )

    client = _build_client(http_client, private_pem)
    tools = make_github_tools(ctx=_ctx(), client=client, pr_body_provider=lambda: None)
    with pytest.raises(GitHubAPIError):
        await _find_tool(tools, "get_pr_diff").ainvoke({})


# ---------------------------- get_linked_issues ----------------------------


async def test_get_linked_issues_happy_path(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    body = "Closes #11, also fixes #12."
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/11").mock(
        return_value=httpx.Response(200, json=_issue_payload(11, title="bug A", state="open"))
    )
    respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/12").mock(
        return_value=httpx.Response(200, json=_issue_payload(12, title="bug B", state="closed"))
    )

    client = _build_client(http_client, private_pem)
    tools = make_github_tools(ctx=_ctx(), client=client, pr_body_provider=lambda: body)
    issues = await _find_tool(tools, "get_linked_issues").ainvoke({})

    assert issues == [
        LinkedIssue(number=11, title="bug A", body="", state="open"),
        LinkedIssue(number=12, title="bug B", body="", state="closed"),
    ]


async def test_get_linked_issues_skips_404(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    body = "Closes #11, fixes #404"
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/11").mock(
        return_value=httpx.Response(200, json=_issue_payload(11, title="real one"))
    )
    respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/404").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    client = _build_client(http_client, private_pem)
    tools = make_github_tools(ctx=_ctx(), client=client, pr_body_provider=lambda: body)
    issues = await _find_tool(tools, "get_linked_issues").ainvoke({})

    assert [i.number for i in issues] == [11]


async def test_get_linked_issues_empty_body(
    private_pem: str, http_client: httpx.AsyncClient
) -> None:
    client = _build_client(http_client, private_pem)
    tools = make_github_tools(ctx=_ctx(), client=client, pr_body_provider=lambda: None)
    issues = await _find_tool(tools, "get_linked_issues").ainvoke({})
    assert issues == []


async def test_get_linked_issues_propagates_5xx(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    body = "Closes #7"
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(503)
    )

    client = _build_client(http_client, private_pem)
    tools = make_github_tools(ctx=_ctx(), client=client, pr_body_provider=lambda: body)
    with pytest.raises(GitHubAPIError):
        await _find_tool(tools, "get_linked_issues").ainvoke({})


# ---------------------------- client-level smoke ----------------------------


async def test_client_get_issue_returns_parsed_payload(
    private_pem: str, http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    ).mock(return_value=_token_response())
    respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/1").mock(
        return_value=httpx.Response(200, json=_issue_payload(1, title="hi", state="open", body="b"))
    )

    client = _build_client(http_client, private_pem)
    payload = await client.get_issue(installation_id=INSTALLATION_ID, repo=REPO, issue_number=1)
    assert payload["number"] == 1
    assert payload["title"] == "hi"

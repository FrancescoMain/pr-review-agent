"""End-to-end tests for the ``/webhook/github`` route.

Covers the full pipeline (signature → JSON parse → event routing →
payload validation) by exercising the FastAPI app via ``TestClient``,
not by calling individual helpers in isolation. Helpers are unit-tested
in ``test_signatures.py``.
"""

import hashlib
import hmac
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pr_review_agent.config import get_settings

WEBHOOK_SECRET = "test-secret-for-pytest"


@pytest.fixture(autouse=True)
def _set_webhook_secret(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Test PR",
            "head": {"ref": "feature/x", "sha": "deadbeef"},
            "base": {"ref": "main", "sha": "cafebabe"},
        },
        "repository": {"full_name": "francesco/playground"},
    }


def test_valid_signature_pull_request_returns_202(client: TestClient) -> None:
    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["pr"] == 42
    assert data["repo"] == "francesco/playground"


def test_missing_signature_returns_401(client: TestClient) -> None:
    response = client.post(
        "/webhook/github",
        json=_pr_payload(),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid signature"}


def test_invalid_signature_returns_401(client: TestClient) -> None:
    response = client.post(
        "/webhook/github",
        json=_pr_payload(),
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert response.status_code == 401


def test_non_json_body_returns_400(client: TestClient) -> None:
    body = b"this is definitely not json"
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400


def test_invalid_pull_request_payload_returns_422(client: TestClient) -> None:
    body = json.dumps({"action": "opened"}).encode()  # missing required fields
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


def test_ping_event_returns_202_ignored(client: TestClient) -> None:
    body = json.dumps({"zen": "Practicality beats purity."}).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "ignored"
    assert "ping" in data["reason"]


def test_get_method_returns_405(client: TestClient) -> None:
    response = client.get("/webhook/github")
    assert response.status_code == 405

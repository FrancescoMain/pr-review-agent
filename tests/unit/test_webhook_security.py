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
        "installation": {"id": 99},
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
    assert data["installation_id"] == 99
    # delivery_id present even when GitHub didn't send one (uuid fallback).
    assert isinstance(data["delivery_id"], str)
    assert data["delivery_id"]


def test_response_echoes_x_github_delivery_header(client: TestClient) -> None:
    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "abc-123-from-github",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["delivery_id"] == "abc-123-from-github"


def test_ignored_event_response_includes_delivery_id(client: TestClient) -> None:
    body = json.dumps({"zen": "Practicality beats purity."}).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "ping-1",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["delivery_id"] == "ping-1"


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


def test_webhook_schedules_agent_run(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_runner(state: dict[str, object]) -> dict[str, object]:
        captured.update(state)
        return state

    client.app.state.agent_runner = _fake_runner  # type: ignore[attr-defined]

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
    # FastAPI runs BackgroundTasks after the response on the same loop;
    # TestClient blocks until they finish before returning.
    assert captured["repo"] == "francesco/playground"
    assert captured["pr_number"] == 42
    assert captured["installation_id"] == 99


def test_background_task_binds_correlation_id_for_logs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the background task, structlog must see correlation_id bound."""
    import structlog

    seen: dict[str, object] = {}

    async def _runner_that_inspects_contextvars(state: dict[str, object]) -> dict[str, object]:
        seen.update(structlog.contextvars.get_contextvars())
        return state

    client.app.state.agent_runner = _runner_that_inspects_contextvars  # type: ignore[attr-defined]

    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-xyz",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert seen.get("correlation_id") == "delivery-xyz"

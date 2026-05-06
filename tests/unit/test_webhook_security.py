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


# ---------------------------- idempotency ----------------------------


class _IdempotencyPool:
    """Stand-in for app.state.db_pool used only by the idempotency check.

    ``find_run_by_correlation_id`` is patched at module-level via
    monkeypatch in each test; this class exists just to be a non-None
    object so the webhook handler enters the idempotency branch.
    """


def test_duplicate_delivery_skips_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched: list[dict[str, object]] = []

    async def _runner(state: dict[str, object]) -> dict[str, object]:
        dispatched.append(state)
        return state

    async def _fake_find(_pool: object, *, correlation_id: str) -> int | None:
        # The handler asks: have I seen this delivery? Yes, run #7.
        assert correlation_id == "duplicate-delivery-1"
        return 7

    monkeypatch.setattr("pr_review_agent.webhook.find_run_by_correlation_id", _fake_find)
    client.app.state.db_pool = _IdempotencyPool()  # type: ignore[attr-defined]
    client.app.state.agent_runner = _runner  # type: ignore[attr-defined]

    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "duplicate-delivery-1",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "duplicate"
    assert data["delivery_id"] == "duplicate-delivery-1"
    assert data["first_seen_run_id"] == 7
    assert dispatched == []  # crucial: the runner must NOT have been called


def test_first_seen_delivery_is_dispatched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched: list[dict[str, object]] = []

    async def _runner(state: dict[str, object]) -> dict[str, object]:
        dispatched.append(state)
        return state

    async def _fake_find(_pool: object, *, correlation_id: str) -> int | None:
        return None  # never seen

    monkeypatch.setattr("pr_review_agent.webhook.find_run_by_correlation_id", _fake_find)
    client.app.state.db_pool = _IdempotencyPool()  # type: ignore[attr-defined]
    client.app.state.agent_runner = _runner  # type: ignore[attr-defined]

    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "first-time-delivery",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert len(dispatched) == 1


def test_idempotency_check_failure_falls_back_to_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the DB query fails, we MUST dispatch anyway — availability over idempotency."""
    dispatched: list[dict[str, object]] = []

    async def _runner(state: dict[str, object]) -> dict[str, object]:
        dispatched.append(state)
        return state

    async def _failing_find(_pool: object, *, correlation_id: str) -> int | None:
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("pr_review_agent.webhook.find_run_by_correlation_id", _failing_find)
    client.app.state.db_pool = _IdempotencyPool()  # type: ignore[attr-defined]
    client.app.state.agent_runner = _runner  # type: ignore[attr-defined]

    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-with-broken-db",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert len(dispatched) == 1


def test_idempotency_check_skipped_when_no_db_pool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pool → no check at all (uuid-fallback delivery, dev mode)."""
    dispatched: list[dict[str, object]] = []
    find_called: list[str] = []

    async def _runner(state: dict[str, object]) -> dict[str, object]:
        dispatched.append(state)
        return state

    async def _fake_find(_pool: object, *, correlation_id: str) -> int | None:
        find_called.append(correlation_id)
        return None

    monkeypatch.setattr("pr_review_agent.webhook.find_run_by_correlation_id", _fake_find)
    client.app.state.db_pool = None  # type: ignore[attr-defined]
    client.app.state.agent_runner = _runner  # type: ignore[attr-defined]

    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-no-db",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert find_called == []  # check was skipped entirely
    assert len(dispatched) == 1


def test_idempotency_check_skipped_when_no_upstream_delivery_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No X-GitHub-Delivery header → uuid fallback path → no idempotency check."""
    dispatched: list[dict[str, object]] = []
    find_called: list[str] = []

    async def _runner(state: dict[str, object]) -> dict[str, object]:
        dispatched.append(state)
        return state

    async def _fake_find(_pool: object, *, correlation_id: str) -> int | None:
        find_called.append(correlation_id)
        return 1  # would say "duplicate" if asked

    monkeypatch.setattr("pr_review_agent.webhook.find_run_by_correlation_id", _fake_find)
    client.app.state.db_pool = _IdempotencyPool()  # type: ignore[attr-defined]
    client.app.state.agent_runner = _runner  # type: ignore[attr-defined]

    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            # No X-GitHub-Delivery: simulates a manual replay/test.
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert find_called == []
    assert len(dispatched) == 1

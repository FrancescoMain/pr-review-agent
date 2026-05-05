"""Tests for the ``/webhook/github`` stub endpoint.

Task 2 only verifies routing and method handling; signature verification and
background dispatch land in Task 3 / Task 4.
"""

from fastapi.testclient import TestClient


def test_webhook_post_returns_accepted(client: TestClient) -> None:
    response = client.post(
        "/webhook/github",
        headers={"X-GitHub-Event": "pull_request"},
        json={"action": "opened", "number": 1},
    )
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


def test_webhook_get_not_allowed(client: TestClient) -> None:
    response = client.get("/webhook/github")
    assert response.status_code == 405

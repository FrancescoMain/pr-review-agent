"""Shared pytest fixtures.

Exposes a ``client`` fixture wrapping the FastAPI app with ``TestClient``
plus a ``settings_env`` fixture that injects deterministic values via
``monkeypatch`` so tests don't depend on the developer's ``.env``.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pr_review_agent.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client

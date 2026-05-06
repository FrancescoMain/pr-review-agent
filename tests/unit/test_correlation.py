"""Unit tests for the correlation-id helpers.

Cover the three branches we care about: a real upstream id is kept
verbatim, a missing one is replaced with a fresh uuid, and clearing
removes the binding from the contextvars scope so the next test (or
the next webhook delivery) starts clean.
"""

import re

import pytest
import structlog

from pr_review_agent.observability.correlation import (
    bind_correlation_id,
    clear_correlation,
)


@pytest.fixture(autouse=True)
def _reset_contextvars() -> None:  # pyright: ignore[reportUnusedFunction]
    structlog.contextvars.clear_contextvars()


def test_bind_uses_request_id_when_provided() -> None:
    cid = bind_correlation_id(request_id="abc-123")
    assert cid == "abc-123"
    assert structlog.contextvars.get_contextvars()["correlation_id"] == "abc-123"


def test_bind_falls_back_to_uuid_when_request_id_is_none() -> None:
    cid = bind_correlation_id(request_id=None)
    # uuid4().hex is 32 lowercase hex chars.
    assert re.fullmatch(r"[0-9a-f]{32}", cid) is not None
    assert structlog.contextvars.get_contextvars()["correlation_id"] == cid


def test_bind_falls_back_to_uuid_when_request_id_is_empty() -> None:
    cid = bind_correlation_id(request_id="")
    assert re.fullmatch(r"[0-9a-f]{32}", cid) is not None


def test_clear_removes_correlation_id() -> None:
    bind_correlation_id(request_id="to-be-cleared")
    clear_correlation()
    assert "correlation_id" not in structlog.contextvars.get_contextvars()


def test_clear_is_safe_when_nothing_bound() -> None:
    """Calling clear without a prior bind must not raise."""
    clear_correlation()
    assert "correlation_id" not in structlog.contextvars.get_contextvars()

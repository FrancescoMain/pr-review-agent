"""Smoke test for ``pr_review_agent.observability.logging.configure_logging``.

Detailed log output (correlation IDs, JSON shape) is W2 territory; here we
just guarantee the entry point exists and is callable.
"""

import logging

from pr_review_agent.observability.logging import configure_logging


def test_configure_logging_does_not_raise() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG

"""Postgres persistence for agent runs and cost tracking.

The DB layer is intentionally narrow: a tiny migrations module that
applies versioned ``.sql`` files at startup, and a ``runs`` repository
with three async functions (``record_run_started``,
``record_run_finished``, ``record_run_failed``) called by the runner.
The DB is optional — when ``database_url`` is unset the runner skips
persistence entirely and just logs the run.

We use ``asyncpg`` directly (no ORM): the schema is small, the queries
are explicit, and we don't pay the cost of a query layer.
"""

from pr_review_agent.db.migrations import apply_migrations
from pr_review_agent.db.runs import (
    find_run_by_correlation_id,
    record_run_failed,
    record_run_finished,
    record_run_started,
)

__all__ = [
    "apply_migrations",
    "find_run_by_correlation_id",
    "record_run_failed",
    "record_run_finished",
    "record_run_started",
]

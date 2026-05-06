# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Apply numbered SQL migrations from ``migrations/`` directory.

We don't use Alembic — the schema is one table for now and adding
Alembic for that is overkill. Migration files are named
``NNNN_<slug>.sql`` and applied in lexicographic order. Each file is
expected to be **idempotent** (``CREATE TABLE IF NOT EXISTS``,
``ON CONFLICT DO NOTHING``) so apply-on-startup is safe even when the
schema is already at head.
"""

from pathlib import Path

import asyncpg
import structlog

_log = structlog.get_logger(__name__)
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


async def apply_migrations(pool: asyncpg.Pool) -> int:  # type: ignore[type-arg]
    """Apply every migration file. Returns the number of files applied.

    Files are read alphabetically. Each file is run as a single SQL
    script inside one connection. We rely on per-file idempotency
    rather than a "skip if version >= X" check; idempotent SQL is
    simpler than tracking version diffs and avoids skew when someone
    runs an out-of-band fix.
    """
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        _log.warning("no migration files found", dir=str(_MIGRATIONS_DIR))
        return 0
    async with pool.acquire() as conn:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            await conn.execute(sql)
            _log.info("migration applied", file=path.name)
    return len(files)

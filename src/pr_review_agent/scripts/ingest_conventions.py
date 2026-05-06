# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Ingest a repo's convention docs into Qdrant.

Run as::

    uv run python -m pr_review_agent.scripts.ingest_conventions \\
        --repo owner/name --head-sha <sha> --installation-id <int>

Pipeline:
  1. Open a ``RepoCheckout`` for ``head_sha`` (clone-on-demand).
  2. Walk the convention globs (``CLAUDE.md``, ``README.md``, etc.).
  3. Chunk each file with ``chunker.chunk_document``.
  4. Embed everything in batches via ``Embedder``.
  5. ``recreate`` the per-repo collection in Qdrant and ``upsert``.

The script is **idempotent** thanks to the recreate-and-upsert
pattern: re-running it with a newer ``head_sha`` replaces the
collection wholesale, no diffing required.

Tests inject a ``ConventionStore`` and a ``RepoCheckout`` factory so
we don't need a live Qdrant or GitHub token.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import structlog
from qdrant_client import AsyncQdrantClient

from pr_review_agent.agent.memory.chunker import chunk_document
from pr_review_agent.agent.memory.embedder import Embedder
from pr_review_agent.agent.memory.store import ConventionDocument, ConventionStore
from pr_review_agent.agent.tools import PRContext, RepoCheckout
from pr_review_agent.config import get_settings
from pr_review_agent.github.auth import GitHubAppAuth

_log = structlog.get_logger(__name__)


def _iter_matching_files(root: Path, globs: Iterable[str]) -> list[Path]:
    """Resolve all glob patterns under ``root`` into a flat, deduped, sorted list."""
    seen: set[Path] = set()
    for pattern in globs:
        for match in root.glob(pattern):
            if match.is_file():
                seen.add(match.resolve())
    return sorted(seen)


def _build_documents(root: Path, files: list[Path]) -> list[ConventionDocument]:
    documents: list[ConventionDocument] = []
    for file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            _log.warning("ingest.skip_non_utf8", path=str(file_path))
            continue
        chunks = chunk_document(text)
        rel_path = file_path.relative_to(root).as_posix()
        for index, chunk in enumerate(chunks):
            documents.append(ConventionDocument(path=rel_path, chunk_index=index, text=chunk))
    return documents


CheckoutFactory = Callable[[PRContext], "RepoCheckout"]


async def run_ingest(
    *,
    repo: str,
    head_sha: str,
    installation_id: int,
    store: ConventionStore,
    checkout_factory: CheckoutFactory,
    globs: Iterable[str],
) -> int:
    """Core pipeline; returns the number of points written. Tests call this directly."""
    ctx = PRContext(
        repo=repo,
        pr_number=1,  # unused for ingest, but PRContext requires gt=0
        installation_id=installation_id,
        head_ref="ingest",  # opaque
        head_sha=head_sha,
    )
    async with checkout_factory(ctx) as checkout:
        files = _iter_matching_files(checkout.root, globs)
        if not files:
            _log.warning("ingest.no_matching_files", repo=repo, globs=list(globs))
            return 0
        _log.info("ingest.found_files", repo=repo, count=len(files))
        documents = _build_documents(checkout.root, files)
        if not documents:
            _log.warning("ingest.no_chunks_extracted", repo=repo)
            return 0
        await store.recreate_collection(repo)
        return await store.upsert_chunks(repo=repo, head_sha=head_sha, documents=documents)


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ingest a repo's conventions into Qdrant.")
    parser.add_argument("--repo", required=True, help="GitHub repo as owner/name")
    parser.add_argument("--head-sha", required=True, help="Commit SHA to ingest")
    parser.add_argument(
        "--installation-id",
        required=True,
        type=int,
        help="GitHub App installation ID (used to authenticate the clone)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.qdrant_url:
        _log.error("ingest.qdrant_url_missing", hint="set QDRANT_URL in .env")
        return 1
    if (
        settings.github_app_id <= 0
        or settings.github_app_private_key_path is None
        or not settings.github_app_private_key_path.exists()
    ):
        _log.error("ingest.github_app_credentials_missing")
        return 1

    qdrant_client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() or None,
    )
    pem = settings.github_app_private_key_path.read_text(encoding="utf-8")

    # The auth, store, and checkout each own their own httpx client; for a
    # short-lived CLI that's simpler than threading a shared one.
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as http:
        auth = GitHubAppAuth(app_id=settings.github_app_id, private_key=pem, http_client=http)
        embedder = Embedder()
        store = ConventionStore(client=qdrant_client, embedder=embedder)

        def checkout_factory(ctx: PRContext) -> RepoCheckout:
            return RepoCheckout(ctx=ctx, auth=auth)

        try:
            count = await run_ingest(
                repo=args.repo,
                head_sha=args.head_sha,
                installation_id=args.installation_id,
                store=store,
                checkout_factory=checkout_factory,
                globs=settings.convention_doc_globs,
            )
        finally:
            await qdrant_client.close()
    _log.info("ingest.done", repo=args.repo, count=count)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover — entry point
    raise SystemExit(main())


# Pyright doesn't see ``Any`` used inside ``run_ingest``; the import keeps
# the future tool generic when we wire ``recall_conventions`` in W3-Task5.
_ = Any

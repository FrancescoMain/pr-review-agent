# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""End-to-end smoke for ``run_ingest`` against an in-memory Qdrant.

We bypass the GitHub clone by giving ``run_ingest`` a checkout factory
that hands back a ``RepoCheckout`` pointing at a local file:// remote
seeded with realistic convention docs (README + CLAUDE.md). This
catches glob resolution, chunker integration, embedder injection, and
Qdrant upsert in a single fast test.
"""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from pr_review_agent.agent.memory.embedder import Embedder
from pr_review_agent.agent.memory.store import ConventionStore, collection_name_for
from pr_review_agent.agent.tools import PRContext, RepoCheckout
from pr_review_agent.scripts.ingest_conventions import run_ingest


def _git(*args: str, cwd: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env, capture_output=True)


@pytest.fixture
def remote_repo(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=remote)
    (remote / "README.md").write_text(
        "# pr-review-agent\n\nHello.\n\nThis is the second paragraph.\n",
        encoding="utf-8",
    )
    (remote / "CLAUDE.md").write_text(
        "# Conventions\n\nUse `uv` for dependencies.\n\nCommits in conventional form.\n",
        encoding="utf-8",
    )
    (remote / "src").mkdir()
    (remote / "src" / "main.py").write_text("# code, not docs\n", encoding="utf-8")
    _git("add", "-A", cwd=remote)
    _git("commit", "-m", "init", "--quiet", cwd=remote)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(remote),
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=remote)
    yield remote, head
    shutil.rmtree(remote, ignore_errors=True)


class _FakeModel:
    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        return [[float(i) for _ in range(4)] for i, _ in enumerate(texts)]


async def test_run_ingest_pushes_chunks_into_qdrant(remote_repo: tuple[Path, str]) -> None:
    remote, head = remote_repo
    repo = "francesco/playground"

    qdrant = AsyncQdrantClient(":memory:")
    store = ConventionStore(client=qdrant, embedder=Embedder(model=_FakeModel()), vector_size=4)

    def checkout_factory(ctx: PRContext) -> RepoCheckout:
        return RepoCheckout(ctx=ctx, remote_url_override=str(remote))

    written = await run_ingest(
        repo=repo,
        head_sha=head,
        installation_id=99,
        store=store,
        checkout_factory=checkout_factory,
        globs=["README.md", "CLAUDE.md", "docs/**/*.md"],
    )

    # README has two paragraphs (heading + Hello + 2nd para → 3 chunks).
    # CLAUDE has three paragraphs (heading + uv + commits → 3 chunks).
    assert written == 6
    assert await store.count(repo) == 6
    # The collection name follows the repo slug convention.
    assert collection_name_for(repo) == "conventions_francesco_playground"


async def test_run_ingest_when_no_files_match(remote_repo: tuple[Path, str]) -> None:
    remote, head = remote_repo
    qdrant = AsyncQdrantClient(":memory:")
    store = ConventionStore(client=qdrant, embedder=Embedder(model=_FakeModel()), vector_size=4)

    def checkout_factory(ctx: PRContext) -> RepoCheckout:
        return RepoCheckout(ctx=ctx, remote_url_override=str(remote))

    written = await run_ingest(
        repo="francesco/playground",
        head_sha=head,
        installation_id=99,
        store=store,
        checkout_factory=checkout_factory,
        globs=["NOPE.md"],
    )
    assert written == 0

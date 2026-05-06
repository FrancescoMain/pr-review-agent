# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for ``ConventionStore`` against an in-memory Qdrant.

``AsyncQdrantClient(":memory:")`` runs the whole vector store in
process — no server, no docker — so these tests stay offline and run
in well under a second. The embedder is faked so we don't load the
real 130 MB model in CI.
"""

from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from pr_review_agent.agent.memory.embedder import Embedder
from pr_review_agent.agent.memory.store import (
    ConventionDocument,
    ConventionMatch,
    ConventionStore,
    collection_name_for,
)


class _FakeModel:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        return [[float(i) + 0.1 for _ in range(self.dim)] for i, _ in enumerate(texts)]


@pytest.fixture
async def store() -> ConventionStore:
    client = AsyncQdrantClient(":memory:")
    embedder = Embedder(model=_FakeModel(dim=4))
    return ConventionStore(client=client, embedder=embedder, vector_size=4)


def test_collection_name_replaces_separators() -> None:
    assert collection_name_for("FrancescoMain/pr-review-agent") == (
        "conventions_FrancescoMain_pr_review_agent"
    )


async def test_recreate_then_upsert_roundtrip(store: ConventionStore) -> None:
    repo = "francesco/playground"
    await store.recreate_collection(repo)
    docs = [
        ConventionDocument(path="README.md", chunk_index=0, text="hello"),
        ConventionDocument(path="README.md", chunk_index=1, text="world"),
        ConventionDocument(path="CLAUDE.md", chunk_index=0, text="conventions"),
    ]
    written = await store.upsert_chunks(repo=repo, head_sha="abc", documents=docs)
    assert written == 3
    assert await store.count(repo) == 3


async def test_recreate_drops_old_points(store: ConventionStore) -> None:
    repo = "francesco/playground"
    await store.recreate_collection(repo)
    await store.upsert_chunks(
        repo=repo,
        head_sha="old",
        documents=[ConventionDocument(path="x", chunk_index=0, text="old")],
    )
    assert await store.count(repo) == 1

    await store.recreate_collection(repo)
    assert await store.count(repo) == 0

    await store.upsert_chunks(
        repo=repo,
        head_sha="new",
        documents=[
            ConventionDocument(path="y", chunk_index=0, text="new1"),
            ConventionDocument(path="y", chunk_index=1, text="new2"),
        ],
    )
    assert await store.count(repo) == 2


async def test_upsert_zero_documents_writes_nothing(store: ConventionStore) -> None:
    repo = "francesco/playground"
    await store.recreate_collection(repo)
    written = await store.upsert_chunks(repo=repo, head_sha="x", documents=[])
    assert written == 0
    assert await store.count(repo) == 0


async def test_query_returns_matches_in_score_order(store: ConventionStore) -> None:
    repo = "francesco/playground"
    await store.recreate_collection(repo)
    await store.upsert_chunks(
        repo=repo,
        head_sha="abc",
        documents=[
            ConventionDocument(path="CLAUDE.md", chunk_index=0, text="use uv"),
            ConventionDocument(
                path="CLAUDE.md", chunk_index=1, text="commits in conventional form"
            ),
            ConventionDocument(path="README.md", chunk_index=0, text="hello world"),
        ],
    )
    matches = await store.query_conventions(repo=repo, query="how do I commit?", top_k=2)

    assert all(isinstance(m, ConventionMatch) for m in matches)
    assert len(matches) == 2
    # Each match must carry its source path + the original text payload.
    assert {m.path for m in matches} <= {"CLAUDE.md", "README.md"}
    assert all(m.text in {"use uv", "commits in conventional form", "hello world"} for m in matches)
    # Scores are ordered descending (best first).
    assert matches[0].score >= matches[1].score


async def test_query_returns_empty_for_unknown_repo(store: ConventionStore) -> None:
    """Querying a repo that was never ingested → empty list, no exception."""
    matches = await store.query_conventions(repo="unknown/never-seeded", query="anything", top_k=3)
    assert matches == []


async def test_query_returns_empty_for_empty_query(store: ConventionStore) -> None:
    repo = "francesco/playground"
    await store.recreate_collection(repo)
    await store.upsert_chunks(
        repo=repo,
        head_sha="x",
        documents=[ConventionDocument(path="x.md", chunk_index=0, text="x")],
    )
    assert await store.query_conventions(repo=repo, query="   ", top_k=3) == []

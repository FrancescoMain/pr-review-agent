# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
"""Qdrant-backed convention store.

W3-Task4 owns the *write* side: the ingest CLI uses
``recreate_collection`` to start fresh per run, then ``upsert_chunks``
to push the embedded paragraphs of a repo's convention docs. W3-Task5
will add ``query_conventions`` for the read side that the Context
Gatherer's ``recall_conventions`` tool will consume.

Collections are scoped by repo (``conventions_<owner>_<name>``) so we
can drop and rebuild a single repo's memory without affecting others,
and so ``recall_conventions`` filters at the routing layer rather than
on every payload.
"""

import datetime as dt
from dataclasses import dataclass

import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from pr_review_agent.agent.memory.embedder import DEFAULT_VECTOR_SIZE, Embedder

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConventionDocument:
    """A single chunk ready to embed and store.

    ``path`` is repo-relative (forward slash); ``chunk_index`` is its
    position within the originating file. ``text`` is the chunk
    content. The store derives the embedding internally.
    """

    path: str
    chunk_index: int
    text: str


def collection_name_for(repo: str) -> str:
    """Map ``owner/name`` to the canonical Qdrant collection name."""
    return "conventions_" + repo.replace("/", "_").replace("-", "_")


class ConventionStore:
    def __init__(
        self,
        *,
        client: AsyncQdrantClient,
        embedder: Embedder,
        vector_size: int = DEFAULT_VECTOR_SIZE,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._vector_size = vector_size

    async def recreate_collection(self, repo: str) -> str:
        """Drop and create the collection for ``repo``. Returns the collection name."""
        name = collection_name_for(repo)
        await self._client.delete_collection(collection_name=name)
        await self._client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
        )
        _log.info("convention_store.recreated", collection=name, repo=repo)
        return name

    async def upsert_chunks(
        self,
        *,
        repo: str,
        head_sha: str,
        documents: list[ConventionDocument],
    ) -> int:
        """Embed and upsert all ``documents`` into the repo's collection.

        Returns the number of points written. The collection MUST
        already exist (call ``recreate_collection`` first).
        """
        if not documents:
            return 0
        name = collection_name_for(repo)
        texts = [doc.text for doc in documents]
        vectors = self._embedder.encode(texts)
        ingested_at = dt.datetime.now(tz=dt.UTC).isoformat()
        points = [
            PointStruct(
                id=idx,
                vector=vector,
                payload={
                    "path": doc.path,
                    "chunk_index": doc.chunk_index,
                    "text": doc.text,
                    "repo": repo,
                    "head_sha": head_sha,
                    "ingested_at": ingested_at,
                },
            )
            for idx, (doc, vector) in enumerate(zip(documents, vectors, strict=True))
        ]
        await self._client.upsert(collection_name=name, points=points, wait=True)
        _log.info(
            "convention_store.upserted",
            collection=name,
            count=len(points),
            head_sha=head_sha,
        )
        return len(points)

    async def count(self, repo: str) -> int:
        """Return how many points are in the repo's collection (for smoke checks)."""
        name = collection_name_for(repo)
        result = await self._client.count(collection_name=name, exact=True)
        return int(result.count)

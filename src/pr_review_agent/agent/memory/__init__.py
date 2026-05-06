"""Convention memory subsystem.

Components (W3-Task4 onwards):
- ``chunker``: split markdown / text docs into bite-sized pieces.
- ``embedder``: thin wrapper around ``sentence-transformers`` with the
  pinned ``BAAI/bge-small-en-v1.5`` model (see CLAUDE.md deviations).
- ``store``: ``ConventionStore`` orchestrates Qdrant + Embedder so the
  ingest CLI and the future ``recall_conventions`` tool share a
  single API.

The ingest CLI (``scripts.ingest_conventions``) writes; W3-Task5 will
add the read side as a tool exposed to the Context Gatherer.
"""

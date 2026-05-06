# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
"""Thin wrapper around ``sentence-transformers`` for batch embedding.

Pinned to ``BAAI/bge-small-en-v1.5`` (384-dim) per CLAUDE.md
deviations: no OpenAI / Voyage / Cohere. The model is downloaded on
first use into the HuggingFace cache (``~/.cache/huggingface``); a
fresh container will block for a few seconds the first time.

The class accepts an injectable ``model`` for tests so we never have
to download the real weights in CI. The production path uses
``SentenceTransformer(model_name)`` lazily — the ``model`` attribute
is built on first ``encode()`` rather than in ``__init__`` so that
loading the runner doesn't transitively load 130 MB of torch weights.
"""

from typing import Any, Protocol

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_VECTOR_SIZE = 384


class _EncodeOnly(Protocol):
    def encode(self, texts: list[str], **kwargs: Any) -> Any: ...


class Embedder:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL,
        model: _EncodeOnly | None = None,
        batch_size: int = 32,
    ) -> None:
        self._model_name = model_name
        self._model: _EncodeOnly | None = model
        self._batch_size = batch_size

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> _EncodeOnly:
        if self._model is None:
            # Local import keeps top-level cheap for code paths that don't embed.
            from sentence_transformers import SentenceTransformer

            # SentenceTransformer satisfies the _EncodeOnly Protocol structurally;
            # pyright doesn't infer it because of overloaded encode() signatures.
            self._model = SentenceTransformer(self._model_name)  # type: ignore[assignment]
        assert self._model is not None  # narrow for the type checker
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Return one float vector per input text.

        ``sentence-transformers`` returns a numpy ndarray; we coerce to
        nested lists so downstream code (Qdrant client, JSON, tests)
        sees a stable Python type.
        """
        if not texts:
            return []
        model = self._load()
        raw = model.encode(texts, batch_size=self._batch_size, show_progress_bar=False)
        # raw can be a numpy array (production) or a plain list (test fakes).
        if hasattr(raw, "tolist"):
            return [list(map(float, vec)) for vec in raw.tolist()]
        return [list(map(float, vec)) for vec in raw]

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for ``Embedder`` with a fake model.

The real ``BAAI/bge-small-en-v1.5`` is 130 MB of torch weights — way
too heavy for CI. We inject a fake model that mimics the shape of
``sentence-transformers``' ``encode()`` output (numpy ndarray of
shape ``(n_texts, dim)``).
"""

from typing import Any

from pr_review_agent.agent.memory.embedder import Embedder


class _FakeModel:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        # Deterministic vector: index-of-text + offset, repeated dim times.
        return [[float(i) + 0.5 for _ in range(self.dim)] for i, _ in enumerate(texts)]


def test_embedder_returns_one_vector_per_text() -> None:
    fake = _FakeModel(dim=4)
    emb = Embedder(model=fake)
    out = emb.encode(["alpha", "beta", "gamma"])
    assert len(out) == 3
    assert all(len(v) == 4 for v in out)
    assert out[0] == [0.5, 0.5, 0.5, 0.5]
    assert out[2] == [2.5, 2.5, 2.5, 2.5]


def test_embedder_returns_empty_on_empty_input() -> None:
    fake = _FakeModel()
    emb = Embedder(model=fake)
    assert emb.encode([]) == []
    assert fake.calls == []  # didn't even invoke the model


def test_embedder_does_not_load_real_model_when_one_is_injected() -> None:
    """Injection short-circuits the lazy ``SentenceTransformer(...)`` import."""
    fake = _FakeModel()
    emb = Embedder(model=fake, model_name="never-loaded")
    emb.encode(["x"])
    assert fake.calls == [["x"]]
    # The model_name is preserved (used downstream for logging).
    assert emb.model_name == "never-loaded"


def test_embedder_coerces_numpy_like_output_to_lists() -> None:
    """If the upstream returned a ``.tolist()``-able object, we still get plain lists."""

    class _NumpyLike:
        def __init__(self, payload: list[list[float]]) -> None:
            self._payload = payload

        def tolist(self) -> list[list[float]]:
            return self._payload

    class _Model:
        def encode(self, texts: list[str], **kwargs: Any) -> _NumpyLike:
            del texts, kwargs
            return _NumpyLike([[1.0, 2.0], [3.0, 4.0]])

    emb = Embedder(model=_Model())
    out = emb.encode(["a", "b"])
    assert out == [[1.0, 2.0], [3.0, 4.0]]

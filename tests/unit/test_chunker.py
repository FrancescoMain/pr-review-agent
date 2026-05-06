"""Unit tests for ``chunk_document``.

The chunker is dumb on purpose; the tests pin its key invariants:
empty input → empty list; paragraphs split on blank lines; oversized
paragraphs fall back to sentence split; oversized sentences are
hard-split; whitespace is trimmed; chunk order matches source order.
"""

from pr_review_agent.agent.memory.chunker import chunk_document


def test_chunker_returns_empty_on_empty_input() -> None:
    assert chunk_document("") == []
    assert chunk_document("   \n\n   ") == []


def test_chunker_splits_on_paragraphs() -> None:
    text = "first paragraph.\n\nsecond paragraph.\n\nthird paragraph."
    assert chunk_document(text) == [
        "first paragraph.",
        "second paragraph.",
        "third paragraph.",
    ]


def test_chunker_handles_multiple_blank_lines_between_paragraphs() -> None:
    text = "alpha\n\n\n   \n\nbeta"
    assert chunk_document(text) == ["alpha", "beta"]


def test_chunker_falls_back_to_sentence_split_for_oversized_paragraph() -> None:
    long_para = ("Short sentence one. " * 200).strip()  # ~4000 chars, single paragraph
    chunks = chunk_document(long_para, max_chars=200)
    # All resulting chunks must be at or below the cap.
    assert all(len(c) <= 200 for c in chunks)
    # And we must have produced more than one chunk.
    assert len(chunks) > 1
    # No chunk is empty.
    assert all(c.strip() == c and c for c in chunks)


def test_chunker_hard_splits_oversized_single_sentence() -> None:
    """A single sentence longer than max_chars must still be split."""
    long_sentence = "x" * 5000  # no terminator → sentence-splitter sees one piece
    chunks = chunk_document(long_sentence, max_chars=1000)
    assert len(chunks) == 5
    assert all(len(c) == 1000 for c in chunks)


def test_chunker_keeps_source_order() -> None:
    text = "AA.\n\nBB.\n\nCC."
    assert chunk_document(text) == ["AA.", "BB.", "CC."]


def test_chunker_strips_chunk_whitespace() -> None:
    text = "   alpha  \n\n  beta\n"
    assert chunk_document(text) == ["alpha", "beta"]

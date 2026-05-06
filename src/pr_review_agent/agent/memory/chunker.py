"""Split a document into chunks small enough to embed and large enough to mean something.

Strategy: split on paragraph (``\\n\\n``) first; if a paragraph is
still over the cap, fall back to sentence split (``. ``); if a single
sentence is over the cap, hard-split at ``max_chars``. The cap is in
**characters**, not tokens — for the convention docs we ingest
(markdown, prose, configs) ~2000 chars ≈ ~500 tokens which keeps the
chunks well under ``BAAI/bge-small-en-v1.5``'s 512-token context.

Empty paragraphs and trailing whitespace are dropped. The order of
chunks in the output preserves the order in the source document.
"""

import re

_DEFAULT_MAX_CHARS = 2000
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def chunk_document(text: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> list[str]:
    """Return non-empty chunks of ``text`` each at most ``max_chars`` long."""
    if not text or not text.strip():
        return []
    chunks: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(text):
        cleaned = paragraph.strip()
        if not cleaned:
            continue
        if len(cleaned) <= max_chars:
            chunks.append(cleaned)
            continue
        # Paragraph too big: split into sentences, then hard-split if needed.
        for sentence in _SENTENCE_SPLIT.split(cleaned):
            sent = sentence.strip()
            if not sent:
                continue
            if len(sent) <= max_chars:
                chunks.append(sent)
                continue
            # A single sentence longer than the cap (rare in markdown). Hard-split.
            for offset in range(0, len(sent), max_chars):
                piece = sent[offset : offset + max_chars].strip()
                if piece:
                    chunks.append(piece)
    return chunks

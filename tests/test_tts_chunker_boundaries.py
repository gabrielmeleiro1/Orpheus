"""
Unit tests for TTS chunker sentence-boundary splitting contract.

Whitespace contract: ``chunk()`` normalizes runs of whitespace to single spaces
via ``' '.join(text.split())``. Rejoining chunks with a single space therefore
recovers the normalized input exactly.
"""

import pytest

from modules.tts.chunker import TextChunker, ChunkConfig


@pytest.fixture
def small_chunker() -> TextChunker:
    """Chunker with max_chars=10 (max_tokens=2, chars_per_token=5)."""
    return TextChunker(ChunkConfig(max_tokens=2, min_tokens=1, chars_per_token=5.0))


MAX_CHARS = 10


def _assert_chunks_within_limit(chunks: list[str], max_chars: int = MAX_CHARS) -> None:
    for chunk in chunks:
        assert len(chunk) <= max_chars, (
            f"Chunk length {len(chunk)} exceeds max_chars={max_chars}: {chunk!r}"
        )


def _assert_text_preserved(chunks: list[str], original: str) -> None:
    """Characters are preserved in order; whitespace may differ at hard-split seams."""
    def _collapse_whitespace(text: str) -> str:
        return "".join(text.split())

    assert _collapse_whitespace(" ".join(chunks)) == _collapse_whitespace(original)


class TestOverlongSentenceSplitting:
    def test_hard_splits_overlong_first_sentence(self, small_chunker: TextChunker) -> None:
        long_sentence = "A" * 39 + "."
        text = f"{long_sentence} Short. End."

        chunks = small_chunker._split_at_boundaries(text, MAX_CHARS)

        _assert_chunks_within_limit(chunks)
        _assert_text_preserved(chunks, text)
        assert len(chunks) > 3

    def test_hard_splits_overlong_middle_sentence(self, small_chunker: TextChunker) -> None:
        long_sentence = "B" * 39 + "."
        text = f"Start. {long_sentence} End."

        chunks = small_chunker._split_at_boundaries(text, MAX_CHARS)

        _assert_chunks_within_limit(chunks)
        _assert_text_preserved(chunks, text)
        assert chunks[0] == "Start."

    def test_hard_splits_overlong_last_sentence(self, small_chunker: TextChunker) -> None:
        long_sentence = "C" * 39 + "."
        text = f"Start. Mid. {long_sentence}"

        chunks = small_chunker._split_at_boundaries(text, MAX_CHARS)

        _assert_chunks_within_limit(chunks)
        _assert_text_preserved(chunks, text)
        assert chunks[0] == "Start."
        assert chunks[1] == "Mid."

    def test_chunk_api_enforces_max_chars_for_overlong_sentences(
        self, small_chunker: TextChunker
    ) -> None:
        """Public chunk() must not emit VRAM-unsafe spans."""
        long_sentence = "A" * 39 + "."
        text = f"{long_sentence} Short. End."

        chunks = small_chunker.chunk(text, max_tokens=2)

        _assert_chunks_within_limit(chunks)
        _assert_text_preserved(chunks, text)

    def test_reproduces_reported_max_chars_violation(self, small_chunker: TextChunker) -> None:
        """Direct reproduction: max_chars=10 must not yield a 40-char first chunk."""
        long_sentence = "A" * 39 + "."
        text = f"{long_sentence} Short. End."

        chunks = small_chunker._split_at_boundaries(text, MAX_CHARS)
        lengths = [len(chunk) for chunk in chunks]

        assert lengths[0] <= MAX_CHARS, f"Got chunk lengths {lengths}"
        assert all(length <= MAX_CHARS for length in lengths)

"""
Property-Based Tests for Text Chunker
======================================
Uses Hypothesis for fuzz testing edge cases.
Tests the TextChunker with various inputs to ensure robustness.
"""

from pathlib import Path
import sys

import pytest
from hypothesis import given, strategies as st, settings, assume

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.tts.chunker import TextChunker, ChunkConfig

pytestmark = pytest.mark.property


def test_chunker_does_not_split_common_abbreviations_as_sentence_boundaries():
    chunker = TextChunker(ChunkConfig(max_tokens=20, min_tokens=5, chars_per_token=4.0))
    text = (
        "Dr. Smith met Prof. Jones at 5 p.m. They discussed the case for a while. "
        "The next full sentence is deliberately long enough to require chunking."
    )

    chunks = chunker.chunk(text)

    assert chunks[0].startswith("Dr. Smith met Prof. Jones at 5 p.m.")
    assert not any(chunk == "Dr." for chunk in chunks)
    assert not any(chunk.endswith("Prof.") for chunk in chunks)


def test_chunker_merges_tiny_sentence_chunks_when_next_chunk_fits():
    chunker = TextChunker(ChunkConfig(max_tokens=35, min_tokens=12, chars_per_token=4.0))
    text = "Yes. No. This sentence gives the tiny answers enough context to speak naturally."

    chunks = chunker.chunk(text)

    assert chunks == [text]


class TestTextChunkerProperties:
    """Property-based tests for TextChunker."""
    
    @pytest.mark.property
    @given(st.text(min_size=0, max_size=10000))
    @settings(max_examples=200, deadline=5000)
    def test_chunker_never_crash(self, text):
        """
        Text chunker should never crash regardless of input.
        
        This is a fundamental robustness property - the chunker should
        handle any text without raising exceptions.
        """
        chunker = TextChunker()
        try:
            chunks = chunker.chunk(text)
            # Result should always be a list
            assert isinstance(chunks, list)
            # Each chunk should be a string
            assert all(isinstance(c, str) for c in chunks)
        except Exception as e:
            pytest.fail(f"Chunker crashed on input: {repr(text[:100])}... Error: {e}")
    
    @pytest.mark.property
    @given(
        st.text(min_size=1, max_size=5000).filter(lambda x: len(x.strip()) > 0),
        st.integers(min_value=10, max_value=1000)
    )
    @settings(max_examples=100, deadline=5000)
    def test_chunked_text_joins_to_original(self, text, max_tokens):
        """
        Joining chunks should produce the original text (minus whitespace differences).
        
        This property ensures no text is lost during chunking.
        """
        config = ChunkConfig(max_tokens=max_tokens)
        chunker = TextChunker(config)
        chunks = chunker.chunk(text)
        
        assert chunks, "Nonempty normalized text must produce chunks"
        assert all(chunk.strip() for chunk in chunks), "Chunks must contain content"
        
        joined = " ".join(chunks)
        original_normalized = " ".join(text.split())
        
        # The joined text should match the original (allowing for whitespace normalization)
        assert joined == original_normalized or len(joined) >= len(original_normalized) * 0.9
        # Hard splits inside overlong tokens can add spaces at chunk seams.
        # Every non-whitespace character must still occur exactly once, in order.
        assert "".join(joined.split()) == "".join(original_normalized.split())
    
    @pytest.mark.property
    @given(
        st.text(min_size=100, max_size=5000),
        st.integers(min_value=20, max_value=200)
    )
    @settings(max_examples=100, deadline=5000)
    def test_chunk_size_honored(self, text, max_tokens):
        """
        Every chunk must respect the documented max_chars ceiling.

        Whitespace is normalized to single spaces before chunking; rejoining
        chunks with one space recovers that normalized text. No 50% overflow
        tolerance — overlong sentences are hard-split at word boundaries.
        """
        config = ChunkConfig(max_tokens=max_tokens)
        chunker = TextChunker(config)
        chunks = chunker.chunk(text)

        max_chars = int(max_tokens * config.chars_per_token)

        for chunk in chunks:
            assert len(chunk) <= max_chars, (
                f"Chunk too long: {len(chunk)} chars, limit was {max_chars}"
            )
    
    @pytest.mark.property
    @given(st.text(min_size=0, max_size=100))
    @settings(max_examples=50, deadline=2000)
    def test_empty_and_whitespace_only(self, text):
        """
        Empty or whitespace-only text should produce empty list.
        """
        chunker = TextChunker()
        chunks = chunker.chunk(text)
        
        if not text or not text.strip():
            # Empty or whitespace-only input
            assert chunks == []
    
    @pytest.mark.property
    @given(
        st.text(min_size=1, max_size=100),
        st.integers(min_value=-100, max_value=10)
    )
    @settings(max_examples=50, deadline=2000)
    def test_negative_max_tokens_clamped(self, text, invalid_max_tokens):
        """
        Invalid (negative or too small) max_tokens should be handled gracefully.
        
        The chunker uses default config when given invalid values.
        """
        assume(invalid_max_tokens < 10)  # Filter to only invalid values
        
        config = ChunkConfig(max_tokens=invalid_max_tokens)
        chunker = TextChunker(config)
        
        # Should not crash
        chunks = chunker.chunk(text)
        assert isinstance(chunks, list)
    
    @pytest.mark.property
    @given(
        st.text(min_size=100, max_size=2000),
        st.integers(min_value=50, max_value=500),
        st.integers(min_value=10, max_value=100)
    )
    @settings(max_examples=100, deadline=5000)
    def test_chunking_is_deterministic(self, text, max_tokens, min_tokens):
        """
        Chunking the same text should produce the same result every time.
        """
        assume(min_tokens < max_tokens)
        
        config = ChunkConfig(max_tokens=max_tokens, min_tokens=min_tokens)
        
        chunker1 = TextChunker(config)
        chunker2 = TextChunker(config)
        
        chunks1 = chunker1.chunk(text)
        chunks2 = chunker2.chunk(text)
        
        assert chunks1 == chunks2



if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-m", "property", "-o", "addopts="]))

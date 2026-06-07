"""
Text Chunker Module
===================
Implements intelligent text chunking for TTS to prevent VRAM overflow.
Uses sentence boundary detection to create natural-sounding chunks.
"""

import re
from dataclasses import dataclass
from typing import Generator


@dataclass
class ChunkConfig:
    """Configuration for text chunking."""
    max_tokens: int = 700
    min_tokens: int = 50
    chars_per_token: float = 4.0  # Approximate chars per token


class TextChunker:
    """
    Intelligent text chunker for TTS processing.
    
    Splits long text into chunks at natural sentence boundaries
    to ensure smooth audio generation without VRAM overflow.
    """
    
    def __init__(self, config: ChunkConfig | None = None):
        """
        Initialize the text chunker.
        
        Args:
            config: Chunk configuration (uses defaults if None)
        """
        self.config = config or ChunkConfig()
        
        # Sentence boundary patterns
        # Matches: . ! ? followed by space or end, with optional quotes
        self._sentence_end = re.compile(
            r'(?<=[.!?])["\']?\s+(?=[A-Z"\'])|'  # Standard sentence end
            r'(?<=[.!?])["\']?$'  # End of text
        )
        self._abbreviations = {
            "Mr.",
            "Mrs.",
            "Ms.",
            "Dr.",
            "Prof.",
            "St.",
            "Jr.",
            "Sr.",
            "etc.",
            "e.g.",
            "i.e.",
            "p.m.",
            "a.m.",
        }

    @staticmethod
    def _token_before_boundary(text: str, boundary: int) -> str:
        """Return the token ending at a sentence boundary using a bounded scan."""
        end = min(len(text), boundary + 1)
        while end > 0 and text[end - 1].isspace():
            end -= 1
        start = end
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        return text[start:end]
        
    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text.
        
        Args:
            text: Input text
            
        Returns:
            Estimated token count
        """
        return int(len(text) / self.config.chars_per_token)
    
    def _find_sentence_boundaries(self, text: str) -> list[int]:
        """
        Find all sentence boundary positions in text.
        
        Args:
            text: Input text
            
        Returns:
            List of character positions where sentences end
        """
        boundaries = []
        for match in self._sentence_end.finditer(text):
            last_token = self._token_before_boundary(text, match.start())
            if last_token in self._abbreviations:
                continue
            boundaries.append(match.start())
        return boundaries

    def _merge_small_chunks(self, chunks: list[str], max_chars: int) -> list[str]:
        """Merge tiny chunks with neighbors when doing so stays under the limit."""
        min_chars = int(max(1, self.config.min_tokens) * self.config.chars_per_token)
        merged: list[str] = []
        i = 0
        while i < len(chunks):
            current = chunks[i].strip()
            if not current:
                i += 1
                continue
            while (
                i + 1 < len(chunks)
                and len(current) < min_chars
                and len(current) + 1 + len(chunks[i + 1]) <= max_chars
            ):
                current = f"{current} {chunks[i + 1].strip()}"
                i += 1
            if merged and len(current) < min_chars and len(merged[-1]) + 1 + len(current) <= max_chars:
                merged[-1] = f"{merged[-1]} {current}"
            else:
                merged.append(current)
            i += 1
        return merged
    
    def _split_at_boundaries(
        self, 
        text: str, 
        max_chars: int
    ) -> list[str]:
        """
        Split text at sentence boundaries respecting max length.
        
        Args:
            text: Text to split
            max_chars: Maximum characters per chunk
            
        Returns:
            List of text chunks
        """
        if len(text) <= max_chars:
            return [text]
        
        boundaries = self._find_sentence_boundaries(text)
        
        if not boundaries:
            # No sentence boundaries found - split by paragraph or hard limit
            paragraphs = text.split('\n\n')
            if len(paragraphs) > 1:
                return self._split_paragraphs(paragraphs, max_chars)
            # Last resort: hard split
            return self._hard_split(text, max_chars)
        
        chunks = []
        current_start = 0
        current_end = 0
        
        for boundary in boundaries:
            # Check if adding this sentence would exceed limit
            potential_end = boundary + 1
            potential_chunk = text[current_start:potential_end].strip()
            
            if len(potential_chunk) <= max_chars:
                current_end = potential_end
            else:
                # Save current chunk and start new one
                if current_end > current_start:
                    chunk = text[current_start:current_end].strip()
                    if chunk:
                        chunks.append(chunk)
                    current_start = current_end
                    current_end = potential_end
                else:
                    # Single sentence too long - need to split differently
                    current_end = potential_end
        
        # Add remaining text
        remaining = text[current_start:].strip()
        if remaining:
            if len(remaining) <= max_chars:
                chunks.append(remaining)
            else:
                # Recursively split remaining
                chunks.extend(self._hard_split(remaining, max_chars))
        
        return chunks
    
    def _split_paragraphs(
        self, 
        paragraphs: list[str], 
        max_chars: int
    ) -> list[str]:
        """Split paragraphs into chunks."""
        chunks = []
        current_chunk = ""
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
                
            if len(current_chunk) + len(para) + 2 <= max_chars:
                if current_chunk:
                    current_chunk += "\n\n" + para
                else:
                    current_chunk = para
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                
                if len(para) <= max_chars:
                    current_chunk = para
                else:
                    # Paragraph too long - split by sentences
                    para_chunks = self._split_at_boundaries(para, max_chars)
                    if para_chunks:
                        chunks.extend(para_chunks[:-1])
                        current_chunk = para_chunks[-1]
                    else:
                        current_chunk = ""
        
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks
    
    def _hard_split(self, text: str, max_chars: int) -> list[str]:
        """
        Hard split text at word boundaries when no sentence breaks.
        
        Args:
            text: Text to split
            max_chars: Maximum characters per chunk
            
        Returns:
            List of chunks
        """
        words = text.split()
        chunks = []
        current_chunk = []
        current_len = 0
        
        for word in words:
            word_len = len(word) + 1  # +1 for space

            if len(word) > max_chars:
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
                    current_chunk = []
                    current_len = 0
                chunks.extend(
                    word[start : start + max_chars]
                    for start in range(0, len(word), max_chars)
                )
                continue
            
            if current_len + word_len <= max_chars:
                current_chunk.append(word)
                current_len += word_len
            else:
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
                current_chunk = [word]
                current_len = len(word)
        
        if current_chunk:
            chunks.append(" ".join(current_chunk))
        
        return chunks
    
    def chunk(
        self, 
        text: str, 
        max_tokens: int | None = None
    ) -> list[str]:
        """
        Split text into chunks suitable for TTS processing.
        
        Args:
            text: Input text to chunk
            max_tokens: Maximum tokens per chunk (uses config default if None)
            
        Returns:
            List of text chunks
        """
        if not text or not text.strip():
            return []
        
        configured_max_tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        max_tokens = configured_max_tokens if configured_max_tokens and configured_max_tokens > 0 else 700
        max_chars = int(max_tokens * self.config.chars_per_token)
        
        # Clean up whitespace
        text = ' '.join(text.split())
        
        # Check if chunking needed
        if self.estimate_tokens(text) <= max_tokens:
            return [text]
        
        return self._merge_small_chunks(self._split_at_boundaries(text, max_chars), max_chars)
    
    def chunk_generator(
        self, 
        text: str, 
        max_tokens: int | None = None
    ) -> Generator[str, None, None]:
        """
        Generate chunks one at a time for memory efficiency.
        
        Args:
            text: Input text to chunk
            max_tokens: Maximum tokens per chunk
            
        Yields:
            Text chunks one at a time
        """
        for chunk in self.chunk(text, max_tokens):
            yield chunk


def chunk_text(
    text: str, 
    max_tokens: int = 700
) -> list[str]:
    """
    Convenience function to chunk text.
    
    Args:
        text: Input text
        max_tokens: Maximum tokens per chunk
        
    Returns:
        List of text chunks
    """
    chunker = TextChunker(ChunkConfig(max_tokens=max_tokens))
    return chunker.chunk(text)


def test_chunker() -> bool:
    """
    Test the text chunker.
    
    Returns:
        True if all tests pass
    """
    chunker = TextChunker()
    
    # Test 1: Short text (no chunking needed)
    short_text = "Hello, this is a short test."
    chunks = chunker.chunk(short_text)
    if len(chunks) != 1 or chunks[0] != short_text:
        print("Test 1 failed: Short text handling")
        return False
    print("✓ Test 1 passed: Short text")
    
    # Test 2: Long text with sentences
    long_text = "First sentence here. " * 50  # ~950 chars
    chunks = chunker.chunk(long_text, max_tokens=100)
    if len(chunks) < 2:
        print("Test 2 failed: Long text not chunked")
        return False
    print(f"✓ Test 2 passed: Long text split into {len(chunks)} chunks")
    
    # Test 3: Dialogue handling
    dialogue = '"Hello there!" she said. "How are you today?" He replied, "I am doing well."'
    chunks = chunker.chunk(dialogue)
    if len(chunks) != 1:
        print("Test 3 failed: Dialogue split unexpectedly")
        return False
    print("✓ Test 3 passed: Dialogue preserved")
    
    # Test 4: Token estimation
    test_text = "word " * 100  # ~500 chars, ~125 tokens
    tokens = chunker.estimate_tokens(test_text)
    if not (100 <= tokens <= 150):
        print(f"Test 4 failed: Token estimation off ({tokens})")
        return False
    print(f"✓ Test 4 passed: Token estimation ({tokens} tokens)")
    
    print("\nAll chunker tests passed!")
    return True


if __name__ == "__main__":
    test_chunker()

"""
Property-Based Tests for File Parsers
======================================
Uses Hypothesis for fuzz testing with malformed/corrupted inputs.
Tests error handling and robustness of PDF and EPUB parsers.
"""

import pytest
import tempfile
from pathlib import Path
from hypothesis import given, strategies as st, settings
from hypothesis.strategies import composite

from modules.ingestion.pdf_parser import PDFParser
from modules.ingestion.epub_parser import EPUBParser


# Binary patterns for malformed files
PDF_HEADER = b"%PDF-1.4\n"
PDF_FOOTER = b"%%EOF\n"
ZIP_HEADER = b"PK\x03\x04"
EPUB_MIMETYPE = b"application/epub+zip"


@composite
def malformed_pdf_bytes(draw):
    """Strategy for generating malformed PDF bytes."""
    strategies = st.one_of(
        # Empty file
        st.just(b""),
        # Just header
        st.just(PDF_HEADER),
        # Just footer
        st.just(PDF_FOOTER),
        # Header with random garbage
        st.binary(min_size=10, max_size=1000).map(lambda x: PDF_HEADER + x),
        # Random binary
        st.binary(min_size=0, max_size=1000),
        # Valid-looking but corrupted
        st.binary(min_size=50, max_size=500).map(
            lambda x: PDF_HEADER + x + PDF_FOOTER
        ),
        # Truncated valid header
        st.just(b"%PDF"),
        # Wrong version format
        st.just(b"%PDF-99.99"),
    )
    return draw(strategies)


@composite
def malformed_epub_bytes(draw):
    """Strategy for generating malformed EPUB bytes."""
    strategies = st.one_of(
        # Empty file
        st.just(b""),
        # Just zip header (valid zip, invalid epub)
        st.just(ZIP_HEADER),
        # Zip header with garbage
        st.binary(min_size=10, max_size=1000).map(lambda x: ZIP_HEADER + x),
        # Random binary
        st.binary(min_size=0, max_size=1000),
        # Truncated
        st.just(b"PK"),
        # Wrong magic bytes
        st.just(b"ZIP\x00\x00"),
    )
    return draw(strategies)


class TestPDFParserRobustness:
    """Property-based tests for PDF parser robustness."""
    
    @pytest.mark.property
    @given(data=malformed_pdf_bytes())
    @settings(max_examples=100, deadline=5000)
    def test_parser_rejects_malformed_pdfs(self, data):
        """
        PDF parser should gracefully reject malformed/corrupted PDFs.
        
        Should raise either FileNotFoundError (if too short) or
        appropriate parsing errors, never crash silently.
        """
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(data)
            temp_path = Path(f.name)

        try:
            # Should raise an error for malformed input. Construction only
            # runs a cheap header/EOF validation, so garbage that keeps both
            # markers is rejected when the content is actually parsed.
            with pytest.raises(Exception) as exc_info:
                PDFParser(temp_path).parse()

            # Verify it's one of the expected exception types
            error_type = type(exc_info.value)
            assert error_type in [
                FileNotFoundError,  # If validation fails early
                ValueError,         # If file extension/signature wrong
                OSError,            # System-level I/O errors
                Exception,          # Parent of our custom errors
            ] or "PDFParsingError" in str(error_type) or "CorruptedFileError" in str(error_type), (
                f"Unexpected error type: {error_type}"
            )
        finally:
            temp_path.unlink(missing_ok=True)
    
    @pytest.mark.property
    @given(
        suffix=st.sampled_from([".txt", ".doc", ".docx", ".html", ".zip", ""])
    )
    @settings(max_examples=20, deadline=2000)
    def test_parser_rejects_wrong_file_types(self, suffix):
        """
        PDF parser should reject files with wrong extensions.
        """
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(b"Some content")
            temp_path = Path(f.name)
        
        try:
            with pytest.raises(ValueError):
                PDFParser(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
    
    @pytest.mark.property
    @given(content=st.text(min_size=0, max_size=1000))
    @settings(max_examples=50, deadline=2000)
    def test_parser_rejects_text_files(self, content):
        """
        PDF parser should reject text files even with .pdf extension.
        """
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(content)
            temp_path = Path(f.name)
        
        try:
            # Should fail validation
            with pytest.raises(Exception):
                PDFParser(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)


class TestEPUBParserRobustness:
    """Property-based tests for EPUB parser robustness."""
    
    @pytest.mark.property
    @given(data=malformed_epub_bytes())
    @settings(max_examples=100, deadline=5000)
    def test_parser_rejects_malformed_epubs(self, data):
        """
        EPUB parser should gracefully reject malformed/corrupted EPUBs.
        
        EPUBs are ZIP files, so we test with various malformed ZIP structures.
        """
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
            f.write(data)
            temp_path = Path(f.name)
        
        try:
            # Should raise an error for malformed input
            with pytest.raises(Exception) as exc_info:
                EPUBParser(temp_path)
            
            # Verify it's one of the expected exception types
            error_type = type(exc_info.value)
            # Allow various error types during validation
            assert error_type in [
                FileNotFoundError,
                ValueError,
                Exception,
            ] or any(name in str(error_type) for name in [
                "EPUBParsingError",
                "CorruptedFileError",
                "BadZipFile",
                "zipfile",
            ]), f"Unexpected error type: {error_type}"
        finally:
            temp_path.unlink(missing_ok=True)
    
    @pytest.mark.property
    @given(
        suffix=st.sampled_from([".txt", ".pdf", ".doc", ".mobi", ".azw3", ""])
    )
    @settings(max_examples=20, deadline=2000)
    def test_parser_rejects_wrong_file_types(self, suffix):
        """
        EPUB parser should reject files with wrong extensions.
        """
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(b"Some content")
            temp_path = Path(f.name)
        
        try:
            with pytest.raises(ValueError):
                EPUBParser(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
    
    @pytest.mark.property
    @given(content=st.text(min_size=0, max_size=1000))
    @settings(max_examples=50, deadline=2000)
    def test_parser_rejects_text_files(self, content):
        """
        EPUB parser should reject text files even with .epub extension.
        """
        with tempfile.NamedTemporaryFile(
            suffix=".epub", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(content)
            temp_path = Path(f.name)
        
        try:
            # Should fail validation
            with pytest.raises(Exception):
                EPUBParser(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "property"])

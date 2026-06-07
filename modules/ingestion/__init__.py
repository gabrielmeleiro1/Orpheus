"""
Ingestion Module
================
Handles PDF, EPUB, TXT, and Markdown parsing, converting documents to structured text.
"""

__all__ = ["Chapter", "Document", "PDFParser", "EPUBParser", "TextParser", "MarkdownParser"]


def __getattr__(name: str):
    """Lazily import parser implementations to avoid heavy side effects on package import."""
    if name == "Chapter":
        from .models import Chapter
        return Chapter
    if name == "Document":
        from .models import Document
        return Document
    if name == "PDFParser":
        from .pdf_parser import PDFParser
        return PDFParser
    if name == "EPUBParser":
        from .epub_parser import EPUBParser
        return EPUBParser
    if name == "TextParser":
        from .text_parser import TextParser
        return TextParser
    if name == "MarkdownParser":
        from .markdown_parser import MarkdownParser
        return MarkdownParser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

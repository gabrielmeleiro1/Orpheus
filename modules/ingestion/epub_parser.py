"""
EPUB Parser Module
==================
Extracts content from EPUB files using EbookLib + BeautifulSoup4.
Optimized for clean text extraction suitable for TTS.
"""

from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

from .models import Chapter as IngestionChapter
from .models import Document as IngestionDocument
from .epub_sections import (
    EPUBSection,
    build_epub_sections,
    extract_title_from_soup,
    parse_epub_markup,
    soup_to_epub_text,
)

from modules.errors import (
    validate_epub,
    EPUBParsingError,
    CorruptedFileError,
    EmptyChapterError,
    ChapterTooLongError,
)


@dataclass
class Chapter(IngestionChapter):
    """Represents a chapter from an EPUB."""
    html_content: str = ""


@dataclass
class Document(IngestionDocument):
    """Represents a parsed EPUB document."""
    language: Optional[str] = None
    publisher: Optional[str] = None


class EPUBParser:
    """
    Parses EPUB files into structured content.
    
    Uses EbookLib for EPUB extraction and BeautifulSoup4 for HTML parsing.
    """
    
    # Maximum words per chapter before warning
    MAX_CHAPTER_WORDS = 50000
    
    def __init__(self, file_path: Path | str, validate: bool = True):
        """
        Initialize the parser with an EPUB file.
        
        Args:
            file_path: Path to the EPUB file
            validate: Whether to validate the EPUB structure
        """
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"EPUB not found: {self.file_path}")
        if self.file_path.suffix.lower() != ".epub":
            raise ValueError(f"Not an EPUB file: {self.file_path}")
        
        # Validate EPUB structure
        if validate:
            try:
                validate_epub(self.file_path)
            except (CorruptedFileError, EPUBParsingError) as e:
                raise EPUBParsingError(
                    message=str(e.message),
                    details=e.details if hasattr(e, 'details') else None,
                    file_path=self.file_path
                )
        
        self._book: Optional[epub.EpubBook] = None
        self._sections: list[EPUBSection] | None = None
    
    def _load_book(self) -> epub.EpubBook:
        """Load the EPUB book if not already loaded."""
        if self._book is None:
            try:
                self._book = epub.read_epub(str(self.file_path))
            except Exception as e:
                raise EPUBParsingError(
                    message=f"Failed to read EPUB: {str(e)}",
                    file_path=self.file_path
                )
        return self._book
    
    def parse(self) -> Document:
        """
        Parse the EPUB into a Document object.
        
        Returns:
            Document with extracted content and chapters
        """
        self._load_book()
        metadata = self.get_metadata()
        chapters = self.extract_chapters()
        
        return Document(
            title=metadata.get("title", self.file_path.stem),
            author=metadata.get("author"),
            chapters=chapters,
            raw_markdown="\n\n".join(chapter.content for chapter in chapters if chapter.content.strip()),
            language=metadata.get("language"),
            publisher=metadata.get("publisher")
        )
    
    def extract_chapters(self, skip_empty: bool = True, max_chapter_words: int = None) -> list[Chapter]:
        """
        Extract all chapters from the EPUB.
        
        Args:
            skip_empty: If True, silently skip empty chapters. If False, raise EmptyChapterError.
            max_chapter_words: Maximum words per chapter (raises ChapterTooLongError if exceeded).
        
        Returns:
            List of Chapter objects with cleaned text content
            
        Raises:
            EmptyChapterError: If chapter is empty and skip_empty=False
            ChapterTooLongError: If chapter exceeds max_chapter_words
        """
        max_words = max_chapter_words or self.MAX_CHAPTER_WORDS
        enforce_max_words = max_chapter_words is not None
        sections = self._extract_sections()
        chapters: list[Chapter] = []

        for chapter_num, section in enumerate(sections, start=1):
            content_stripped = section.content.strip()
            if not content_stripped:
                if not skip_empty:
                    raise EmptyChapterError(chapter_num)
                continue

            word_count = len(content_stripped.split())
            if enforce_max_words and word_count > max_words:
                raise ChapterTooLongError(chapter_num, word_count, max_words)

            chapters.append(Chapter(
                number=chapter_num,
                title=section.title or f"Chapter {chapter_num}",
                content=section.content,
                html_content=section.html_content
            ))
        
        # Check if we got any chapters
        if not chapters:
            raise EPUBParsingError(
                message="No readable chapters found in EPUB",
                file_path=self.file_path
            )
        
        return chapters

    def _extract_sections(self) -> list[EPUBSection]:
        """Extract raw readable sections once; per-call chapter options are applied later."""
        if self._sections is None:
            book = self._load_book()
            self._sections = build_epub_sections(
                book,
                parse_markup=self._parse_markup,
                soup_to_text=self._soup_to_text,
                extract_title=self._extract_title_from_soup,
            )
        return self._sections
    
    def get_metadata(self) -> dict[str, str]:
        """
        Extract metadata from the EPUB.
        
        Returns:
            Dictionary with title, author, language, publisher
        """
        book = self._load_book()
        metadata = {}
        
        # Title
        title = book.get_metadata("DC", "title")
        if title:
            metadata["title"] = title[0][0]
        
        # Author
        creator = book.get_metadata("DC", "creator")
        if creator:
            metadata["author"] = creator[0][0]
        
        # Language
        language = book.get_metadata("DC", "language")
        if language:
            metadata["language"] = language[0][0]
        
        # Publisher
        publisher = book.get_metadata("DC", "publisher")
        if publisher:
            metadata["publisher"] = publisher[0][0]
        
        return metadata
    
    def _html_to_text(self, html_content: str) -> str:
        """
        Convert HTML content to clean text.
        
        Args:
            html_content: Raw HTML string
            
        Returns:
            Cleaned text content
        """
        return self._soup_to_text(self._parse_markup(html_content))

    def _soup_to_text(self, soup: BeautifulSoup) -> str:
        """Convert parsed HTML content to clean text."""
        return soup_to_epub_text(soup)
    
    def _extract_title(self, html_content: str) -> Optional[str]:
        """
        Extract chapter title from HTML.
        
        Args:
            html_content: Raw HTML string
            
        Returns:
            Title string or None
        """
        return self._extract_title_from_soup(self._parse_markup(html_content))

    def _extract_title_from_soup(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract chapter title from parsed HTML."""
        return extract_title_from_soup(soup)

    @staticmethod
    def _parse_markup(markup: str) -> BeautifulSoup:
        """
        Parse chapter markup while avoiding XML-as-HTML warnings for XHTML.

        EPUB chapter documents are commonly XHTML/XML; those should use the XML parser.
        """
        return parse_epub_markup(markup)

    def get_cover(self) -> Optional[bytes]:
        """
        Extract cover image from EPUB if available.
        
        Returns:
            Cover image bytes or None
        """
        book = self._load_book()
        
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_COVER:
                return item.get_content()
        
        # Try to find by name
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if "cover" in item.get_name().lower():
                return item.get_content()
        
        return None

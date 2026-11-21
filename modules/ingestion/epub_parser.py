"""
EPUB Parser Module
==================
Extracts content from EPUB files using EbookLib + BeautifulSoup4.
Optimized for clean text extraction suitable for TTS.
"""

from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import re
from ebooklib import epub
from bs4 import BeautifulSoup

from .models import Chapter as IngestionChapter
from .models import Document as IngestionDocument
from .text_layout import opening_heading_text
from .epub_structure import read_epub_book
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
)


# Font obfuscation (IDPF and Adobe) is declared in encryption.xml too, but it
# only mangles embedded fonts; every other algorithm means encrypted content.
_FONT_OBFUSCATION_ALGORITHMS = frozenset({
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
})


def encrypted_epub_resources(file_path: Path | str) -> list[str]:
    """Return archive members an EPUB declares as encrypted (DRM), fonts excluded.

    Call after ``validate_epub`` so the member size limits already apply.
    """
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(file_path) as archive:
        try:
            payload = archive.read("META-INF/encryption.xml")
        except KeyError:
            return []
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError:
        # Unreadable encryption metadata cannot vouch for readable content.
        return ["META-INF/encryption.xml"]
    encrypted: list[str] = []
    for data in root.iter("{*}EncryptedData"):
        method = data.find("{*}EncryptionMethod")
        algorithm = (method.get("Algorithm") if method is not None else "") or ""
        if algorithm.strip() in _FONT_OBFUSCATION_ALGORITHMS:
            continue
        reference = data.find(".//{*}CipherReference")
        encrypted.append((reference.get("URI") if reference is not None else "") or "?")
    return encrypted


def raise_if_drm_protected(file_path: Path) -> None:
    """Refuse DRM-encrypted EPUBs instead of narrating ciphertext as text."""
    encrypted = encrypted_epub_resources(file_path)
    if encrypted:
        shown = ", ".join(encrypted[:3]) + (" ..." if len(encrypted) > 3 else "")
        raise EPUBParsingError(
            message=(
                "This EPUB is DRM-protected (its content is encrypted) and cannot be "
                "converted. Use a DRM-free copy of the book."
            ),
            details=f"Encrypted resources: {shown}",
            file_path=file_path,
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
                # Re-check at the decompression boundary so callers cannot
                # bypass ZIP metadata limits by opting out of constructor
                # validation.
                validate_epub(self.file_path)
                raise_if_drm_protected(self.file_path)
                self._book = read_epub_book(self.file_path)
            except (CorruptedFileError, EPUBParsingError) as e:
                raise EPUBParsingError(
                    message=e.message,
                    details=e.details if hasattr(e, "details") else None,
                    file_path=self.file_path,
                )
            except Exception as e:
                # Keep the raw exception for diagnostics, not in the message.
                raise EPUBParsingError(
                    message="The EPUB could not be read; the file is damaged or not a valid EPUB.",
                    details=f"{type(e).__name__}: {e}",
                    file_path=self.file_path,
                ) from e
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
    
    def extract_chapters(self) -> list[Chapter]:
        """Extract the EPUB's readable sections as chapters, skipping empty ones."""
        sections = self._extract_sections()
        chapters: list[Chapter] = []

        for chapter_num, section in enumerate(sections, start=1):
            if not section.content.strip():
                continue

            heading_text = ""
            soup = parse_epub_markup(section.html_content)
            heading = soup.find(["h1", "h2", "h3"])
            if (
                heading and opening_heading_text(soup_to_epub_text(heading), section.title)
                or re.match(r"(?i)^part\s+[ivxlcdm\d]+\b", section.title)
            ):
                heading_text = opening_heading_text(section.content, section.title)
            chapters.append(Chapter(
                number=chapter_num,
                title=section.title or f"Chapter {chapter_num}",
                content=section.content,
                html_content=section.html_content,
                heading_text=heading_text,
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
        
        # Title. An empty element (<dc:title/>) yields [(None, {})], which is
        # truthy, so only record a key when there is real text behind it --
        # otherwise the caller's .get(key, default) fallback never fires.
        title = book.get_metadata("DC", "title")
        if title:
            title_text = (title[0][0] or "").strip()
            if title_text:
                metadata["title"] = title_text

        # Author
        creator = book.get_metadata("DC", "creator")
        if creator:
            creator_text = (creator[0][0] or "").strip()
            if creator_text:
                metadata["author"] = creator_text
        
        # Language
        language = book.get_metadata("DC", "language")
        if language:
            metadata["language"] = language[0][0]
        
        # Publisher
        publisher = book.get_metadata("DC", "publisher")
        if publisher:
            metadata["publisher"] = publisher[0][0]
        
        return metadata
    
    def _soup_to_text(self, soup: BeautifulSoup) -> str:
        """Convert parsed HTML content to clean text."""
        return soup_to_epub_text(soup)

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

"""
PDF Parser Module
=================
Converts PDF files to structured Markdown using PyMuPDF4LLM.
Optimized for Apple Silicon.
"""

from pathlib import Path
from dataclasses import dataclass
from typing import Literal
import re
import pymupdf4llm

from .models import Chapter as IngestionChapter
from .models import Document as IngestionDocument
from .pdf_artifact_normalizer import normalize_pdf_artifacts
from .pdf_preflight import analyze_extracted_pdf_text_layer

from modules.errors import (
    validate_pdf,
    PDFParsingError,
    CorruptedFileError,
    EmptyChapterError,
    ChapterTooLongError,
    OCRRequiredPDFError,
)


@dataclass
class Chapter(IngestionChapter):
    """Represents a chapter extracted from a PDF."""
    start_page: int
    end_page: int


@dataclass
class Document(IngestionDocument):
    """Represents a parsed PDF document."""
    total_pages: int = 0


PDFExtractionBackend = Literal["pymupdf4llm", "fitz", "auto"]

_STRUCTURAL_TOC_TITLE_RE = re.compile(
    r"^(?:"
    r"contents|table of contents|"
    r"list of\b.*|"
    r"part\s+(?:[ivxlcdm]+|\d+)\b.*|"
    r"subject index|name index|index|"
    r"bibliography|references|notes|endnotes|"
    r"copyright(?: page)?|title page|half title"
    r")$",
    re.IGNORECASE,
)
_NUMBERED_TOC_TITLE_RE = re.compile(r"^\s*(?:chapter\s+)?\d{1,3}[.)]?\s+\S", re.IGNORECASE)
_INTRO_TOC_TITLE_RE = re.compile(r"^\s*(?:introduction|preface|prologue)\b", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,12})\s*$")
_DOTTED_LEADER_RE = re.compile(r"\.{3,}|…{2,}")
_NO_TOC_PAGE_HEADING_RE = re.compile(
    r"^(?:"
    r"Prologue|Preface|Introduction|Epilogue|"
    r"From\s+[A-Z0-9].+|"
    r"A\s+passage\s+from\s+.+|"
    r"An\s+excerpt\s+from\s+.+|"
    r"Excerpt\s+from\s+.+"
    r")$"
)


class PDFParser:
    """
    Parses PDF files into structured Markdown format.
    
    Uses PyMuPDF4LLM for fast, accurate extraction optimized for LLMs.
    """
    
    # Maximum words per chapter before warning
    MAX_CHAPTER_WORDS = 50000
    
    def __init__(
        self,
        file_path: Path | str,
        validate: bool = True,
        extraction_backend: PDFExtractionBackend = "auto",
    ):
        """
        Initialize the parser with a PDF file.
        
        Args:
            file_path: Path to the PDF file
            validate: Whether to validate the PDF structure
            extraction_backend: Text extraction backend. ``fitz`` is fastest
                for plain text PDFs, ``pymupdf4llm`` preserves legacy markdown
                extraction, and ``auto`` falls back to ``fitz`` when markdown
                coverage is clearly incomplete.
        """
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"PDF not found: {self.file_path}")
        if self.file_path.suffix.lower() != ".pdf":
            raise ValueError(f"Not a PDF file: {self.file_path}")
        
        # Validate PDF structure
        if validate:
            try:
                validate_pdf(self.file_path)
            except CorruptedFileError as e:
                raise PDFParsingError(
                    message=str(e.message),
                    details=e.details,
                    file_path=self.file_path
                )
        if extraction_backend not in {"pymupdf4llm", "fitz", "auto"}:
            raise ValueError(f"Unsupported PDF extraction backend: {extraction_backend}")
        self.extraction_backend = extraction_backend

        self._doc = None
        self._toc = None
        self._markdown = None
        self._metadata = None
        self._total_pages = None
    
    def parse(self, skip_empty: bool = True, max_chapter_words: int = None) -> Document:
        """
        Parse the PDF into a Document object.
        
        Args:
            skip_empty: If True, silently skip empty chapters. If False, raise EmptyChapterError.
            max_chapter_words: Maximum words per chapter (raises ChapterTooLongError if exceeded).
                              Defaults to MAX_CHAPTER_WORDS class attribute.
        
        Returns:
            Document with extracted content and chapters
        
        Raises:
            PDFParsingError: If PDF cannot be parsed
            CorruptedFileError: If PDF structure is invalid
            EmptyChapterError: If chapter is empty and skip_empty=False
            ChapterTooLongError: If chapter exceeds max_chapter_words
        """
        import fitz
        
        max_words = max_chapter_words or self.MAX_CHAPTER_WORDS
        
        try:
            pages_data = self._extract_pages()
        except Exception as e:
            raise PDFParsingError(
                message=f"Failed to extract markdown: {str(e)}",
                file_path=self.file_path
            )
        
        # Handle empty document
        if not pages_data:
            raise CorruptedFileError(
                message="PDF contains no extractable text",
                file_path=self.file_path
            )

        text_layer = analyze_extracted_pdf_text_layer(
            pages_data,
            total_pages=len(pages_data),
        )
        if text_layer.requires_ocr:
            raise OCRRequiredPDFError(
                file_path=self.file_path,
                confidence=text_layer.confidence,
                sampled_pages=text_layer.sampled_pages,
                text_chars=text_layer.text_chars,
            )

        pages_data = normalize_pdf_artifacts(pages_data)
        
        # Combine full markdown for the document record
        self._markdown = "\n\n".join(p["text"] for p in pages_data)
        
        # Get metadata
        try:
            metadata = self._metadata
            total_pages = self._total_pages
            if metadata is None or total_pages is None:
                with fitz.open(self.file_path) as doc:
                    total_pages = len(doc)
                    metadata = doc.metadata
                    self._total_pages = total_pages
                    self._metadata = metadata
                    self._toc = doc.get_toc()
            title = metadata.get("title", "") or self.file_path.stem
            author = metadata.get("author", "")
        except Exception as e:
            raise PDFParsingError(
                message=f"Failed to read PDF metadata: {str(e)}",
                file_path=self.file_path
            )
        
        # Extract chapters structure from TOC or page-heading fallback.
        chapters = self._extract_chapters(total_pages, pages_data)
        
        # Populate content for each chapter based on page ranges
        valid_chapters = []
        for chapter in chapters:
            # Handle potential bounds errors
            start_idx = max(0, chapter.start_page - 1)
            end_idx = min(len(pages_data), chapter.end_page)
            
            # Join text for pages in this chapter
            chapter_pages = pages_data[start_idx:end_idx]
            chapter.content = "\n\n".join(p["text"] for p in chapter_pages)
            
            # Check for empty chapter
            content_stripped = chapter.content.strip()
            if not content_stripped:
                if skip_empty:
                    continue  # Skip this chapter
                else:
                    raise EmptyChapterError(chapter.number, chapter.title)
            
            # Check for extremely long chapter
            word_count = len(content_stripped.split())
            if word_count > max_words:
                raise ChapterTooLongError(chapter.number, word_count, max_words)
            
            valid_chapters.append(chapter)
        
        # Renumber chapters after filtering
        for i, chapter in enumerate(valid_chapters):
            chapter.number = i + 1
        
        return Document(
            title=title,
            author=author if author else None,
            chapters=valid_chapters,
            total_pages=total_pages,
            raw_markdown=self._markdown
        )

    def _extract_pages(self) -> list[dict[str, str]]:
        """Extract page text chunks using the configured backend."""
        if self.extraction_backend == "fitz":
            return self._extract_fitz_pages()

        if self.extraction_backend == "pymupdf4llm":
            return self._extract_pymupdf4llm_pages()

        try:
            fitz_pages = self._extract_fitz_pages()
        except Exception:
            return self._extract_pymupdf4llm_pages()

        if self._fitz_pages_look_complete(fitz_pages):
            return fitz_pages

        try:
            markdown_pages = self._extract_pymupdf4llm_pages()
        except Exception:
            return fitz_pages

        markdown_chars = self._page_text_chars(markdown_pages)
        fitz_chars = self._page_text_chars(fitz_pages)
        markdown_non_empty = self._non_empty_page_count(markdown_pages)
        fitz_non_empty = self._non_empty_page_count(fitz_pages)

        if fitz_chars > markdown_chars and (
            markdown_chars < fitz_chars * 0.5
            or markdown_non_empty < fitz_non_empty * 0.5
        ):
            return fitz_pages
        return markdown_pages

    @classmethod
    def _fitz_pages_look_complete(cls, pages_data: list[dict[str, str]]) -> bool:
        """Return True when plain PyMuPDF text extraction has document-level coverage."""
        page_count = len(pages_data)
        if page_count == 0:
            return False

        text_chars = cls._page_text_chars(pages_data)
        non_empty_pages = cls._non_empty_page_count(pages_data)
        minimum_non_empty = max(1, (page_count + 1) // 2)
        return text_chars >= 1000 and non_empty_pages >= minimum_non_empty

    def _extract_pymupdf4llm_pages(self) -> list[dict[str, str]]:
        """Extract markdown page chunks through the legacy PyMuPDF4LLM path."""
        pages_data = pymupdf4llm.to_markdown(str(self.file_path), page_chunks=True)
        return [
            dict(page) if isinstance(page, dict) else {"text": str(page)}
            for page in pages_data
        ]

    def _extract_fitz_pages(self) -> list[dict[str, str]]:
        """Extract plain text per page through PyMuPDF."""
        import fitz

        with fitz.open(self.file_path) as doc:
            self._total_pages = len(doc)
            self._metadata = doc.metadata
            self._toc = doc.get_toc()
            return [{"text": page.get_text("text")} for page in doc]

    @staticmethod
    def _page_text_chars(pages_data: list[dict[str, str]]) -> int:
        return sum(len(str(page.get("text", ""))) for page in pages_data)

    @staticmethod
    def _non_empty_page_count(pages_data: list[dict[str, str]]) -> int:
        return sum(1 for page in pages_data if str(page.get("text", "")).strip())

    def extract_toc(self) -> list[tuple[int, str, int]]:
        """
        Extract the Table of Contents from the PDF.
        
        Returns:
            List of (level, title, page_number) tuples
        """
        if self._toc is not None:
            return self._toc or []

        import fitz
        with fitz.open(self.file_path) as doc:
            self._toc = doc.get_toc()
        return self._toc or []
    
    @staticmethod
    def _normalize_title_for_structure(title: str) -> str:
        normalized = re.sub(r"\s+", " ", title or "").strip()
        return normalized.strip(" .:-–—")

    @classmethod
    def _is_structural_toc_title(cls, title: str) -> bool:
        normalized = cls._normalize_title_for_structure(title)
        return bool(_STRUCTURAL_TOC_TITLE_RE.match(normalized))

    @classmethod
    def _select_toc_chapter_entries(
        cls,
        toc: list[tuple[int, str, int]],
    ) -> list[tuple[str, int]]:
        content_entries = [
            (level, str(title).strip(), int(page))
            for level, title, page in toc
            if str(title).strip()
            and int(page) > 0
            and not cls._is_structural_toc_title(str(title))
        ]
        numbered_entries = [
            (level, title, page)
            for level, title, page in content_entries
            if _NUMBERED_TOC_TITLE_RE.match(title)
        ]
        if len(numbered_entries) >= 2:
            first_numbered_page = min(page for _, _, page in numbered_entries)
            introductory_entries = [
                (level, title, page)
                for level, title, page in content_entries
                if page < first_numbered_page and _INTRO_TOC_TITLE_RE.match(title)
            ]
            selected = introductory_entries + numbered_entries
            return [(title, page) for _, title, page in sorted(selected, key=lambda item: item[2])]

        if not content_entries:
            return []

        level_counts: dict[int, int] = {}
        for level, _title, _page in content_entries:
            level_counts[level] = level_counts.get(level, 0) + 1
        selected_level = min(
            level_counts,
            key=lambda level: (-level_counts[level], level),
        )
        return [
            (title, page)
            for level, title, page in content_entries
            if level == selected_level
        ]

    @classmethod
    def _page_heading_candidate(cls, text: str) -> str | None:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return None

        first_lines = lines[:14]
        if any(line.lower() in {"contents", "table of contents"} for line in first_lines):
            return None
        if sum(1 for line in first_lines if _DOTTED_LEADER_RE.search(line)) >= 2:
            return None

        for index, line in enumerate(first_lines):
            if _PAGE_NUMBER_RE.fullmatch(line):
                continue
            if len(line) > 160:
                continue
            candidate = line
            if (
                re.match(r"^(?:From|A passage from|An excerpt from|Excerpt from)\b", candidate)
                and candidate.count("(") > candidate.count(")")
                and index + 1 < len(first_lines)
                and not _PAGE_NUMBER_RE.fullmatch(first_lines[index + 1])
                and len(candidate) + 1 + len(first_lines[index + 1]) <= 160
            ):
                candidate = f"{candidate} {first_lines[index + 1]}"
            if _NO_TOC_PAGE_HEADING_RE.match(candidate):
                return candidate
        return None

    @classmethod
    def _extract_heading_chapters_from_pages(
        cls,
        pages_data: list[dict[str, str]],
    ) -> list[tuple[str, int]]:
        candidates: list[tuple[str, int]] = []
        seen_pages: set[int] = set()
        for page_index, page in enumerate(pages_data, start=1):
            heading = cls._page_heading_candidate(str(page.get("text", "")))
            if heading is None or page_index in seen_pages:
                continue
            candidates.append((heading, page_index))
            seen_pages.add(page_index)
        if len(candidates) < 2:
            return []
        return candidates

    @staticmethod
    def _chapters_from_entries(
        entries: list[tuple[str, int]],
        total_pages: int,
    ) -> list[Chapter]:
        chapters: list[Chapter] = []
        ordered_entries = sorted(entries, key=lambda item: item[1])
        for i, (title, start_page) in enumerate(ordered_entries):
            end_page = ordered_entries[i + 1][1] - 1 if i + 1 < len(ordered_entries) else total_pages
            if start_page > end_page:
                start_page = end_page
            chapters.append(
                Chapter(
                    number=i + 1,
                    title=title,
                    content="",
                    start_page=start_page,
                    end_page=end_page,
                )
            )
        return chapters

    def _extract_chapters(
        self,
        total_pages: int,
        pages_data: list[dict[str, str]] | None = None,
    ) -> list[Chapter]:
        """
        Extract chapters based on TOC or heading detection.
        
        Args:
            total_pages: Total page count to calculate end pages
            
        Returns:
            List of Chapter objects
        """
        toc = self.extract_toc()
        
        if not toc:
            if pages_data:
                heading_entries = self._extract_heading_chapters_from_pages(pages_data)
                if heading_entries:
                    return self._chapters_from_entries(heading_entries, total_pages)
            return [
                Chapter(
                    number=1,
                    title="Full Document",
                    content="",
                    start_page=1,
                    end_page=total_pages,
                )
            ]

        entries = self._select_toc_chapter_entries(toc)
        if not entries:
            return [
                Chapter(
                    number=1,
                    title="Full Document",
                    content="",
                    start_page=1,
                    end_page=total_pages,
                )
            ]

        return self._chapters_from_entries(entries, total_pages)
    
    def get_page_count(self) -> int:
        """Get the total number of pages in the PDF."""
        import fitz
        with fitz.open(self.file_path) as doc:
            return len(doc)
    
    def get_markdown(self) -> str:
        """
        Get the full markdown content.
        
        Returns:
            Markdown string of entire document
        """
        if self._markdown is None:
            pages_data = normalize_pdf_artifacts(self._extract_pages())
            self._markdown = "\n\n".join(str(page.get("text", "")) for page in pages_data)
        return self._markdown

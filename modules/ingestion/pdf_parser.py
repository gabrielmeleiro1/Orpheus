"""
PDF Parser Module
=================
Extracts per-page PDF text with PyMuPDF and splits it into chapters by the
outline or, without one, by page headings.
"""

from pathlib import Path
from dataclasses import dataclass
from typing import Literal
import re
import pymupdf4llm

from .epub_sections import _is_structural_title
from .models import Chapter as IngestionChapter
from .models import Document as IngestionDocument
from .pdf_artifact_normalizer import normalize_pdf_artifacts
from .pdf_preflight import analyze_extracted_pdf_text_layer
from .text_layout import opening_heading_text

from modules.errors import (
    validate_pdf,
    PDFParsingError,
    CorruptedFileError,
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

# Outline entries that are never chapters: book matter, and Part dividers.
_BOOK_MATTER_TOC_TITLE_RE = re.compile(
    r"^(?:"
    r"contents|table of contents|"
    r"list of\b.*|"
    r"subject index|name index|index|"
    r"bibliography|references|notes|endnotes|"
    r"copyright(?: page)?|title page|half[ -]title|"
    r"front ?matter|back ?matter|prelims|preliminary pages"
    r")$",
    re.IGNORECASE,
)
_PART_TOC_TITLE_RE = re.compile(r"^part\s+(?:[ivxlcdm]+|\d+)\b.*$", re.IGNORECASE)
_NUMBERED_TOC_TITLE_RE = re.compile(r"^\s*(?:chapter\s+)?(\d{1,3})[.)]?\s+\S", re.IGNORECASE)
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
    """Parses PDF files into chapters of page text."""

    def __init__(
        self,
        file_path: Path | str,
        validate: bool = True,
        extraction_backend: PDFExtractionBackend = "auto",
        artifact_policies: tuple | None = None,
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
            artifact_policies: Optional artifact normalization policy override.
                Defaults to the parser-safe default policies.
        """
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"PDF not found: {self.file_path}")
        if self.file_path.suffix.lower() != ".pdf":
            raise ValueError(f"Not a PDF file: {self.file_path}")
        
        # Validate PDF structure
        if validate:
            from .source_problems import file_problem

            problem = file_problem(self.file_path)
            if problem is not None:
                raise PDFParsingError(
                    message=problem.message, details=problem.details, file_path=self.file_path,
                )
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
        self.artifact_policies = artifact_policies

        self._toc = None
        self._markdown = None
        self._metadata = None
        self._total_pages = None
    
    def parse(self) -> Document:
        """
        Parse the PDF into a Document object, skipping chapters without text.

        Returns:
            Document with extracted content and chapters

        Raises:
            PDFParsingError: If PDF cannot be parsed
            CorruptedFileError: If PDF structure is invalid
        """
        import fitz

        try:
            pages_data = self._extract_pages()
        except Exception as e:
            # PyMuPDF can retain a native file stream in its constructor's
            # traceback after rejecting a corrupt PDF. Release frame locals
            # across the cause chain so even a retained UI/test exception does
            # not keep the source locked on Windows. Stack locations survive.
            import traceback

            pending = [e]
            seen = set()
            while pending:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                traceback.clear_frames(error.__traceback__)
                pending.extend(
                    cause for cause in (error.__cause__, error.__context__) if cause is not None
                )
            # Name the actual problem (password, damaged file) instead of the
            # library's "document closed or encrypted" wording.
            from .source_problems import pdf_problem

            problem = pdf_problem(self.file_path, require_pages=False)
            if problem is not None:
                raise PDFParsingError(
                    message=problem.message, details=problem.details, file_path=self.file_path,
                ) from None
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

        if self.artifact_policies is not None:
            pages_data = normalize_pdf_artifacts(pages_data, self.artifact_policies)
        else:
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
        chapters = self._extract_chapters(self.extract_toc(), pages_data, total_pages, title)

        # Populate content for each chapter based on page ranges
        valid_chapters = []
        for chapter in chapters:
            # Handle potential bounds errors
            start_idx = max(0, chapter.start_page - 1)
            end_idx = min(len(pages_data), chapter.end_page)
            
            # Join text for pages in this chapter
            chapter_pages = pages_data[start_idx:end_idx]
            chapter.content = "\n\n".join(p["text"] for p in chapter_pages)
            chapter.heading_text = opening_heading_text(chapter.content, chapter.title)
            if chapter.content.strip():
                valid_chapters.append(chapter)
        
        # Guard against a document that parsed to no readable narrative content
        # (0-byte/whitespace-only source), mirroring the EPUB empty-output guard.
        if not valid_chapters:
            raise CorruptedFileError(
                message="PDF contains no readable text",
                file_path=self.file_path,
            )

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
        import fitz

        # Own the document lifetime: PyMuPDF4LLM can retain an opened file in
        # an exception traceback when malformed input fails during extraction.
        # On Windows that prevents upload cleanup and keeps the source locked.
        with fitz.open(self.file_path) as doc:
            pages_data = pymupdf4llm.to_markdown(doc, page_chunks=True)
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
        return bool(_BOOK_MATTER_TOC_TITLE_RE.match(normalized) or _PART_TOC_TITLE_RE.match(normalized))

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
        selected = cls._select_chapter_level_entries(content_entries)
        if not selected:
            return []

        # Content pages between the front matter and the first selected entry
        # (a parent chapter's opening, an unnumbered foreword) would otherwise
        # belong to no chapter. Such a lead must follow outlined front matter
        # or sit at the level of a later chapter; a book-title root spanning
        # every chapter opens on the title, copyright and contents pages. On
        # the first page it must also open nested entries: a same-level entry
        # there ("Front Matter", the book's title) spans those pages too.
        # After the first page an outlined entry is content: dropping a parent
        # chapter's opening pages is worse than narrating a title page.
        first_page = min(page for _, page in selected)
        front_matter_end = max(
            (
                int(page)
                for _, title, page in toc
                if 0 < int(page) < first_page and cls._is_book_matter_toc_title(str(title))
            ),
            default=0,
        )
        body_levels = {
            level
            for level, title, page in content_entries
            if page >= first_page and not cls._is_book_matter_toc_title(title)
        }
        leads: list[tuple[str, int]] = []
        for index, (level, title, page) in enumerate(content_entries):
            opens_nested_entries = index + 1 < len(content_entries) and content_entries[index + 1][0] > level
            if (
                front_matter_end < page < first_page
                and not cls._is_book_matter_toc_title(title)
                and (
                    page > 1
                    or (opens_nested_entries and (front_matter_end or level in body_levels))
                )
            ):
                leads.append((title, page))
        lead = min(leads, key=lambda entry: entry[1], default=None)
        # Outlined back matter after the last chapter (Notes, Bibliography,
        # Index) gets its own chapters instead of extending the last one, so
        # the non-prose filter can judge those pages on their own.
        last_page = max(page for _, page in selected)
        back_matter = [
            (str(title).strip(), int(page))
            for _, title, page in toc
            if int(page) > last_page
            and str(title).strip()
            and _BOOK_MATTER_TOC_TITLE_RE.match(cls._normalize_title_for_structure(str(title)))
        ]
        return [*([lead] if lead else []), *selected, *back_matter]

    @classmethod
    def _is_book_matter_toc_title(cls, title: str) -> bool:
        # The outline's own book-matter titles plus the EPUB ones (cover,
        # dedication, also by, ...); Part dividers are body structure.
        normalized = cls._normalize_title_for_structure(title)
        return bool(_BOOK_MATTER_TOC_TITLE_RE.match(normalized) or _is_structural_title(normalized))

    @staticmethod
    def _select_chapter_level_entries(
        content_entries: list[tuple[int, str, int]],
    ) -> list[tuple[str, int]]:
        numbered_entries = [
            (level, title, page)
            for level, title, page in content_entries
            if _NUMBERED_TOC_TITLE_RE.match(title)
        ]
        # Chapter numbers run in sequence somewhere; titles that merely open
        # with a number ("10 Days at Sea", "20 Years Later") do not, and must
        # not drop their unnumbered sibling chapters. Numbered entries below
        # the top content level are sections of unnumbered chapters, whose
        # pages they do not cover.
        numbers = [
            int(_NUMBERED_TOC_TITLE_RE.match(title).group(1))
            for _, title, _ in sorted(numbered_entries, key=lambda item: item[2])
        ]
        top_level = min((level for level, _, _ in content_entries), default=0)
        if (
            len(numbered_entries) >= 2
            and all(level == top_level for level, _, _ in numbered_entries)
            and any(b == a + 1 for a, b in zip(numbers, numbers[1:]))
        ):
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
        for page_index, page in enumerate(pages_data, start=1):
            heading = cls._page_heading_candidate(str(page.get("text", "")))
            if heading is not None:
                candidates.append((heading, page_index))
        if len(candidates) < 2:
            return []
        return candidates

    @staticmethod
    def _chapters_from_entries(
        entries: list[tuple[str, int]],
        total_pages: int,
    ) -> list[Chapter]:
        # Chapters own whole pages, so entries that start on the same page
        # (including pages clamped to the end) collapse into the first one.
        ordered_entries: list[tuple[str, int]] = []
        for title, page in sorted(entries, key=lambda item: item[1]):
            page = min(page, total_pages)
            if not ordered_entries or ordered_entries[-1][1] != page:
                ordered_entries.append((title, page))

        chapters: list[Chapter] = []
        for i, (title, start_page) in enumerate(ordered_entries):
            end_page = (
                ordered_entries[i + 1][1] - 1
                if i + 1 < len(ordered_entries)
                else total_pages
            )
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

    @classmethod
    def _extract_chapters(
        cls,
        toc: list[tuple[int, str, int]],
        pages_data: list[dict[str, str]],
        total_pages: int,
        document_title: str,
    ) -> list[Chapter]:
        """
        Extract chapters based on TOC or heading detection.

        Without either, the whole document is one chapter named after the
        document, as for plain-text sources.
        """
        if toc:
            entries = cls._select_toc_chapter_entries(toc)
        else:
            entries = cls._extract_heading_chapters_from_pages(pages_data)
        return cls._chapters_from_entries(entries or [(document_title, 1)], total_pages)

"""Lightweight source preflight helpers."""

from __future__ import annotations

from pathlib import Path
import re
import zipfile
from typing import Any, Optional

from .text_encoding import read_source_text
from .markdown_parser import detect_title_block, iter_markdown_lines, strip_leading_front_matter
from .pdf_preflight import analyze_pdf_text_layer
from .source_problems import source_problem
from modules.errors import validate_epub

from modules.contracts.public_errors import PublicError, public_error_detail
from modules.contracts.source_extensions import SUPPORTED_SOURCE_SUFFIXES

SUPPORTED_PREFLIGHT_SUFFIXES = SUPPORTED_SOURCE_SUFFIXES
DEFAULT_SAMPLE_CHARS = 900
MAX_CHAPTER_TITLES = 200
MAX_CHAPTER_TITLE_CHARS = 160
MAX_PDF_PREVIEW_PAGES = 20
# Without an outline, the parser splits on page headings it finds after
# normalizing every page (about 4 ms a page). Preflight runs that same path up
# to this length; a longer outline-less PDF previews as one chapter, and its
# headings are found during conversion.
MAX_PDF_HEADING_SCAN_PAGES = 100
TINY_SOURCE_BYTES = 64
# Measured on Kokoro at speed 1.0 (a 524k-character novel became 7.9 h of
# audio, chapter announcements and pauses included).
ESTIMATED_CHARS_PER_AUDIO_SECOND = 18.5
# Paired emphasis delimiters (*word*, __word__, ***word***) that open and close
# a run of text. Word-internal underscores (snake_case) and fence or code
# markers are left alone. Each scan starts at a delimiter that does not follow
# another delimiter or a word character and stops at the next delimiter, so
# matching stays linear in the sample length.
_MARKDOWN_EMPHASIS_RE = re.compile(
    r"(?<![\w*_])([*_]{1,3})(?=[^\s*_])([^*_]+)(?<=\S)\1(?![\w*_])"
)


def _strip_markdown_emphasis(text: str) -> str:
    return _MARKDOWN_EMPHASIS_RE.sub(r"\2", text)


def estimated_audio_seconds(text_chars: int) -> int:
    """Rough spoken length of ``text_chars`` characters, so the UI can warn early."""
    return round(max(0, int(text_chars)) / ESTIMATED_CHARS_PER_AUDIO_SECOND)


def _collapse_sample(text: str, max_chars: int) -> str:
    # Short lines are already bounded; the C regex substitution is cheaper
    # than constructing one Python match per word for these common inputs.
    if max_chars < 0 or len(text) <= 4096:
        return re.sub(r"\s+", " ", text).strip()[:max_chars].strip()
    parts: list[str] = []
    remaining = max_chars
    for match in re.finditer(r"\S+", text):
        if remaining <= 0:
            break
        if parts:
            parts.append(" ")
            remaining -= 1
        end = min(match.end(), match.start() + remaining)
        parts.append(text[match.start():end])
        remaining -= end - match.start()
    return "".join(parts).strip()


def _base_payload(
    source_path: Path,
    source_type: str,
    warnings: list[str],
    fallback_title: str | None = None,
) -> dict[str, Any]:
    try:
        size_bytes = source_path.stat().st_size
    except OSError:
        size_bytes = 0

    if size_bytes < TINY_SOURCE_BYTES:
        warnings.append("Source is very small; conversion may produce little or no audio.")

    return {
        "title": fallback_title if fallback_title is not None else source_path.stem,
        "author": None,
        "chapter_count": 0,
        "chapter_titles": [],
        "source_type": source_type,
        "sample": "",
        "warnings": warnings,
        "size_bytes": size_bytes,
        "requires_ocr": False,
        "pdf_text_layer": None,
        # Set when the source cannot be converted at all, in the same shape as
        # an API error detail: a stable code, a plain-language message for the
        # user, and the library's own wording under "details" when there is any.
        "blocking_error": None,
        "estimated_audio_seconds": None,
    }


def _preflight_text(
    source_path: Path,
    source_type: str,
    max_sample_chars: int,
    fallback_title: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    payload = _base_payload(source_path, source_type, warnings, fallback_title)
    text = read_source_text(source_path)

    sample = _collapse_sample(text, max_sample_chars)
    if not sample:
        warnings.append("No readable text was found in this source.")

    payload.update(
        {
            "estimated_audio_seconds": estimated_audio_seconds(len(text.strip())),
            "chapter_count": 1 if sample else 0,
            "chapter_titles": [payload["title"][:MAX_CHAPTER_TITLE_CHARS]] if sample else [],
            "sample": sample,
        }
    )
    return payload


def _preflight_markdown(
    source_path: Path,
    max_sample_chars: int,
    fallback_title: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    payload = _base_payload(source_path, "md", warnings, fallback_title)
    text = strip_leading_front_matter(read_source_text(source_path))
    first_heading = None
    chapter_count = 0
    chapter_titles: list[str] = []
    sample_parts: list[str] = []
    sample_length = 0
    # Preserve Python slice semantics for callers explicitly requesting a negative limit.
    unbounded_body: list[str] | None = [] if max_sample_chars < 0 else None
    # A leading title block ("# Title" + "*by Author*") is metadata, as in the
    # parser: it is neither a chapter nor part of the sample.
    title_block = detect_title_block(text)
    in_title_block = False
    body_chars = 0
    for line, heading in iter_markdown_lines(text):
        if title_block is not None and heading is not None and first_heading is None:
            first_heading = title_block[0]
            in_title_block = True
            continue
        if heading is not None:
            in_title_block = False
        elif in_title_block:
            continue
        if heading is None:
            body_chars += len(line.strip())
        if heading is not None:
            chapter_count += 1
            if len(chapter_titles) < MAX_CHAPTER_TITLES:
                chapter_titles.append(heading[:MAX_CHAPTER_TITLE_CHARS])
            if first_heading is None:
                first_heading = heading
        elif unbounded_body is not None:
            unbounded_body.append(line)
        elif sample_length < max_sample_chars:
            chunk = _collapse_sample(line, max_sample_chars - sample_length)
            if chunk:
                sample_parts.append(chunk)
                sample_length += len(chunk) + 1
    sample = (
        _collapse_sample("\n".join(unbounded_body), max_sample_chars)
        if unbounded_body is not None
        else " ".join(sample_parts)[:max_sample_chars].strip()
    )
    # Markdown emphasis markers are formatting, never text to show or speak.
    sample = _strip_markdown_emphasis(sample)
    if not sample:
        warnings.append("No readable text was found in this source.")

    payload.update(
        {
            "title": first_heading if first_heading is not None else payload["title"],
            "author": title_block[1] if title_block is not None else None,
            "estimated_audio_seconds": estimated_audio_seconds(body_chars),
            "chapter_count": chapter_count if chapter_count else (1 if sample else 0),
            "chapter_titles": chapter_titles or (
                [payload["title"][:MAX_CHAPTER_TITLE_CHARS]] if sample else []
            ),
            "sample": sample,
        }
    )
    return payload


def _preflight_pdf(
    source_path: Path,
    max_sample_chars: int,
    fallback_title: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    payload = _base_payload(source_path, "pdf", warnings, fallback_title)
    text_layer = None
    try:
        import fitz

        from .pdf_artifact_normalizer import normalize_pdf_artifacts
        from .pdf_parser import PDFParser

        text_layer = analyze_pdf_text_layer(source_path)
        payload.update(
            {
                "requires_ocr": text_layer.requires_ocr,
                "pdf_text_layer": text_layer.to_dict(),
            }
        )
        if text_layer.warning_message:
            warnings.append(text_layer.warning_message)

        with fitz.open(source_path) as document:
            metadata = document.metadata or {}
            title = str(metadata.get("title") or "").strip() or payload["title"]
            author: Optional[str] = str(metadata.get("author") or "").strip() or None
            # The chapters the parser will produce, not every outline entry.
            chapter_titles: list[str] = []
            # Pages already extracted for heading detection also feed the sample.
            page_texts: list[str] = []
            if text_layer.text_chars > 0 and not text_layer.requires_ocr:
                toc = document.get_toc(simple=True)
                pages_data: list[dict[str, str]] = []
                if not toc and len(document) <= MAX_PDF_HEADING_SCAN_PAGES:
                    page_texts = [page.get_text("text") for page in document]
                    pages_data = normalize_pdf_artifacts([{"text": text} for text in page_texts])
                chapter_titles = [
                    chapter.title
                    for chapter in PDFParser._extract_chapters(toc, pages_data, len(document), title)
                ]
            page_samples: list[str] = []
            if not text_layer.requires_ocr:
                for index in range(min(len(document), MAX_PDF_PREVIEW_PAGES)):
                    page_samples.append(
                        page_texts[index] if page_texts else document[index].get_text("text")
                    )
                    if sum(len(chunk) for chunk in page_samples) >= max_sample_chars:
                        break
    except Exception as exc:
        warnings.append(f"Source may be malformed or unreadable: {exc}")
        return payload

    sample = _collapse_sample("\n".join(page_samples), max_sample_chars)
    has_text = text_layer.text_chars > 0
    if not has_text and not payload["requires_ocr"]:
        warnings.append("No readable text was found in this PDF.")
    payload.update(
        {
            "title": title,
            "author": author,
            "estimated_audio_seconds": (
                estimated_audio_seconds(
                    text_layer.text_chars * text_layer.total_pages / text_layer.sampled_pages
                )
                if has_text and text_layer.sampled_pages
                else None
            ),
            "chapter_count": len(chapter_titles),
            "chapter_titles": [
                chapter_title[:MAX_CHAPTER_TITLE_CHARS]
                for chapter_title in chapter_titles[:MAX_CHAPTER_TITLES]
            ],
            "sample": sample,
        }
    )
    return payload


def _preflight_epub(
    source_path: Path,
    max_sample_chars: int,
    fallback_title: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    payload = _base_payload(source_path, "epub", warnings, fallback_title)
    if not zipfile.is_zipfile(source_path):
        return _blocked(
            payload,
            "epub_damaged",
            "This EPUB is damaged or is not an EPUB file, so it cannot be opened.",
        )
    failure_code = "epub_damaged"
    try:
        # Validate ZIP metadata before ebooklib can decompress any member.
        validate_epub(source_path)
        from .epub_parser import raise_if_drm_protected
        from .epub_sections import build_epub_sections
        from .epub_structure import read_epub_book

        failure_code = "epub_drm_protected"
        raise_if_drm_protected(source_path)
        failure_code = "epub_damaged"

        book = read_epub_book(source_path)
        title_values = book.get_metadata("DC", "title")
        author_values = book.get_metadata("DC", "creator")
        # An empty <dc:title/> yields [(None, {})]; guard the None before strip.
        title = (title_values[0][0] or "").strip() if title_values else ""
        title = title or payload["title"]
        author = (author_values[0][0] or "").strip() if author_values else ""
        author = author or None
        sections = build_epub_sections(book)
        samples: list[str] = []
        for section in sections:
            if sum(len(chunk) for chunk in samples) >= max_sample_chars:
                break
            samples.append(section.content)
    except Exception as exc:
        # Typed ingestion errors carry a user-facing message; anything else is
        # a parser internal whose text would mean nothing to the user.
        reason = getattr(exc, "message", None) or "The EPUB could not be read."
        details = getattr(exc, "details", None) or (None if hasattr(exc, "message") else str(exc))
        return _blocked(payload, failure_code, reason, details)

    sample = _collapse_sample("\n".join(samples), max_sample_chars)
    if not sample:
        warnings.append("No readable text was found in this EPUB.")
    payload.update(
        {
            "title": title or payload["title"],
            "author": author or None,
            "chapter_count": len(sections),
            "estimated_audio_seconds": estimated_audio_seconds(
                sum(len(section.content) for section in sections)
            ),
            "chapter_titles": [
                section.title.strip()[:MAX_CHAPTER_TITLE_CHARS]
                for section in sections[:MAX_CHAPTER_TITLES] if section.title.strip()
            ],
            "sample": sample,
        }
    )
    return payload


def _blocked(
    payload: dict[str, Any], code: str, reason: str, details: str | None = None,
) -> dict[str, Any]:
    payload["blocking_error"] = public_error_detail(
        PublicError(code, reason, 422, {"details": details} if details else None)
    )
    payload["warnings"] = [reason]
    payload["chapter_count"] = 0
    return payload


def preflight_source(
    source_path: Path | str,
    max_sample_chars: int = DEFAULT_SAMPLE_CHARS,
    *,
    fallback_title: str | None = None,
) -> dict[str, Any]:
    """Return quick source metadata and conversion warnings."""
    path = Path(source_path).expanduser()
    if not path.exists():
        raise ValueError(f"Source file not found: {path}")
    if not path.is_file():
        raise ValueError("Choose a source file, not a folder or another non-file item.")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_PREFLIGHT_SUFFIXES:
        raise ValueError("Unsupported source type. Choose .pdf, .epub, .txt, or .md")

    problem = source_problem(path)
    if problem is not None:
        payload = _base_payload(path, suffix.lstrip("."), [], fallback_title)
        return _blocked(payload, problem.code, problem.message, problem.details)
    if suffix == ".txt":
        return _preflight_text(path, "txt", max_sample_chars, fallback_title)
    if suffix == ".md":
        return _preflight_markdown(path, max_sample_chars, fallback_title)
    if suffix == ".pdf":
        return _preflight_pdf(path, max_sample_chars, fallback_title)
    return _preflight_epub(path, max_sample_chars, fallback_title)

"""Lightweight source preflight helpers."""

from __future__ import annotations

from pathlib import Path
import re
import zipfile
from typing import Any, Optional

from .text_encoding import read_source_text
from .markdown_parser import iter_markdown_lines, strip_leading_front_matter
from .pdf_preflight import analyze_pdf_text_layer
from modules.errors import validate_epub

from modules.contracts.source_extensions import SUPPORTED_SOURCE_SUFFIXES

SUPPORTED_PREFLIGHT_SUFFIXES = SUPPORTED_SOURCE_SUFFIXES
DEFAULT_SAMPLE_CHARS = 900
TINY_SOURCE_BYTES = 64


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
        "source_type": source_type,
        "sample": "",
        "warnings": warnings,
        "size_bytes": size_bytes,
        "requires_ocr": False,
        "pdf_text_layer": None,
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
            "chapter_count": 1 if sample else 0,
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
    sample_parts: list[str] = []
    sample_length = 0
    # Preserve Python slice semantics for callers explicitly requesting a negative limit.
    unbounded_body: list[str] | None = [] if max_sample_chars < 0 else None
    for line, heading in iter_markdown_lines(text):
        if heading is not None:
            chapter_count += 1
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
    if not sample:
        warnings.append("No readable text was found in this source.")

    payload.update(
        {
            "title": first_heading if first_heading is not None else payload["title"],
            "chapter_count": chapter_count if chapter_count else (1 if sample else 0),
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
            toc = document.get_toc(simple=True)
            page_samples: list[str] = []
            if not text_layer.requires_ocr:
                for page in document[: min(len(document), 5)]:
                    page_samples.append(page.get_text("text"))
                    if sum(len(chunk) for chunk in page_samples) >= max_sample_chars:
                        break
    except Exception as exc:
        warnings.append(f"Source may be malformed or unreadable: {exc}")
        return payload

    sample = _collapse_sample("\n".join(page_samples), max_sample_chars)
    if not sample and not payload["requires_ocr"]:
        warnings.append("No readable text was found in this PDF.")
    payload.update(
        {
            "title": title,
            "author": author,
            "chapter_count": 0 if payload["requires_ocr"] else len(toc) if toc else (1 if sample else 0),
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
        warnings.append("Source may be malformed or unreadable: not a valid EPUB archive")
        return payload
    try:
        # Validate ZIP metadata before ebooklib can decompress any member.
        validate_epub(source_path)
        from ebooklib import epub

        from .epub_sections import build_epub_sections

        book = epub.read_epub(str(source_path))
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
        warnings.append(f"Source may be malformed or unreadable: {exc}")
        return payload

    sample = _collapse_sample("\n".join(samples), max_sample_chars)
    if not sample:
        warnings.append("No readable text was found in this EPUB.")
    payload.update(
        {
            "title": title or payload["title"],
            "author": author or None,
            "chapter_count": len(sections),
            "sample": sample,
        }
    )
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

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_PREFLIGHT_SUFFIXES:
        raise ValueError("Unsupported source type. Choose .pdf, .epub, .txt, or .md")

    if suffix == ".txt":
        return _preflight_text(path, "txt", max_sample_chars, fallback_title)
    if suffix == ".md":
        return _preflight_markdown(path, max_sample_chars, fallback_title)
    if suffix == ".pdf":
        return _preflight_pdf(path, max_sample_chars, fallback_title)
    return _preflight_epub(path, max_sample_chars, fallback_title)

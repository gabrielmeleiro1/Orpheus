"""Pipeline ingestion-stage helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.errors import CorruptedFileError
from modules.ingestion.models import Document

_MAX_LOGGED_CHAPTERS = 200


def ingest_source(runtime: Any, source_path: Path):
    """Parse source file and extract document structure."""
    suffix = source_path.suffix.lower()
    runtime.config.source_type = suffix.lstrip(".")
    runtime.log_verbose(f"[PARSER] Ingesting file: {source_path.name}", "process")
    runtime.log_verbose(f"[PARSER] File type: {suffix}", "info")

    file_size = source_path.stat().st_size
    runtime.log_verbose(f"[PARSER] File size: {file_size / 1024:.1f} KB", "info")

    parser = None
    if suffix == ".epub":
        from modules.ingestion.epub_parser import EPUBParser

        parser = EPUBParser(source_path)
    elif suffix == ".pdf":
        from modules.ingestion.pdf_parser import PDFParser

        parser = PDFParser(
            source_path,
            extraction_backend=getattr(runtime.config, "pdf_extraction_backend", "fitz"),
        )
    elif suffix == ".txt":
        from modules.ingestion.text_parser import TextParser

        parser = TextParser(source_path)
    elif suffix == ".md":
        from modules.ingestion.markdown_parser import MarkdownParser

        parser = MarkdownParser(source_path)
    else:
        described = suffix or "(no extension)"
        raise ValueError(
            f"Unsupported file type: {described}. "
            "Supported formats are .pdf, .epub, .txt, and .md."
        )

    runtime.log_verbose("[PARSER] Parsing content...", "process")
    document = parser.parse()
    if bool(getattr(runtime.config, "pre_chunk_document_cleanup", True)):
        from modules.text.document_normalizer import normalize_extracted_document

        raw_chars = sum(len(chapter.content) for chapter in document.chapters)
        raw_chapters = len(document.chapters)
        document = normalize_extracted_document(
            document,
            source_type=suffix,
            skip_non_prose_sections=bool(
                getattr(runtime.config, "skip_non_prose_sections", True)
            ),
        )
        normalized_chars = sum(len(chapter.content) for chapter in document.chapters)
        normalized_chapters = len(document.chapters)
        runtime.log_verbose(
            "[PARSER] Pre-chunk cleanup complete: "
            f"{raw_chapters} -> {normalized_chapters} chapters, "
            f"{raw_chars} -> {normalized_chars} chars",
            "info",
        )
        for section in document.skipped_non_prose_sections[:_MAX_LOGGED_CHAPTERS]:
            runtime.log_verbose(
                f"[PARSER] Skipped non-prose section: {section['title']} ({section['reason']})",
                "info",
            )

    readable_chapters = [
        chapter
        for chapter in document.chapters
        if str(getattr(chapter, "content", "") or "").strip()
    ]
    if not readable_chapters:
        raise CorruptedFileError(
            message="Document contains no readable narrative content",
            file_path=source_path,
        )

    num_chapters = len(document.chapters)
    runtime.log_verbose(f"[PARSER] Extracted: {num_chapters} chapters found", "success")

    # Parsing is already finished here; this loop only reports it. Bound the
    # report so a source with thousands of headings cannot flood the event
    # stream: one progress event per percent, and a capped chapter listing.
    last_reported_pct = -1
    for i, chapter in enumerate(document.chapters):
        progress_pct = int((i + 1) / num_chapters * 100) if num_chapters > 0 else 0
        if i < _MAX_LOGGED_CHAPTERS:
            runtime.log_verbose(
                f"[PARSER] [{progress_pct:3d}%] Chapter {i + 1}/{num_chapters}: "
                f"{chapter.title[:40]}... ({len(chapter.content)} chars)",
                "info",
            )
        elif i == _MAX_LOGGED_CHAPTERS:
            runtime.log_verbose(
                f"[PARSER] ... {num_chapters - i} more chapters not listed", "info",
            )
        if progress_pct == last_reported_pct and i != num_chapters - 1:
            continue
        last_reported_pct = progress_pct
        runtime.notify_progress(
            chapter_idx=i,
            total_chapters=num_chapters,
            message=f"Parsing chapter {i + 1}/{num_chapters}: {chapter.title[:30]}...",
        )

    runtime.log_verbose(f"[PARSER] Parsing complete - {num_chapters} chapters ready", "success")
    runtime.log_verbose(f"[STATUS] Parsing done - {num_chapters} chapters extracted", "process")
    return document


def save_intermediate_source(runtime: Any, document: Document, output_dir: Path) -> None:
    """Save full source markdown for debugging."""
    if not runtime.config.save_cleaning_debug:
        return

    try:
        raw_markdown = getattr(document, "raw_markdown", "")
        if not isinstance(raw_markdown, str):
            raw_markdown = ""
        if raw_markdown.strip():
            source_content = raw_markdown
        else:
            fallback_body = "\n\n".join(
                str(getattr(chapter, "content", "")).strip()
                for chapter in document.chapters
            ).strip()
            source_content = f"# {document.title}\n\n{fallback_body}"
        source_md = output_dir / "source.md"
        source_md.write_text(source_content, encoding="utf-8")
        runtime.log_verbose(f"Saved source markdown: {source_md.name}", "info")
    except Exception as exc:
        runtime.log_verbose(f"Failed to save source.md: {exc}", "warning")

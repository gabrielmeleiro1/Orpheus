"""Pipeline ingestion-stage helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.ingestion.models import Document


def _select_configured_chapter_range(document: Any, config: Any) -> Any:
    chapter_start = max(1, int(getattr(config, "chapter_start", 1) or 1))
    chapter_limit = getattr(config, "chapter_limit", None)
    start_index = chapter_start - 1
    if chapter_limit is None and start_index == 0:
        return document

    chapters = list(getattr(document, "chapters", []))
    if chapter_limit is None:
        document.chapters = chapters[start_index:]
    else:
        document.chapters = chapters[start_index : start_index + max(1, int(chapter_limit))]
    return document


def ingest_source(runtime: Any, source_path: Path):
    """Parse source file and extract document structure."""
    suffix = source_path.suffix.lower()
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
        raise ValueError(f"Unsupported file type: {suffix}")

    runtime.log_verbose("[PARSER] Parsing content...", "process")
    document = parser.parse()
    document = _select_configured_chapter_range(document, runtime.config)
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

    num_chapters = len(document.chapters)
    runtime.log_verbose(f"[PARSER] Extracted: {num_chapters} chapters found", "success")

    for i, chapter in enumerate(document.chapters):
        progress_pct = int((i + 1) / num_chapters * 100) if num_chapters > 0 else 0
        runtime.log_verbose(
            f"[PARSER] [{progress_pct:3d}%] Chapter {i + 1}/{num_chapters}: "
            f"{chapter.title[:40]}... ({len(chapter.content)} chars)",
            "info",
        )
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

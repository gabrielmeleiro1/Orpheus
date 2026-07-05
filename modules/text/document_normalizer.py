"""Deterministic document cleanup before TTS chunking."""

from __future__ import annotations

from collections import Counter
from dataclasses import is_dataclass, replace
import logging
import re
import string
from typing import Iterable

from modules.errors import ChapterTooLongError
from modules.ingestion.models import Chapter, Document
from modules.ingestion.content_filter import (
    SkippedSection,
    classify_chapter_content,
    filter_audiobook_chapters,
    serialize_skipped_section,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_CHAPTER_WORDS = 50_000
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,12})\s*$")
_LOWERCASE_HYPHEN_BREAK_RE = re.compile(r"([a-z])-\s*\n\s*([a-z])")
_BACKLINK_RE = re.compile(
    r"(?im)(?:^\s*|\s+)(?:back to (?:top|contents)|return to (?:top|contents))\s*[↩\u21a9]?\s*$"
)
_PUBLISHER_BOILERPLATE_RE = re.compile(
    r"(?im)^\s*(?:publisher boilerplate|copyright page|all rights reserved).*$"
)
_NAV_TITLE_RE = re.compile(r"(?i)\b(?:table of contents|contents|toc|nav(?:igation)?|landmarks?)\b")
_EPUB_NON_NARRATIVE_TITLE_RE = re.compile(
    r"^(?:"
    r"also\s+by(?:\s+.+)?|"
    r"praise(?:\s+for\s+.+)?|"
    r"about\s+the\s+(?:author|publisher)|"
    r"copyright(?:\s+page)?|"
    r"permissions?|credits?|"
    r"bibliography|references|notes?|endnotes?|"
    r"index"
    r")$",
    re.IGNORECASE,
)


def _normalize_repeated_candidate(line: str) -> str:
    lowered = line.lower().strip()
    lowered = lowered.translate(str.maketrans("", "", string.punctuation))
    lowered = re.sub(r"\d+", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _iter_lines(chapters: Iterable[Chapter]) -> Iterable[str]:
    for chapter in chapters:
        yield from str(chapter.content).splitlines()


def _detect_repeated_headers(chapters: list[Chapter]) -> set[str]:
    candidates: list[str] = []
    for line in _iter_lines(chapters):
        stripped = line.strip()
        if not 4 <= len(stripped) <= 120:
            continue
        normalized = _normalize_repeated_candidate(stripped)
        if normalized and not normalized.startswith("chapter "):
            candidates.append(normalized)

    if not candidates:
        return set()

    counts = Counter(candidates)
    threshold = max(2, int(len(chapters) * 0.30))
    dominant_body_limit = max(10, int(len(candidates) * 0.50))
    return {
        candidate
        for candidate, count in counts.items()
        if threshold <= count <= dominant_body_limit
    }


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r"(?i)^(?:chapter|book|part|section)\b", stripped))


def _reflow_lines(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    reflowed: list[str] = []
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        current: list[str] = []
        blocks: list[str] = []
        for line in lines:
            if _is_heading(line) or re.match(r"^[*#\-]\s+", line) or line.startswith(("\"", "'", "“", "‘")):
                if current:
                    blocks.append(" ".join(current))
                    current = []
                blocks.append(line)
                continue
            current.append(line)
        if current:
            blocks.append(" ".join(current))
        reflowed.append("\n".join(blocks))
    return "\n\n".join(reflowed)


def _normalize_pdf_chapter(text: str, repeated_headers: set[str]) -> str:
    kept_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept_lines.append("")
            continue
        if _PAGE_NUMBER_RE.fullmatch(stripped):
            continue
        normalized = _normalize_repeated_candidate(stripped)
        if normalized in repeated_headers:
            continue
        kept_lines.append(line.rstrip())

    cleaned = "\n".join(kept_lines)
    cleaned = _LOWERCASE_HYPHEN_BREAK_RE.sub(r"\1\2", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = _reflow_lines(cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _normalize_epub_chapter(text: str) -> str:
    cleaned = _BACKLINK_RE.sub("", text)
    cleaned = _PUBLISHER_BOILERPLATE_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _is_epub_non_narrative_chapter(title: str, content: str) -> bool:
    normalized_title = re.sub(r"\s+", " ", title).strip().strip(" .:-–—")
    if _NAV_TITLE_RE.search(normalized_title) and len(content) < 2000:
        return True
    return bool(_EPUB_NON_NARRATIVE_TITLE_RE.match(normalized_title))


def _normalize_text_like_chapter(text: str, *, markdown: bool = False) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    if markdown:
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()
    cleaned = _LOWERCASE_HYPHEN_BREAK_RE.sub(r"\1\2", cleaned)
    cleaned = _reflow_lines(cleaned)
    return cleaned.strip()


def _renumber(chapters: list[Chapter]) -> list[Chapter]:
    renumbered: list[Chapter] = []
    for index, chapter in enumerate(chapters, start=1):
        renumbered.append(replace(chapter, number=index))
    return renumbered


def _paragraphs_for_split(content: str) -> list[str]:
    return [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", content) if paragraph.strip()]


def _split_overlong_chapter(chapter: Chapter, *, max_chapter_words: int) -> list[Chapter]:
    word_count = len(str(chapter.content).split())
    if word_count <= max_chapter_words:
        return [chapter]

    paragraphs = _paragraphs_for_split(str(chapter.content))
    if not paragraphs:
        return [chapter]

    for paragraph in paragraphs:
        paragraph_word_count = len(paragraph.split())
        if paragraph_word_count > max_chapter_words:
            raise ChapterTooLongError(chapter.number, paragraph_word_count, max_chapter_words)

    parts: list[str] = []
    current: list[str] = []
    current_words = 0
    for paragraph in paragraphs:
        paragraph_words = len(paragraph.split())
        if current and current_words + paragraph_words > max_chapter_words:
            parts.append("\n\n".join(current))
            current = [paragraph]
            current_words = paragraph_words
        else:
            current.append(paragraph)
            current_words += paragraph_words
    if current:
        parts.append("\n\n".join(current))

    if len(parts) <= 1:
        return [chapter]

    LOGGER.info(
        "Split chapter %s (%s words) into %s parts at paragraph boundaries",
        chapter.number,
        word_count,
        len(parts),
    )
    return [
        replace(chapter, title=f"{chapter.title} (part {index})", content=part)
        for index, part in enumerate(parts, start=1)
    ]


def normalize_extracted_document(
    document: Document,
    *,
    source_type: str,
    skip_non_prose_sections: bool = True,
    max_chapter_words: int = DEFAULT_MAX_CHAPTER_WORDS,
) -> Document:
    """Return a copy of ``document`` after parser-specific deterministic cleanup.

    Chapters above ``max_chapter_words`` are split at paragraph boundaries
    before downstream chunking/validation. A single paragraph above the cap
    remains pathological and raises ``ChapterTooLongError``.
    """
    if not is_dataclass(document):
        return document

    source_type = source_type.lower().lstrip(".")
    chapters = list(document.chapters)
    repeated_headers = _detect_repeated_headers(chapters) if source_type == "pdf" else set()
    skipped_sections: list[SkippedSection] = []

    normalized_chapters: list[Chapter] = []
    for chapter in chapters:
        if not is_dataclass(chapter):
            return document
        title = str(chapter.title).strip()
        content = str(chapter.content)
        if skip_non_prose_sections:
            classification = classify_chapter_content(title, content)
            if classification.should_skip:
                skipped_sections.append(
                    SkippedSection(
                        original_number=chapter.number,
                        title=title,
                        reason=classification.reason or "non_prose",
                        confidence=classification.confidence,
                        chars=len(content),
                    )
                )
                continue
        if source_type == "epub" and _is_epub_non_narrative_chapter(title, content):
            skipped_sections.append(
                SkippedSection(
                    original_number=chapter.number,
                    title=title,
                    reason="epub_non_narrative",
                    confidence=0.95,
                    chars=len(content),
                )
            )
            continue
        if source_type == "pdf":
            content = _normalize_pdf_chapter(content, repeated_headers)
        elif source_type == "epub":
            content = _normalize_epub_chapter(content)
        elif source_type == "md":
            content = _normalize_text_like_chapter(content, markdown=True)
        else:
            content = _normalize_text_like_chapter(content)
        if content.strip():
            normalized_chapter = replace(chapter, content=content)
            normalized_chapters.extend(
                _split_overlong_chapter(
                    normalized_chapter,
                    max_chapter_words=max_chapter_words,
                )
            )

    renumbered_chapters = _renumber(normalized_chapters)
    if skip_non_prose_sections:
        retained_chapters, filtered_skipped = filter_audiobook_chapters(renumbered_chapters)
        skipped_sections.extend(filtered_skipped)
        renumbered_chapters = retained_chapters

    return replace(
        document,
        chapters=renumbered_chapters,
        skipped_non_prose_sections=[
            serialize_skipped_section(section) for section in skipped_sections
        ],
    )

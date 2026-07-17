"""Conservative non-prose classifier for audiobook chapter ingestion."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, is_dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ContentClassification:
    should_skip: bool
    reason: str | None
    confidence: float


@dataclass(frozen=True)
class SkippedSection:
    original_number: Any
    title: str
    reason: str
    confidence: float
    chars: int


def serialize_skipped_section(section: SkippedSection) -> dict[str, object]:
    return {
        "original_number": section.original_number,
        "title": section.title,
        "reason": section.reason,
        "confidence": section.confidence,
        "chars": section.chars,
    }


_INDEX_TITLE_RE = re.compile(r"\b(?:index|name index|subject index)\b", re.IGNORECASE)
_REFERENCES_TITLE_RE = re.compile(r"\b(?:bibliography|references|works cited)\b", re.IGNORECASE)
_TOC_TITLE_RE = re.compile(r"\b(?:contents|table of contents)\b", re.IGNORECASE)
_COPYRIGHT_TITLE_RE = re.compile(r"\b(?:copyright|legal|imprint)\b", re.IGNORECASE)
_MAP_LIST_TITLE_RE = re.compile(
    r"^\s*(?:list\s+of\s+)?(?:maps|figures|illustrations)\s*$",
    re.IGNORECASE,
)

_INDEX_LINE_RE = re.compile(
    r"^[A-Z][A-Za-z' -]{2,}(?:,\s+[A-Z][A-Za-z' -]+)?\s+"
    r"(?:\d{1,4}|[ivxlcdm]{1,10})(?:\s*[-,]\s*(?:\d{1,4}|[ivxlcdm]{1,10}))*\.?$",
    re.IGNORECASE,
)
_INDEX_LOCATOR_RE = re.compile(
    r"\b(?:\d{1,4}|[ivxlcdm]{1,10})(?:\s*[-–,]\s*(?:\d{1,4}|[ivxlcdm]{1,10}))+\b",
    re.IGNORECASE,
)
_TOC_LINE_RE = re.compile(
    r"^\s*[A-Za-z][A-Za-z0-9 ,'\":;-]{2,}?\s*(?:\.{3,}|…+)\s*"
    r"(?:\d{1,4}|[ivxlcdm]{1,10})\s*$",
    re.IGNORECASE,
)
_BARE_TOC_LINE_RE = re.compile(
    r"^\s*(?:chapter\s+\d{1,3}|prologue|epilogue|foreword|preface|introduction)\b"
    r".{0,80}\s+\d{1,4}\s*$",
    re.IGNORECASE,
)
_CITATION_LINE_RE = re.compile(
    r"(?:\(\d{4}[a-z]?\)|\b(?:1[5-9]\d{2}|20\d{2})\b|"
    r"\b(?:journal|press|university|doi:|https?://|isbn)\b|"
    r"\b\d+\(\d+\),\s*\d+[-–]\d+)",
    re.IGNORECASE,
)
_COPYRIGHT_TERMS = (
    "copyright",
    "all rights reserved",
    "no part of this publication",
    "isbn",
    "published by",
    "publisher",
    "permissions",
    "reproduced or transmitted",
)
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_ALPHA_RE = re.compile(r"[A-Za-z]")
_NUMBER_ONLY_RE = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdm]{1,10}|[A-Z])\s*$", re.IGNORECASE)
# Real copyright / imprint pages are front matter of a few hundred words. A
# longer chapter that merely discusses publishing (publishers, ISBNs,
# permissions) is narrative prose and must never be deleted on term hits alone.
_MAX_COPYRIGHT_WORDS = 450


def classify_chapter_content(
    title: str,
    content: str,
    *,
    enable_low_prose_density: bool = True,
) -> ContentClassification:
    title_text = title or ""
    lines = _content_lines(content)

    if _is_copyright(title_text, content):
        return ContentClassification(True, "copyright", 0.95)
    if _is_table_of_contents(title_text, lines):
        return ContentClassification(True, "table_of_contents", 0.95)
    if _is_map_list(title_text, lines):
        return ContentClassification(True, "list_of_maps", 0.93)
    if _is_index(title_text, lines):
        return ContentClassification(True, "index", 0.95)
    if _is_references(title_text, lines):
        return ContentClassification(True, "references", 0.92)
    if enable_low_prose_density and _is_low_prose_density(lines):
        return ContentClassification(True, "low_prose_density", 0.88)

    return ContentClassification(False, None, 0.2)


def is_verse_like(content: str) -> bool:
    """True when a chapter has the short-line shape of verse/poetry.

    Exposed so document-level callers can detect verse-heavy books (poetry
    collections) and disable the low-prose-density rule for them.
    """
    return _is_low_prose_density(_content_lines(content))


def filter_audiobook_chapters(
    chapters: list[Any],
    *,
    enable_low_prose_density: bool = True,
) -> tuple[list[Any], list[SkippedSection]]:
    retained: list[Any] = []
    skipped: list[SkippedSection] = []

    for chapter in chapters:
        title = str(getattr(chapter, "title", "") or "")
        content = str(getattr(chapter, "content", "") or "")
        classification = classify_chapter_content(
            title, content, enable_low_prose_density=enable_low_prose_density
        )

        if classification.should_skip:
            skipped.append(
                SkippedSection(
                    original_number=getattr(chapter, "number", None),
                    title=title,
                    reason=classification.reason or "non_prose",
                    confidence=classification.confidence,
                    chars=len(content),
                )
            )
            continue

        retained.append(_renumber_chapter(chapter, len(retained) + 1))

    return retained, skipped


def _content_lines(content: str) -> list[str]:
    return [line.strip() for line in content.splitlines() if line.strip()]


def _has_heading(title: str, lines: list[str], pattern: re.Pattern[str]) -> bool:
    heading_text = "\n".join([title, *lines[:8]])
    return bool(pattern.search(heading_text))


def _is_index(title: str, lines: list[str]) -> bool:
    if len(lines) < 4 or not _has_heading(title, lines, _INDEX_TITLE_RE):
        return False
    matching = sum(
        1
        for line in lines
        if _INDEX_LINE_RE.match(line) or _INDEX_LOCATOR_RE.search(line)
    )
    starts = [line[:1].upper() for line in lines if _ALPHA_RE.match(line)]
    alphabetic_progression = starts == sorted(starts) and len(set(starts)) >= 3
    locator_density = matching / len(lines)
    sentence_like = sum(1 for line in lines if re.search(r"[.!?]$", line))
    low_sentence_density = sentence_like / len(lines) <= 0.25
    return (
        matching >= 3
        and locator_density >= 0.25
        and (alphabetic_progression or low_sentence_density)
    )


def _is_references(title: str, lines: list[str]) -> bool:
    if len(lines) < 3 or not _has_heading(title, lines, _REFERENCES_TITLE_RE):
        return False
    matching = sum(1 for line in lines if _CITATION_LINE_RE.search(line))
    citation_density = matching / len(lines)
    return matching >= 3 and (citation_density >= 0.30 or matching >= 12)


def _is_table_of_contents(title: str, lines: list[str]) -> bool:
    if len(lines) < 3 or not _has_heading(title, lines, _TOC_TITLE_RE):
        return False
    matching = sum(1 for line in lines if _TOC_LINE_RE.match(line) or _BARE_TOC_LINE_RE.match(line))
    return matching / len(lines) >= 0.6


def _is_map_list(title: str, lines: list[str]) -> bool:
    if not _MAP_LIST_TITLE_RE.search(title):
        return False
    if len(lines) <= 12:
        sentence_like = sum(1 for line in lines if re.search(r"[.!?]$", line))
        return sentence_like == 0
    return _is_low_prose_density(lines)


def _is_copyright(title: str, content: str) -> bool:
    # Length gate first: a chapter longer than a real copyright page is prose
    # about publishing, not a copyright page, no matter how many bibliographic
    # terms it mentions.
    if len(content.split()) > _MAX_COPYRIGHT_WORDS:
        return False
    normalized = f"{title}\n{content}".lower()
    hits = sum(1 for term in _COPYRIGHT_TERMS if term in normalized)
    return bool(_COPYRIGHT_TITLE_RE.search(title) and hits >= 2) or hits >= 4


def _is_low_prose_density(lines: list[str]) -> bool:
    if len(lines) < 8:
        return False

    short_or_numeric = sum(1 for line in lines if len(line) <= 16 or _NUMBER_ONLY_RE.match(line))
    sentence_like = sum(1 for line in lines if len(_WORD_RE.findall(line)) >= 6 and re.search(r"[.!?]$", line))
    word_count = sum(len(_WORD_RE.findall(line)) for line in lines)

    return short_or_numeric / len(lines) >= 0.75 and sentence_like <= 1 and word_count / len(lines) < 4


def _renumber_chapter(chapter: Any, number: int) -> Any:
    if is_dataclass(chapter) and not isinstance(chapter, type):
        return replace(chapter, number=number)

    clone = copy.copy(chapter)
    setattr(clone, "number", number)
    return clone

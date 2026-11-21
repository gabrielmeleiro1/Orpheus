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
_COPYRIGHT_NOTICE_MARKERS = (
    "copyright",
    "all rights reserved",
    "no part of this publication may be reproduced",
    "isbn",
)
# Markdown marks and folios around a heading ("## Notes", "194   References").
_HEADING_DECORATION_RE = re.compile(r"^(?:#{1,6}\s+)?(?:\d{1,4}\s+)?|\s+\d{1,4}$")
_MAX_REFERENCES_HEADING_WORDS = 6
# A long bibliography with wrapped entries may fall below the 30% density, but
# dated narrative prose with a citation-like line every few paragraphs is not one.
_MIN_LONG_REFERENCES_DENSITY = 0.15
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
    lines = _content_lines(content)
    structural = _structural_classification(title or "", content, lines)
    if structural is not None:
        return structural
    if enable_low_prose_density and _is_low_prose_density(lines):
        return _LOW_PROSE_CLASSIFICATION
    return _PROSE_CLASSIFICATION


_LOW_PROSE_CLASSIFICATION = ContentClassification(True, "low_prose_density", 0.88)
_PROSE_CLASSIFICATION = ContentClassification(False, None, 0.2)


def _structural_classification(
    title: str, content: str, lines: list[str]
) -> ContentClassification | None:
    """Return the back-matter verdict, or None when no structural rule matches."""
    if _is_copyright(title, content):
        return ContentClassification(True, "copyright", 0.95)
    if _is_table_of_contents(title, lines):
        return ContentClassification(True, "table_of_contents", 0.95)
    if _is_map_list(title, lines):
        return ContentClassification(True, "list_of_maps", 0.93)
    if _is_index(title, lines):
        return ContentClassification(True, "index", 0.95)
    if _is_references(title, lines):
        return ContentClassification(True, "references", 0.92)
    return None


class ChapterClassifier:
    """``classify_chapter_content`` memoised for one document's cleanup.

    Normalization asks about the same chapter several times (verse detection,
    prose-body detection, the per-chapter filter and the post-normalization
    filter), with different low-prose flags. The structural verdict depends
    only on ``(title, content)`` and the low-prose rule only on ``content``,
    so each is computed at most once per distinct input; every answer is the
    one ``classify_chapter_content`` would give. Create one per document so
    chapter text is not retained beyond that call.
    """

    def __init__(self) -> None:
        self._structural: dict[tuple[str, str], ContentClassification | None] = {}
        self._low_prose: dict[str, bool] = {}

    def classify(
        self,
        title: str,
        content: str,
        *,
        enable_low_prose_density: bool = True,
    ) -> ContentClassification:
        key = (title or "", content)
        if key in self._structural:
            structural = self._structural[key]
        else:
            structural = _structural_classification(key[0], content, _content_lines(content))
            self._structural[key] = structural
        if structural is not None:
            return structural
        if enable_low_prose_density and self.is_verse_like(content):
            return _LOW_PROSE_CLASSIFICATION
        return _PROSE_CLASSIFICATION

    def is_verse_like(self, content: str) -> bool:
        cached = self._low_prose.get(content)
        if cached is None:
            cached = is_verse_like(content)
            self._low_prose[content] = cached
        return cached


def is_copyright_notice(text: str) -> bool:
    """True when text carries a publisher's copyright notice (front matter)."""
    normalized = re.sub(r"\s+", " ", text).casefold()
    return (
        "copyright" in normalized
        and sum(1 for marker in _COPYRIGHT_NOTICE_MARKERS if marker in normalized) >= 2
    )


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
    classifier: ChapterClassifier | None = None,
) -> tuple[list[Any], list[SkippedSection]]:
    retained: list[Any] = []
    skipped: list[SkippedSection] = []
    classify = classifier.classify if classifier is not None else classify_chapter_content

    for chapter in chapters:
        title = str(getattr(chapter, "title", "") or "")
        content = str(getattr(chapter, "content", "") or "")
        classification = classify(
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


def _is_references_heading_line(line: str) -> bool:
    """A short standalone heading naming the section ("Notes and References").

    A prose line that mentions references opens lowercase, runs long or ends
    a sentence, so it is not a heading.
    """
    heading = _HEADING_DECORATION_RE.sub("", line).strip()
    return (
        heading[:1].isupper()
        and len(heading.split()) <= _MAX_REFERENCES_HEADING_WORDS
        and not heading.endswith((".", "!", "?", "\u2026", ","))
        and bool(_REFERENCES_TITLE_RE.search(heading))
    )


def _has_citation_syntax(line: str) -> bool:
    """A citation token other than a bare year, which dated prose also has."""
    return any(not match.group().isdigit() for match in _CITATION_LINE_RE.finditer(line))


def _is_citation_line(line: str) -> bool:
    """Citation syntax, or a bare year where entries put it: closing the line.

    "Riley, E. C. Cervantes's Theory. Oxford, 1962." ends on its year; dated
    prose ("In 1703 the fleet left harbour") mostly does not.
    """
    if _has_citation_syntax(line):
        return True
    tail = " ".join(line.split()[-3:])
    return bool(_CITATION_LINE_RE.search(tail))


def _is_references(title: str, lines: list[str]) -> bool:
    if len(lines) < 3 or not (
        _REFERENCES_TITLE_RE.search(title)
        or any(_is_references_heading_line(line) for line in lines[:8])
    ):
        return False
    matching = sum(1 for line in lines if _is_citation_line(line))
    citation_density = matching / len(lines)
    if matching >= 3 and citation_density >= 0.30:
        return True
    # Below that density only lines with real citation syntax count: a
    # dated narrative chapter has years in many paragraphs but no presses,
    # journals, DOIs or "(1998)" references.
    return (
        matching >= 12
        and citation_density >= _MIN_LONG_REFERENCES_DENSITY
        and sum(1 for line in lines if _has_citation_syntax(line)) >= 12
    )


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
    if _COPYRIGHT_TITLE_RE.search(title) and hits >= 2:
        return True
    return hits >= 4 and _looks_like_copyright_page(content)


def _looks_like_copyright_page(content: str) -> bool:
    """Require page-like structure before term-only copyright filtering."""
    lines = _content_lines(content)
    if len(lines) < 3:
        return False

    term_lines = sum(
        1 for line in lines if any(term in line.lower() for term in _COPYRIGHT_TERMS)
    )
    sentence_like = sum(
        1
        for line in lines
        if len(_WORD_RE.findall(line)) >= 8 and re.search(r"[.!?]$", line)
    )
    return (
        term_lines >= 3
        and term_lines / len(lines) >= 0.5
        and sentence_like / len(lines) <= 0.4
    )


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

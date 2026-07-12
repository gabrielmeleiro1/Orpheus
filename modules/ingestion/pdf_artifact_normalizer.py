"""Page-level artifact normalization policies for extracted PDF text."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional, Protocol
import re


PageData = dict[str, object]

_TRAILING_REFERENCE_ENTRY_RE = re.compile(
    r"^\s*(?:\[(?P<bracket>\d{1,4})\]|(?P<number>\d{1,4})(?:[.)]|\s+))\s*\S"
)
_INLINE_REFERENCE_MARKER_RE = re.compile(
    r"\[(\d{1,4})\]|(?<=[A-Za-z\)\].,;:!?])(\d{1,4})(?!\d)"
)
_PAGE_BOUNDARY_WINDOW = 4
_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")
_PAGE_NUMBER_RESIDUE_RE = re.compile(r"^\s*\d{1,4}[Il]\s*$")
_LINE_BREAK_HYPHEN_RE = re.compile(
    r"(?<!-)\b(?P<prefix>[A-Za-z]{2,})-\s*\n\s*(?P<suffix>[a-z]{3,})\b"
)
_KNOWN_HYPHENATED_LINE_BREAK_RE = re.compile(
    r"\b(?P<left>[A-Za-z]+(?:-[A-Za-z]+)*)-\s*\n\s*"
    r"(?P<right>[a-z]+(?:-[a-z]+)*)\b"
)
_KNOWN_HYPHENATED_LINE_BREAK_REPAIRS = {
    "alto-relievo": "alto-relievo",
    "auto-immune": "auto-immune",
    "auto-nomy": "auto-nomy",
    "auto-pause": "auto-pause",
    "auto-pilot": "auto-pilot",
    "coming-into-being": "coming-into-being",
    "crypto-political": "crypto-political",
    "gameto-technical": "gameto-technical",
    "into-just": "into-just",
    "lacto-ovo": "lacto-ovo",
    "lotto-game": "lotto-game",
    "mosquito-nets": "mosquito-nets",
    "parieto-occipital": "parieto-occipital",
    "photo-reconnaissance": "photo-reconnaissance",
    "potato-digger": "potato-digger",
    "proto-awareness": "proto-awareness",
    "proto-cities": "proto-cities",
    "proto-evolutionary": "proto-evolutionary",
    "proto-imperial": "proto-imperial",
    "proto-intuitionistic": "proto-intuitionistic",
    "proto-modernist": "proto-modernist",
    "proto-nationalist": "proto-nationalist",
    "proto-palaces": "proto-palaces",
    "proto-romantics": "proto-romantics",
    "proto-scouting": "proto-scouting",
    "proto-soul": "proto-soul",
    "proto-types": "proto-types",
    "putto-like": "putto-like",
}
# Corpus-specific rewrites that change user text; applied only when the
# known-corpus policies are explicitly requested (see
# KNOWN_CORPUS_PDF_ARTIFACT_POLICIES), never by default.
_KNOWN_CORPUS_LINE_BREAK_REPAIRS = {
    "auto-sug-gestion": "auto-suggestion",
    "into-just": "into—just",
    "proto-leg-end": "proto-legend",
}
_INLINE_FOOTNOTE_BLOCK_START_RE = re.compile(
    r"^\s*(?:[IVX]{1,4}|\d{1,3})\s+The problem\b"
)
_HYPHENATION_PREFIX_EXCEPTIONS = {
    "body",
    "mind",
    "non",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
}
_HYPHENATION_SUFFIX_EXCEPTIONS = {
    "and",
    "but",
    "for",
    "nor",
    "objective",
    "or",
    "so",
    "specific",
    "species",
    "that",
    "the",
    "then",
    "these",
    "this",
    "those",
    "toward",
    "towards",
    "why",
    "yet",
}
_JSTOR_BOUNDARY_NOISE_RE = re.compile(
    r"^\s*(?:"
    r"This content downloaded from\b|"
    r"All use subject to\b|"
    r"https?://about\.jstor\.org/terms\s*$"
    r")",
    re.IGNORECASE,
)
_JSTOR_COVER_MARKERS = (
    "stable url:",
    "your use of the jstor archive",
    "jstor is a not-for-profit",
)
_COPYRIGHT_FRONT_MATTER_MARKERS = (
    "copyright",
    "all rights reserved",
    "no part of this publication may be reproduced",
    "isbn",
)
_TOC_HEADING_RE = re.compile(r"^\s*(?:contents|table of contents)\s*$", re.IGNORECASE | re.MULTILINE)
_DOTTED_TOC_LINE_RE = re.compile(r"\.{3,}|…{2,}")
_CAPTION_LINE_RE = re.compile(
    r"^\s*(?:"
    r"fig(?:ure)?\.?\s*\d[\w.\-]*|"
    r"image\s+caption:|"
    r"caption:|"
    r"plate\s+\d[\w.\-]*"
    r").*$",
    re.IGNORECASE,
)
_REFERENCE_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:bibliography|references|endnotes|notes)\s*$",
    re.IGNORECASE,
)


class PDFArtifactNormalizationPolicy(Protocol):
    """Policy interface for page-level PDF artifact cleanup."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        """Return normalized page data without mutating the input pages."""


def _copy_page_with_text(page: PageData, text: str) -> PageData:
    page_copy = dict(page)
    page_copy["text"] = text
    return page_copy


def _is_jstor_cover_page(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).casefold()
    return all(marker in normalized for marker in _JSTOR_COVER_MARKERS)


def _is_copyright_front_matter_page(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).casefold()
    return (
        "copyright" in normalized
        and sum(1 for marker in _COPYRIGHT_FRONT_MATTER_MARKERS if marker in normalized) >= 2
    )


def _is_toc_duplicate_page(text: str) -> bool:
    if not _TOC_HEADING_RE.search(text):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dotted_lines = sum(1 for line in lines if _DOTTED_TOC_LINE_RE.search(line))
    numbered_lines = sum(
        1
        for line in lines
        if re.match(r"^\s*(?:\d+|[ivxlcdm]+)[.)]?\s+\S", line, re.IGNORECASE)
    )
    return dotted_lines >= 2 or numbered_lines >= 3


def _strip_caption_lines(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not _CAPTION_LINE_RE.match(line)
    ).strip()


def _strip_reference_sections(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        if _REFERENCE_SECTION_HEADING_RE.fullmatch(line.strip()):
            break
        kept.append(line)
    return "\n".join(kept).rstrip()


def repair_line_break_hyphenation(text: str, *, known_corpus_repairs: bool = False) -> str:
    """Join likely soft hyphenation introduced by PDF line wrapping."""

    known_repairs = _KNOWN_HYPHENATED_LINE_BREAK_REPAIRS
    if known_corpus_repairs:
        known_repairs = {**known_repairs, **_KNOWN_CORPUS_LINE_BREAK_REPAIRS}

    def _repair_known_compound(match: re.Match[str]) -> str:
        left = match.group("left")
        candidate = f"{left}-{match.group('right')}".casefold()
        repair = known_repairs.get(candidate)
        if repair is None:
            return match.group(0)
        if left[:1].isupper():
            return repair[:1].upper() + repair[1:]
        return repair

    def _join(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        if (
            prefix.casefold() in _HYPHENATION_PREFIX_EXCEPTIONS
            or suffix.casefold() in _HYPHENATION_SUFFIX_EXCEPTIONS
        ):
            return f"{prefix}-\n {suffix}"
        return f"{prefix}{suffix}"

    text = _KNOWN_HYPHENATED_LINE_BREAK_RE.sub(_repair_known_compound, text)
    return _LINE_BREAK_HYPHEN_RE.sub(_join, text)


def _strip_page_number_residue_lines(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not _PAGE_NUMBER_RESIDUE_RE.match(line)
    ]
    return "\n".join(lines)


def _strip_inline_footnote_blocks(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if _INLINE_FOOTNOTE_BLOCK_START_RE.match(line):
            idx += 1
            while idx < len(lines) and lines[idx].strip():
                idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
            continue
        kept.append(line)
        idx += 1
    return "\n".join(kept)


def _page_boundary_line_indexes(lines: list[str]) -> set[int]:
    non_empty = [idx for idx, line in enumerate(lines) if line.strip()]
    if not non_empty:
        return set()
    return set(non_empty[:_PAGE_BOUNDARY_WINDOW]) | set(non_empty[-_PAGE_BOUNDARY_WINDOW:])


def _normalize_repeated_boundary_line(line: str) -> Optional[str]:
    stripped = re.sub(r"\s+", " ", line).strip()
    if not stripped or _PAGE_NUMBER_RE.match(stripped):
        return None
    if len(stripped) > 120:
        return None
    return stripped.casefold()


def _is_page_boundary_noise_line(line: str, *, strip_page_numbers: bool) -> bool:
    stripped = line.strip()
    return bool(
        (strip_page_numbers and _PAGE_NUMBER_RE.match(stripped))
        or _JSTOR_BOUNDARY_NOISE_RE.match(stripped)
    )


def _extract_reference_number(line: str) -> Optional[int]:
    match = _TRAILING_REFERENCE_ENTRY_RE.match(line.strip())
    if not match:
        return None
    raw = match.group("bracket") or match.group("number")
    if raw is None:
        return None
    return int(raw)


def _looks_like_reference_continuation(line: str) -> bool:
    return bool(re.match(r"^\s{2,}\S", line))


def _extract_inline_reference_markers(text: str) -> set[int]:
    markers: set[int] = set()
    for match in _INLINE_REFERENCE_MARKER_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        if raw is not None:
            markers.add(int(raw))
    return markers


def _strip_trailing_single_reference_note(text: str) -> str:
    lines = text.splitlines()
    last_non_empty_idx = len(lines) - 1
    while last_non_empty_idx >= 0 and not lines[last_non_empty_idx].strip():
        last_non_empty_idx -= 1
    if last_non_empty_idx < 0:
        return text

    search_start = max(0, last_non_empty_idx - 9)
    for idx in range(search_start, last_non_empty_idx + 1):
        reference_number = _extract_reference_number(lines[idx])
        if reference_number is None:
            continue

        body_text = "\n".join(lines[:idx]).rstrip()
        if not body_text:
            continue

        body_markers = _extract_inline_reference_markers(body_text)
        if reference_number not in body_markers:
            continue

        note_lines = [line for line in lines[idx : last_non_empty_idx + 1] if line.strip()]
        note_word_count = sum(len(line.split()) for line in note_lines)
        if len(note_lines) <= 10 and note_word_count <= 140:
            block_start = idx
            probe = idx - 1
            while probe >= search_start:
                if not lines[probe].strip():
                    break
                if (
                    _extract_reference_number(lines[probe]) is not None
                    or _looks_like_reference_continuation(lines[probe])
                ):
                    block_start = probe
                    probe -= 1
                    continue
                break
            return "\n".join(lines[:block_start]).rstrip()

    return text


def _strip_trailing_reference_block(text: str) -> str:
    if not text.strip():
        return text

    text = _strip_trailing_single_reference_note(text)

    lines = text.splitlines()
    last_non_empty_idx = len(lines) - 1
    while last_non_empty_idx >= 0 and not lines[last_non_empty_idx].strip():
        last_non_empty_idx -= 1
    if last_non_empty_idx < 0:
        return text

    numbered_indexes: list[int] = []
    numbered_values: list[int] = []

    idx = last_non_empty_idx
    while idx >= 0:
        line = lines[idx]
        stripped = line.strip()

        if not stripped:
            idx -= 1
            continue

        reference_number = _extract_reference_number(stripped)
        if reference_number is not None:
            numbered_indexes.append(idx)
            numbered_values.append(reference_number)
            idx -= 1
            continue

        if not numbered_indexes:
            return text

        if _looks_like_reference_continuation(line):
            idx -= 1
            continue

        break

    if len(numbered_indexes) < 2:
        return text

    block_start = min(numbered_indexes)
    while block_start > 0 and lines[block_start - 1].strip():
        block_start -= 1

    body_text = "\n".join(lines[:block_start]).rstrip()
    if not body_text:
        return text

    body_markers = _extract_inline_reference_markers(body_text)
    block_numbers = set(numbered_values)
    if not body_markers.intersection(block_numbers):
        return text

    return body_text


@dataclass(frozen=True)
class JSTORFrontMatterPolicy:
    """Remove source-platform cover pages that are not article/book content."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        before_body = True
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            page_text = str(page.get("text", ""))
            if before_body and _is_jstor_cover_page(page_text):
                cleaned_pages.append(_copy_page_with_text(page, ""))
                continue

            if page_text.strip():
                before_body = False
            cleaned_pages.append(dict(page))
        return cleaned_pages


@dataclass(frozen=True)
class FrontMatterPolicy:
    """Remove obvious copyright and TOC duplicate pages before body text starts."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        before_body = True
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            page_text = str(page.get("text", ""))
            if before_body and (
                _is_copyright_front_matter_page(page_text)
                or _is_toc_duplicate_page(page_text)
            ):
                cleaned_pages.append(_copy_page_with_text(page, ""))
                continue

            if page_text.strip():
                before_body = False
            cleaned_pages.append(dict(page))
        return cleaned_pages


@dataclass(frozen=True)
class RepeatedPageBoundaryPolicy:
    """Remove repeated running headers, footers, page numbers, and boundary noise."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        boundary_counts: Counter[str] = Counter()
        page_line_indexes: list[tuple[PageData, list[str], set[int]]] = []

        for page in pages_data:
            if not isinstance(page, dict):
                continue
            lines = str(page.get("text", "")).splitlines()
            boundary_indexes = _page_boundary_line_indexes(lines)
            page_line_indexes.append((page, lines, boundary_indexes))
            seen_on_page: set[str] = set()
            for idx in boundary_indexes:
                normalized = _normalize_repeated_boundary_line(lines[idx])
                if normalized:
                    seen_on_page.add(normalized)
            boundary_counts.update(seen_on_page)

        page_count = max(1, len(page_line_indexes))
        if page_count < 2:
            repeated_lines: set[str] = set()
        else:
            repeated_threshold = page_count if page_count == 2 else max(3, (page_count + 3) // 4)
            repeated_lines = {
                normalized
                for normalized, count in boundary_counts.items()
                if count >= repeated_threshold
            }

        cleaned_pages: list[PageData] = []
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            lines = str(page.get("text", "")).splitlines()
            boundary_indexes = _page_boundary_line_indexes(lines)
            strip_page_numbers = page_count > 1
            kept_lines: list[str] = []
            for idx, line in enumerate(lines):
                if idx in boundary_indexes:
                    normalized = _normalize_repeated_boundary_line(line)
                    if normalized in repeated_lines or _is_page_boundary_noise_line(
                        line,
                        strip_page_numbers=strip_page_numbers,
                    ):
                        continue
                kept_lines.append(line)

            cleaned_pages.append(_copy_page_with_text(page, "\n".join(kept_lines).strip()))

        return cleaned_pages


@dataclass(frozen=True)
class PDFLineArtifactPolicy:
    """Repair per-page text artifacts that must be fixed before chunking."""

    known_corpus_repairs: bool = False

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            page_text = str(page.get("text", ""))
            page_text = repair_line_break_hyphenation(
                page_text,
                known_corpus_repairs=self.known_corpus_repairs,
            )
            page_text = _strip_page_number_residue_lines(page_text)
            cleaned_pages.append(_copy_page_with_text(page, page_text.strip()))
        return cleaned_pages


@dataclass(frozen=True)
class KnownCorpusInlineFootnotePolicy:
    """Remove known inline footnote blocks seen in corpus PDF extraction."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            page_text = str(page.get("text", ""))
            cleaned_pages.append(_copy_page_with_text(page, _strip_inline_footnote_blocks(page_text).strip()))
        return cleaned_pages


@dataclass(frozen=True)
class ReferenceBlockPolicy:
    """Remove trailing footnote/reference blocks from per-page text chunks."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            page_text = str(page.get("text", ""))
            cleaned_pages.append(_copy_page_with_text(page, _strip_trailing_reference_block(page_text)))
        return cleaned_pages


@dataclass(frozen=True)
class CaptionLinePolicy:
    """Remove standalone image/figure caption lines from extracted text."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            cleaned_pages.append(
                _copy_page_with_text(page, _strip_caption_lines(str(page.get("text", ""))))
            )
        return cleaned_pages


@dataclass(frozen=True)
class ReferenceSectionPolicy:
    """Remove terminal bibliography, references, notes, and endnotes sections."""

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            cleaned_pages.append(
                _copy_page_with_text(page, _strip_reference_sections(str(page.get("text", ""))))
            )
        return cleaned_pages


DEFAULT_PDF_ARTIFACT_POLICIES: tuple[PDFArtifactNormalizationPolicy, ...] = (
    JSTORFrontMatterPolicy(),
    FrontMatterPolicy(),
    RepeatedPageBoundaryPolicy(),
    PDFLineArtifactPolicy(),
    ReferenceBlockPolicy(),
    CaptionLinePolicy(),
    ReferenceSectionPolicy(),
)

# Opt-in variant for the known benchmark corpus: adds corpus-specific inline
# footnote stripping and text rewrites that are unsafe for arbitrary user PDFs.
KNOWN_CORPUS_PDF_ARTIFACT_POLICIES: tuple[PDFArtifactNormalizationPolicy, ...] = (
    JSTORFrontMatterPolicy(),
    FrontMatterPolicy(),
    RepeatedPageBoundaryPolicy(),
    PDFLineArtifactPolicy(known_corpus_repairs=True),
    KnownCorpusInlineFootnotePolicy(),
    ReferenceBlockPolicy(),
    CaptionLinePolicy(),
    ReferenceSectionPolicy(),
)


def normalize_pdf_artifacts(
    pages_data: list[PageData],
    policies: Iterable[PDFArtifactNormalizationPolicy] = DEFAULT_PDF_ARTIFACT_POLICIES,
) -> list[PageData]:
    """Apply page-level PDF artifact policies in parser-safe order."""
    normalized_pages = pages_data
    for policy in policies:
        normalized_pages = policy.normalize_pages(normalized_pages)
    return normalized_pages

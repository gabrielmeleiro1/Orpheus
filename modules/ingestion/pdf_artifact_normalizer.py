"""Page-level artifact normalization policies for extracted PDF text."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional, Protocol
import re

from .content_filter import _CITATION_LINE_RE, is_copyright_notice
from .text_layout import _SUSPENDED_COMPOUND_WORDS
from .text_layout import repair_line_break_hyphenation as repair_wrapped_words
from .text_layout import corroborated_folio_titles, hyphenation_word_forms, numbered_folio_title, strip_page_furniture


PageData = dict[str, object]

_TRAILING_REFERENCE_ENTRY_RE = re.compile(
    r"^\s*(?:\[(?P<bracket>\d{1,4})\]|(?P<number>\d{1,4})(?:[.)]|\s+))\s*\S"
)
# A flattened superscript marker follows punctuation, a closing quote or a
# lowercase word ("source.15", "theory15"). Digits after a separator that
# itself follows a digit belong to a number (times "3:15", decimals "1.2",
# thousands "1,500"), and digits glued to a capital belong to a code ("B12",
# "F150", "COVID19"). The one exception is a marker after a sentence that
# ends on a year ("signed in 1918.7 The"): it must be followed by a new
# sentence or the line end ("1999.99 dollars" and "2019.1 release" are
# numbers), and both strippers count it only when a trailing note entry with
# the same number corroborates it.
_INLINE_REFERENCE_MARKER_RE = re.compile(
    r"\[(\d{1,4})\]"
    r"|(?:(?<=[\)\].,;:!?\"\u201d])(?<!\d[.,:])|(?<=[a-z][a-z]))(\d{1,4})(?!\d)"
    r"|(?<![\d.,$])(?:1[5-9]\d\d|20\d\d)[.!?](\d{1,3})(?=[^\S\n]+[A-Z\"\u201c]|[^\S\n]*(?:\n|$))"
)
_PAGE_BOUNDARY_WINDOW = 4
_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")
_PAGE_NUMBER_RESIDUE_RE = re.compile(r"^\s*\d{1,4}[Il]\s*$")
_LINE_BREAK_HYPHEN_RE = re.compile(
    r"(?<!-)\b(?P<prefix>[A-Za-z]{2,})-\s*\n\s*(?P<suffix>[A-Za-z]+)\b"
)
# The left-hand compound is matched atomically (lookahead capture + backref)
# and its hyphenated segments are bounded, so a hyphen-dense page cannot drive
# the quadratic backtracking the naive ``[A-Za-z]+(?:-[A-Za-z]+)*`` form causes.
# ``run`` keeps the trailing hyphen; the known compounds never
# exceed a couple of hyphenated segments, so the {1,4}/{0,4} bounds are ample.
_KNOWN_HYPHENATED_LINE_BREAK_RE = re.compile(
    r"\b(?=(?P<run>(?:[A-Za-z]+-){1,4}))(?P=run)\s*\n\s*"
    r"(?P<right>[a-z]+(?:-[a-z]+){0,4})\b"
)
_KNOWN_HYPHENATED_LINE_BREAK_COMPOUNDS = frozenset({
    "alto-relievo",
    "auto-immune",
    "auto-nomy",
    "auto-pause",
    "auto-pilot",
    "coming-into-being",
    "crypto-political",
    "gameto-technical",
    "into-just",
    "lacto-ovo",
    "lotto-game",
    "mosquito-nets",
    "parieto-occipital",
    "photo-reconnaissance",
    "potato-digger",
    "proto-awareness",
    "proto-cities",
    "proto-evolutionary",
    "proto-imperial",
    "proto-intuitionistic",
    "proto-modernist",
    "proto-nationalist",
    "proto-palaces",
    "proto-romantics",
    "proto-scouting",
    "proto-soul",
    "proto-types",
    "putto-like",
})
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
# Horizontal whitespace only: a line-anchored ``\s*`` rescans a run of blank
# lines from each of its line starts, which is quadratic on blank-heavy pages.
_TOC_HEADING_RE = re.compile(
    r"^[^\S\n]*(?:contents|table of contents)[^\S\n]*$", re.IGNORECASE | re.MULTILINE,
)
_DOTTED_TOC_LINE_RE = re.compile(r"\.{3,}|…{2,}")
_CAPTION_LABEL_RE = re.compile(r"^\s*(?i:image\s+caption:|caption:)")
# A figure label, with an optional panel label ("(a)"), opens a caption only
# when a separator, a capitalised title or the line end follows it;
# "Figure 2, the ...", "Figure 2 shows" and "Figure 2 (left) shows" are prose.
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*(?i:fig(?:ure)?\.?|plate)\s*\d[\w.\-]*(?:\s*\([^()\s]{1,8}\))?"
    r"(?:\s*[.:\u2014\u2013-](?:\s|$)|\s+[^\s,(a-z]|\s*$)"
)
_MAX_CAPTION_CHARS = 120
_SENTENCE_END_RE = re.compile(r"[.!?:;\u2026\"'\u201d\u2019)\]]\s*$")
_NUMBERED_HEADING_PREFIX_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+")
_TITLE_SMALL_WORDS = frozenset(
    {"a", "an", "the", "and", "or", "of", "by", "for", "in", "on", "to", "at", "from", "with"}
)
# "Surname, Given." / "Surname, G." -- not "However, I ..." prose.
_AUTHOR_ENTRY_RE = re.compile(r"^[A-Z][\w'\u2019\-]+(?:\s[A-Z][\w'\u2019\-]+)?,\s+[A-Z][\w'\u2019\-]*\.")
_NUMBERED_NOTE_RE = re.compile(r"^(?:\[\d{1,4}\]|\d{1,4}[.)])\s+\S")
_REFERENCE_LOCATOR_RE = re.compile(r"\bibid\b|\bpp?\.\s*\d", re.IGNORECASE)
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


def _looks_like_heading(line: str) -> bool:
    """A short Title Case line, optionally numbered ("2.3 Growth of the Cities")."""
    words = _NUMBERED_HEADING_PREFIX_RE.sub("", line).split()
    return 0 < len(words) <= 12 and words[0][:1].isupper() and all(
        word[:1].isupper() or word[:1].isdigit() or word.lower() in _TITLE_SMALL_WORDS
        for word in words
    )


def _is_caption_line(line: str, previous_line: str) -> bool:
    if _CAPTION_LABEL_RE.match(line):
        return True
    # A hard-wrapped sentence can put a figure reference at a line start.
    continues_sentence = (
        bool(previous_line)
        and not _SENTENCE_END_RE.search(previous_line)
        and not _looks_like_heading(previous_line)
    )
    return (
        len(line.strip()) <= _MAX_CAPTION_CHARS
        and not continues_sentence
        and bool(_FIGURE_CAPTION_RE.match(line))
    )


def _strip_caption_lines(text: str) -> str:
    kept: list[str] = []
    previous_line = ""
    for line in text.splitlines():
        if _is_caption_line(line, previous_line):
            continue
        kept.append(line)
        if line.strip():
            previous_line = line
    return "\n".join(kept).strip()


def _reference_entries(lines: list[str]) -> list[tuple[Optional[int], str]]:
    """Group the lines under a notes heading into (note number, text) entries.

    A numbered or "Surname, Given." line starts an entry and the lines after
    it are its wrapped continuation; lines before the first such start stand
    alone, so a prose box is weighed line by line.
    """
    entries: list[tuple[Optional[int], str]] = []
    started = False
    for line in filter(None, map(str.strip, lines)):
        number = _extract_reference_number(line)
        if number is not None or _AUTHOR_ENTRY_RE.match(line):
            started = True
            entries.append((number, line))
        elif started:
            entries[-1] = (entries[-1][0], f"{entries[-1][1]} {line}")
        else:
            entries.append((None, line))
    return entries


def _is_reference_entry(number: Optional[int], text: str, in_note_run: bool) -> bool:
    if (
        (number is not None and in_note_run)
        or _NUMBERED_NOTE_RE.match(text)
        or _AUTHOR_ENTRY_RE.match(text)
        or _REFERENCE_LOCATOR_RE.search(text)
    ):
        return True
    numbered = _TRAILING_REFERENCE_ENTRY_RE.match(text) if number is not None else None
    tokens = _CITATION_LINE_RE.finditer(text, numbered.end() - 1 if numbered else 0)
    # A bare year is citation evidence only in a numbered note ("1 Darwin to
    # Hooker, 1859."); dated prose and quantities ("4 ripe plums") are text.
    return any(numbered or not token.group().isdigit() for token in tokens)


def _strip_reference_sections(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _REFERENCE_SECTION_HEADING_RE.fullmatch(line.strip()):
            continue
        # The same words head a recipe's or textbook's notes box; only a
        # heading over mostly citation-shaped entries (or over nothing)
        # starts a reference section. Notes numbered 1, 2, 3 ... count
        # whatever their wording; quantities ("4 plums", "200 g flour") do
        # not run in sequence.
        entries = _reference_entries(lines[index + 1:])
        numbers = [number for number, _ in entries if number is not None]
        in_note_run = len(numbers) >= 2 and all(b == a + 1 for a, b in zip(numbers, numbers[1:]))
        references = sum(_is_reference_entry(number, text, in_note_run) for number, text in entries)
        if 2 * references >= len(entries):
            return "\n".join(lines[:index]).rstrip()
    return "\n".join(lines).rstrip()


def repair_line_break_hyphenation(
    text: str, *, word_forms: set[str] | None = None,
) -> str:
    """Join likely soft hyphenation introduced by PDF line wrapping."""
    if word_forms is None:
        word_forms = hyphenation_word_forms(text)

    def _repair_known_compound(match: re.Match[str]) -> str:
        run = match.group("run")  # includes the trailing line-break hyphen
        candidate = f"{run}{match.group('right')}".casefold()
        if candidate not in _KNOWN_HYPHENATED_LINE_BREAK_COMPOUNDS:
            return match.group(0)
        if run[:1].isupper():
            return candidate[:1].upper() + candidate[1:]
        return candidate

    def _join(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        if (prefix + suffix).casefold() not in word_forms and (
            prefix.casefold() in _HYPHENATION_PREFIX_EXCEPTIONS
            or suffix.casefold() in _HYPHENATION_SUFFIX_EXCEPTIONS
        ):
            # Settle the break here: a hyphen left before a line break would
            # be rejoined by the later chapter-level dehyphenation pass.
            if suffix.casefold() in _SUSPENDED_COMPOUND_WORDS:
                return f"{prefix}- {suffix}"
            return f"{prefix}-{suffix}"
        return repair_wrapped_words(match.group(0), word_forms=word_forms)

    text = _KNOWN_HYPHENATED_LINE_BREAK_RE.sub(_repair_known_compound, text)
    return _LINE_BREAK_HYPHEN_RE.sub(_join, text)


def _strip_page_number_residue_lines(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not _PAGE_NUMBER_RESIDUE_RE.match(line)
    ]
    return "\n".join(lines)


def _page_boundary_line_indexes(lines: list[str]) -> set[int]:
    non_empty = [idx for idx, line in enumerate(lines) if line.strip()]
    if not non_empty:
        return set()
    return set(non_empty[:_PAGE_BOUNDARY_WINDOW]) | set(non_empty[-_PAGE_BOUNDARY_WINDOW:])


def _normalize_repeated_boundary_line(line: str, folio_titles: set[str]) -> Optional[str]:
    stripped = re.sub(r"\s+", " ", line).strip()
    if not stripped or _PAGE_NUMBER_RE.match(stripped):
        return None
    if len(stripped) > 120:
        return None
    if re.match(
        r"^(?:#{1,6}\s+)?(?:chapter|part|book|section)\s+\d+\b",
        stripped,
        re.IGNORECASE,
    ):
        return None
    title = numbered_folio_title(stripped)
    if title.casefold() in folio_titles:
        stripped = title
    return stripped.casefold() or None


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
        raw = match.group(1) or match.group(2) or match.group(3)
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

    # Footnotes often follow body text without a blank line, so the block
    # starts at its first numbered entry, never above it.
    block_start = min(numbered_indexes)

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
                is_copyright_notice(page_text)
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
        folio_titles = corroborated_folio_titles([
            lines[index] for _, lines, indexes in page_line_indexes for index in sorted(indexes)
        ])
        for _, lines, boundary_indexes in page_line_indexes:
            seen_on_page: set[str] = set()
            for idx in boundary_indexes:
                normalized = _normalize_repeated_boundary_line(lines[idx], folio_titles)
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
        split_pages = iter(page_line_indexes)
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            _, lines, boundary_indexes = next(split_pages)
            strip_page_numbers = page_count > 1
            kept_lines: list[str] = []
            for idx, line in enumerate(lines):
                if idx in boundary_indexes:
                    normalized = _normalize_repeated_boundary_line(line, folio_titles)
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

    def normalize_pages(self, pages_data: list[PageData]) -> list[PageData]:
        cleaned_pages: list[PageData] = []
        word_forms = hyphenation_word_forms("\n".join(
            str(page.get("text", "")) for page in pages_data if isinstance(page, dict)
        ))
        for page in pages_data:
            if not isinstance(page, dict):
                cleaned_pages.append(page)
                continue

            # A lone number in a one-page extraction may be a section heading.
            page_text = strip_page_furniture(
                str(page.get("text", "")), strip_page_numbers=len(pages_data) > 1
            )
            page_text = repair_line_break_hyphenation(
                page_text,
                word_forms=word_forms,
            )
            page_text = _strip_page_number_residue_lines(page_text)
            cleaned_pages.append(_copy_page_with_text(page, page_text.strip()))
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


def normalize_pdf_artifacts(
    pages_data: list[PageData],
    policies: Iterable[PDFArtifactNormalizationPolicy] = DEFAULT_PDF_ARTIFACT_POLICIES,
) -> list[PageData]:
    """Apply page-level PDF artifact policies in parser-safe order."""
    normalized_pages = pages_data
    for policy in policies:
        normalized_pages = policy.normalize_pages(normalized_pages)
    return normalized_pages

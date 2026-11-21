"""Deterministic document cleanup before TTS chunking."""

from __future__ import annotations

from collections import Counter
from dataclasses import is_dataclass, replace
import logging
import re
import string

from modules.ingestion.models import Chapter, Document
from modules.ingestion.text_layout import repair_line_break_hyphenation
from modules.ingestion.content_filter import (
    ContentClassification,
    SkippedSection,
    classify_chapter_content,
    filter_audiobook_chapters,
    is_verse_like,
    serialize_skipped_section,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_CHAPTER_WORDS = 50_000
# Hard caps admit short headers and longer Title-by-Author running headers.
# Title-like typography (not these caps alone) gates membership in the set.
_MAX_HEADER_LENGTH = 120
_MAX_HEADER_WORDS = 18
_TITLE_SMALL_WORDS = frozenset(
    {"a", "an", "the", "and", "or", "of", "by", "for", "in", "on", "to", "at", "from", "with"}
)
_DIGIT_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")
# Lowercase roman page numbers use only i/v/x/l/c: front-matter pagination
# never reaches the d/m (500/1000) range, and the strict canonical form keeps
# real words like "ill", "mix", "did", "civic" out. Uppercase words built from
# these letters ("I", "DID", "MIX") are always body text, never page furniture.
_ROMAN_PAGE_NUMBER_RE = re.compile(
    r"^(?=[ivxlc])c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$"
)


def _is_page_number(line: str) -> bool:
    """True only for digit page numbers and lowercase canonical roman numerals."""
    stripped = line.strip()
    if not stripped:
        return False
    if _DIGIT_PAGE_NUMBER_RE.fullmatch(stripped):
        return True
    return bool(_ROMAN_PAGE_NUMBER_RE.fullmatch(stripped))
_BACKLINK_RE = re.compile(
    r"(?i)(?<!\S)(?:back|return) to (?:top|contents)[^\S\n]*(?:\u21a9[^\S\n]*)?(?=\n|\Z)"
)
_PUBLISHER_BOILERPLATE_RE = re.compile(
    r"(?im)^[^\S\n]*(?:publisher boilerplate|copyright page|all rights reserved).*$"
)


def _remove_backlinks(text: str) -> str:
    """Drop end-of-line "back to top" links together with the whitespace before them.

    Matching starts at the phrase, not at the whitespace before it: a leading
    whitespace pattern would rescan a long whitespace run from each of its positions.
    """
    pieces: list[str] = []
    position = 0
    for match in _BACKLINK_RE.finditer(text):
        pieces.append(text[position:match.start()].rstrip())
        position = match.end()
    pieces.append(text[position:])
    return "".join(pieces)


_NAV_TITLE_RE = re.compile(r"(?i)^(?:table of contents|contents|toc|nav(?:igation)?|landmarks?)$")
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
# Titles that corroborate a low-prose chapter being genuine front/back matter
# rather than an embedded poem/list/screenplay. Only these justify deleting an
# otherwise-readable low-prose chapter that sits inside a prose book.
_LOW_PROSE_BACK_MATTER_TITLE_RE = re.compile(
    r"^(?:"
    r"(?:table\s+of\s+)?contents|toc|"
    r"index|name\s+index|subject\s+index|"
    r"copyright(?:\s+page)?|legal|imprint|colophon|"
    r"bibliography|references|works\s+cited|"
    r"acknowledge?ments?|"
    r"glossary|"
    r"notes?|endnotes?|footnotes?|"
    r"appendix(?:es|\s+[a-z0-9]+)?|appendices|"
    r"permissions?|credits?|"
    r"also\s+by(?:\s+.+)?|"
    r"list\s+of\s+(?:maps|figures|illustrations|tables)|"
    r"about\s+the\s+(?:author|publisher)"
    r")$",
    re.IGNORECASE,
)
# classify_chapter_content reasons that mark genuine non-narrative matter. A
# low-prose chapter is readable content wrongly shaped like verse/lists, so it
# is deliberately excluded here.
_BACK_MATTER_REASONS = frozenset(
    {
        "copyright",
        "table_of_contents",
        "list_of_maps",
        "index",
        "references",
        "epub_non_narrative",
    }
)


def _is_low_prose_back_matter_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title or "").strip().strip(" .:-–—")
    return bool(normalized and _LOW_PROSE_BACK_MATTER_TITLE_RE.match(normalized))


def _has_prose_body(chapters: list[Chapter]) -> bool:
    """True when at least one chapter is genuine narrative prose.

    A lone low-prose chapter (a poem/list) is only protected from deletion when
    it sits *among* real prose; a book that is nothing but verse and back matter
    is left to the normal filter plus the all-removed safety net.
    """
    for chapter in chapters:
        content = str(chapter.content)
        if not content.strip():
            continue
        classification = classify_chapter_content(
            str(chapter.title).strip(), content, enable_low_prose_density=True
        )
        if not classification.should_skip:
            return True
    return False


def _normalize_repeated_candidate(line: str) -> str:
    lowered = line.lower().strip()
    lowered = lowered.translate(str.maketrans("", "", string.punctuation))
    lowered = re.sub(r"\d+", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _looks_like_running_header_text(line: str) -> bool:
    """Typographic cues that separate running headers from body refrains."""
    stripped = line.strip()
    if re.search(r"\sby\s", stripped, re.IGNORECASE):
        return True
    letters = [char for char in stripped if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        return True
    words = stripped.split()
    if not words:
        return False
    significant = [
        word
        for word in words
        if word.lower().strip(string.punctuation) not in _TITLE_SMALL_WORDS
    ] or words
    capitalized = sum(1 for word in significant if word[:1].isupper())
    # Strict majority keeps mixed-case prose ("Never again") out while
    # admitting real Title Case headers ("Example Book", "Running Header").
    return capitalized * 2 > len(significant)


def _looks_like_strong_header_text(line: str) -> bool:
    """Confident running-header typography: ALL CAPS or a "Title by Author" line.

    Plain Title Case ("The Wind Rises", "Summary") is deliberately excluded --
    on its own it is far too weak to justify deleting a line from every chapter.
    """
    stripped = line.strip()
    if re.search(r"\sby\s", stripped, re.IGNORECASE):
        return True
    letters = [char for char in stripped if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters)


def _is_page_number_adjacent(lines: list[str], index: int) -> bool:
    """True when a page number sits within a line or two of ``index``.

    A running header paired with its page number is strong page-furniture
    corroboration that a bare Title-Case line cannot supply on its own.
    """
    for offset in range(1, min(3, index + 1)):
        neighbor = lines[index - offset].strip()
        if not neighbor:
            continue
        if _is_page_number(neighbor):
            return True
        break
    for offset in range(1, min(3, len(lines) - index)):
        neighbor = lines[index + offset].strip()
        if not neighbor:
            continue
        if _is_page_number(neighbor):
            return True
        break
    return False


def _is_page_edge_position(
    lines: list[str], index: int, content_edges: tuple[int, int],
) -> bool:
    """True when a line sits at a chapter/page edge rather than mid-prose.

    ``content_edges`` holds the first and last non-blank line indices; callers
    compute it once per chapter because rescanning every line for each
    candidate made header detection quadratic in chapter length.
    """
    previous_blank = index == 0 or not lines[index - 1].strip()
    next_blank = index == len(lines) - 1 or not lines[index + 1].strip()
    if not (previous_blank or next_blank):
        return False

    # Page numbers often sit one blank away from the running header/footer.
    if _is_page_number_adjacent(lines, index):
        return True

    if index in content_edges:
        return True

    # Isolated blank-surrounded lines often mark page breaks in PDF text.
    return previous_blank and next_blank


def _standalone_header_like_lines(content: str) -> list[tuple[str, bool]]:
    """Return ``(stripped_line, has_strong_evidence)`` for page-edge header lines.

    ``has_strong_evidence`` is True only when the line carries confident header
    corroboration -- ALL CAPS / "by Author" typography, or an adjacent page
    number. A bare Title-Case line surrounded by blanks reports ``False`` so it
    cannot, by itself, promote a body refrain into the running-header set.
    """
    lines = content.splitlines()
    non_blank = [index for index, line in enumerate(lines) if line.strip()]
    if not non_blank:
        return []
    content_edges = (non_blank[0], non_blank[-1])
    candidates: list[tuple[str, bool]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not _is_plausible_page_edge_header(stripped):
            continue
        if not _is_page_edge_position(lines, index, content_edges):
            continue
        strong = _looks_like_strong_header_text(stripped) or _is_page_number_adjacent(
            lines, index
        )
        candidates.append((stripped, strong))
    return candidates


def _raw_variants_differ_only_by_digits(variants: set[str]) -> bool:
    if len(variants) <= 1:
        return False
    digit_stripped = {re.sub(r"\d+", "", variant).strip() for variant in variants}
    return len(digit_stripped) == 1


def _is_plausible_page_edge_header(line: str) -> bool:
    stripped = line.strip()
    if not 4 <= len(stripped) <= _MAX_HEADER_LENGTH:
        return False
    word_count = len(stripped.split())
    if word_count > _MAX_HEADER_WORDS:
        return False
    # Sentence punctuation marks body prose, not running headers/footers.
    if stripped.endswith((".", "!", "?", "…")):
        return False
    # Typographic gate: Title Case / ALL CAPS / "by Author" — not bare
    # length/word caps — so lowercase body refrains stay out of the set.
    return _looks_like_running_header_text(stripped)


def _detect_repeated_headers(chapters: list[Chapter]) -> set[str]:
    occurrence_counts: Counter[str] = Counter()
    normalized_to_raw: dict[str, set[str]] = {}
    normalized_has_strong: dict[str, bool] = {}

    for chapter in chapters:
        for stripped, strong in _standalone_header_like_lines(str(chapter.content)):
            normalized = _normalize_repeated_candidate(stripped)
            if not normalized or normalized.startswith("chapter "):
                continue
            occurrence_counts[normalized] += 1
            normalized_to_raw.setdefault(normalized, set()).add(stripped)
            normalized_has_strong[normalized] = (
                normalized_has_strong.get(normalized, False) or strong
            )

    if not occurrence_counts:
        return set()

    threshold = max(2, int(len(chapters) * 0.30))
    # A recurring line is treated as a running header only when it is frequent
    # AND at least one occurrence carries strong evidence (ALL CAPS / "by
    # Author" typography, or an adjacent page number). Plain Title-Case body
    # refrains and subheadings ("The Wind Rises", "Summary") lack that evidence
    # and are kept -- silently dropping body text is worse than narrating a
    # stray header. No frequency upper bound: a real header dominates the pool.
    return {
        normalized
        for normalized, count in occurrence_counts.items()
        if threshold <= count
        and normalized_has_strong.get(normalized, False)
        and not _raw_variants_differ_only_by_digits(normalized_to_raw.get(normalized, set()))
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


_CHAPTER_LABEL_RE = re.compile(
    r"^(?:chapter|part|section|book)\s+(?:\d+|[ivxlcdm]+|[a-z]+)\b", re.IGNORECASE,
)


def _chapter_running_header(title: str) -> str | None:
    """The label ("Chapter 3") a PDF prints as this chapter's page header, if any."""
    match = _CHAPTER_LABEL_RE.match(str(title or "").strip())
    return match.group(0).casefold() if match else None


def _normalize_pdf_chapter(
    text: str, repeated_headers: set[str], *, running_header: str | None = None,
) -> str:
    kept_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept_lines.append("")
            continue
        if _is_page_number(stripped):
            continue
        normalized = _normalize_repeated_candidate(stripped)
        if normalized in repeated_headers:
            continue
        # The chapter's own label alone on a line is its running page header;
        # header detection keeps "Chapter N" lines because the number varies
        # between chapters. The printed heading was split off before this.
        if running_header is not None and re.sub(r"\s+", " ", stripped).casefold() == running_header:
            continue
        kept_lines.append(line.rstrip())

    cleaned = "\n".join(kept_lines)
    cleaned = repair_line_break_hyphenation(cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = _reflow_lines(cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _normalize_epub_chapter(text: str) -> str:
    cleaned = _remove_backlinks(text)
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
    cleaned = repair_line_break_hyphenation(cleaned)
    cleaned = _reflow_lines(cleaned)
    return cleaned.strip()


def _renumber(chapters: list[Chapter]) -> list[Chapter]:
    renumbered: list[Chapter] = []
    for index, chapter in enumerate(chapters, start=1):
        renumbered.append(replace(chapter, number=index))
    return renumbered


def _paragraphs_for_split(content: str) -> list[str]:
    return [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", content) if paragraph.strip()]


# Mirrors the TTS chunker's sentence-boundary detector so a single hard-wrapped
# paragraph (OCR/export text without blank lines) can still be split rather than
# aborting the whole conversion.
_SENTENCE_BOUNDARY_RE = re.compile(
    r'(?:(?<=[.!?])|(?<=[.!?]["\'”’]))\s+(?=[A-Z"\'“‘])'
)


def _sentences_for_split(paragraph: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in _SENTENCE_BOUNDARY_RE.split(paragraph)
        if sentence.strip()
    ]


def _split_units_for_overlong_chapter(
    paragraphs: list[str], *, max_chapter_words: int
) -> list[str]:
    """Return packable units no larger than the cap.

    An over-cap paragraph splits at sentence boundaries, an over-cap sentence
    at line breaks, and unbroken text (a huge paste without punctuation or
    newlines) into word windows, so oversized input becomes bounded parts
    instead of failing late in the conversion.
    """
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph.split()) <= max_chapter_words:
            units.append(paragraph)
            continue
        for sentence in _sentences_for_split(paragraph):
            if len(sentence.split()) <= max_chapter_words:
                units.append(sentence)
                continue
            for line in (line.strip() for line in sentence.splitlines()):
                words = line.split()
                for start in range(0, len(words), max_chapter_words):
                    units.append(" ".join(words[start:start + max_chapter_words]))
    return units


def _split_overlong_chapter(chapter: Chapter, *, max_chapter_words: int) -> list[Chapter]:
    word_count = len(str(chapter.content).split())
    if word_count <= max_chapter_words:
        return [chapter]

    paragraphs = _paragraphs_for_split(str(chapter.content))
    if not paragraphs:
        return [chapter]

    split_units = _split_units_for_overlong_chapter(
        paragraphs, max_chapter_words=max_chapter_words
    )

    parts: list[str] = []
    current: list[str] = []
    current_words = 0
    for paragraph in split_units:
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
        "Split chapter %s (%s words) into %s parts at paragraph/sentence boundaries",
        chapter.number,
        word_count,
        len(parts),
    )
    return [
        replace(chapter, title=f"{chapter.title} (part {index})", content=part,
                heading_text=chapter.heading_text if index == 1 else "")
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
    before downstream chunking/validation; an over-cap paragraph falls back
    to sentences, then line breaks, then word windows, so every part is
    bounded.
    """
    if not is_dataclass(document):
        return document

    source_type = source_type.lower().lstrip(".")
    chapters = list(document.chapters)
    for chapter in chapters:
        if not is_dataclass(chapter):
            return document

    repeated_headers = _detect_repeated_headers(chapters) if source_type == "pdf" else set()

    # A verse-heavy book (poetry collection) is a legitimate audiobook. The
    # low-prose-density rule would strip its poems one by one, so disable that
    # single rule when more than half of the readable chapters look like verse.
    enable_low_prose_density = not (
        skip_non_prose_sections and _is_verse_heavy(chapters)
    )
    has_prose_body = skip_non_prose_sections and _has_prose_body(chapters)

    normalized_chapters, skipped_sections = _normalize_chapters(
        chapters,
        source_type=source_type,
        apply_non_prose_filter=skip_non_prose_sections,
        apply_epub_non_narrative=True,
        enable_low_prose_density=enable_low_prose_density,
        repeated_headers=repeated_headers,
        max_chapter_words=max_chapter_words,
        has_prose_body=has_prose_body,
    )

    # A non-prose filter that deletes the entire book is always wrong for an
    # audiobook app. If filtering would remove every chapter of a multi-chapter
    # book while the source had readable content, keep the document unfiltered
    # and warn instead. A single standalone back-matter section (an index or
    # copyright page on its own) is still allowed to filter down to nothing --
    # but a single readable chapter that is merely verse/list-shaped must be
    # kept, so a lone chapter dropped only for low prose density also resurrects.
    had_readable_content = any(str(chapter.content).strip() for chapter in chapters)
    removed_only_readable_low_prose = bool(skipped_sections) and all(
        section.reason not in _BACK_MATTER_REASONS for section in skipped_sections
    )
    if (
        skip_non_prose_sections
        and had_readable_content
        and not normalized_chapters
        and (len(chapters) > 1 or removed_only_readable_low_prose)
    ):
        LOGGER.warning(
            "Non-prose filtering would remove all %d chapters of %r; "
            "keeping the document unfiltered.",
            len(chapters),
            getattr(document, "title", ""),
        )
        normalized_chapters, skipped_sections = _normalize_chapters(
            chapters,
            source_type=source_type,
            apply_non_prose_filter=False,
            apply_epub_non_narrative=False,
            enable_low_prose_density=True,
            repeated_headers=repeated_headers,
            max_chapter_words=max_chapter_words,
        )

    for section in skipped_sections:
        LOGGER.info("Skipped section %r during cleanup: %s", section.title, section.reason)
    return replace(
        document,
        chapters=normalized_chapters,
        skipped_non_prose_sections=[
            serialize_skipped_section(section) for section in skipped_sections
        ],
    )


def _is_verse_heavy(chapters: list[Chapter]) -> bool:
    """True when more than half of the readable chapters look like verse."""
    readable = [chapter for chapter in chapters if str(chapter.content).strip()]
    if len(readable) < 2:
        return False
    verse = sum(1 for chapter in readable if is_verse_like(str(chapter.content)))
    return verse * 2 > len(readable)


def _normalize_chapters(
    chapters: list[Chapter],
    *,
    source_type: str,
    apply_non_prose_filter: bool,
    apply_epub_non_narrative: bool,
    enable_low_prose_density: bool,
    repeated_headers: set[str],
    max_chapter_words: int,
    has_prose_body: bool = False,
) -> tuple[list[Chapter], list[SkippedSection]]:
    skipped_sections: list[SkippedSection] = []
    normalized_chapters: list[Chapter] = []
    for chapter in chapters:
        title = str(chapter.title).strip()
        content = str(chapter.content)
        if apply_non_prose_filter:
            classification = classify_chapter_content(
                title, content, enable_low_prose_density=enable_low_prose_density
            )
            # An isolated low-prose chapter (a lone poem/list/screenplay) inside
            # a prose book is readable audio content, not back matter, so keep it
            # unless its title corroborates it being genuine front/back matter.
            if (
                classification.should_skip
                and classification.reason == "low_prose_density"
                and has_prose_body
                and not _is_low_prose_back_matter_title(title)
            ):
                LOGGER.warning(
                    "Keeping low-prose chapter %r among prose chapters instead of "
                    "deleting it as non-prose.",
                    title,
                )
                classification = ContentClassification(False, None, classification.confidence)
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
        if (
            apply_epub_non_narrative
            and source_type == "epub"
            and _is_epub_non_narrative_chapter(title, content)
        ):
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
        heading = getattr(chapter, "heading_text", "")
        if heading and content.startswith(heading):
            content = content[len(heading):].lstrip()
        else:
            heading = ""
        if source_type == "pdf":
            content = _normalize_pdf_chapter(
                content, repeated_headers, running_header=_chapter_running_header(title),
            )
        elif source_type == "epub":
            content = _normalize_epub_chapter(content)
        elif source_type == "md":
            content = _normalize_text_like_chapter(content, markdown=True)
        else:
            content = _normalize_text_like_chapter(content)
        if heading:
            content = heading + ("\n\n" + content if content else "")
        if content.strip():
            normalized_chapter = replace(chapter, content=content, heading_text=heading)
            normalized_chapters.extend(
                _split_overlong_chapter(
                    normalized_chapter,
                    max_chapter_words=max_chapter_words,
                )
            )

    renumbered_chapters = _renumber(normalized_chapters)
    if apply_non_prose_filter:
        # Low-prose dropping was already decided per-chapter above (with the
        # among-prose protection); disabling it here keeps the second pass from
        # silently re-deleting a protected poem while still re-checking the
        # structural back-matter classifiers on normalized content.
        second_pass_low_prose = enable_low_prose_density and not has_prose_body
        retained_chapters, filtered_skipped = filter_audiobook_chapters(
            renumbered_chapters, enable_low_prose_density=second_pass_low_prose
        )
        skipped_sections.extend(filtered_skipped)
        renumbered_chapters = retained_chapters

    return renumbered_chapters, skipped_sections

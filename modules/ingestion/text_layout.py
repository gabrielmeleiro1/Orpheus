"""Shared line-aware extraction repairs for ingestion and TTS cleaning."""

from __future__ import annotations

from collections import Counter, defaultdict
import re


def opening_heading_text(content: str, title: str) -> str:
    """Locate an already identified structural title before prose is reflowed."""
    def key(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[#*_`]", "", value)).strip().casefold()

    def words(value: str) -> list[str]:
        return re.findall(r"\w+", value.casefold())

    def without_repeated_label(prefix_words: list[str], title_words: list[str]) -> list[str]:
        # PDF pages often repeat the title's label ("Chapter 1") as a running
        # header right above the printed heading ("CHAPTER 1 / The Title").
        for size in range(1, len(title_words)):
            label = title_words[:size]
            while prefix_words[:size] == label and prefix_words[size:2 * size] == label:
                prefix_words = prefix_words[size:]
        return prefix_words

    lines = content.splitlines(keepends=True)
    title_words = words(title)
    for count in range(1, min(5, len(lines)) + 1):
        prefix = "".join(lines[:count])
        if key(prefix) == key(title):
            return prefix.rstrip()
        # Punctuation and line breaks differ between an outline title
        # ("Chapter 1: Title") and its printed heading ("CHAPTER 1\nTitle").
        if title_words and without_repeated_label(words(prefix), title_words) == title_words:
            return prefix.rstrip()
    return ""


_COMPOUND_PREFIXES = frozenset({
    "self", "well", "non", "half", "all", "ex", "cross", "full",
    "high", "low", "long", "short", "great", "semi", "quasi", "anti",
    "multi", "pre", "post", "mid", "ill",
})
_SUSPENDED_COMPOUND_WORDS = frozenset({"and", "or", "nor", "but"})


def hyphenation_word_forms(text: str) -> set[str]:
    """Collect intact spellings without treating line-break fragments as words."""
    return set(re.findall(r"(?<![\w-])[^\W\d_]+(?:-[^\W\d_]+)*(?![\w-])", text.casefold()))


def should_join_hyphenated_word(left: str, right: str, word_forms: set[str]) -> bool:
    """Prefer document spelling, then compound and capitalization evidence."""
    if right.casefold() in _SUSPENDED_COMPOUND_WORDS:
        return False
    if f"{left}-{right}".casefold() in word_forms:
        return False
    if (left + right).casefold() in word_forms:
        return True
    if not right.islower():
        return False
    return left.casefold() not in _COMPOUND_PREFIXES and not (left.casefold() == "co" and right[:1] in "aeiou")


# Whitespace runs are matched only from their first character. Starting at
# every position of a long run (as a bare ``\s+`` does) is quadratic.
_LEADING_FOLIO_RE = re.compile(r"^\d+\s+")
_TRAILING_FOLIO_RE = re.compile(r"(?<!\s)\s+(\d+)$")
# Wrapped words start only at the beginning of a letter run, for the same reason.
_LINE_BREAK_HYPHEN_RE = re.compile(
    r"(?<![^\W\d_])(?P<left>[^\W\d_]+)-[ \t]*\r?\n[ \t]*(?P<right>[^\W\d_]+)\b"
)


def numbered_folio_title(line: str) -> str:
    title = _LEADING_FOLIO_RE.sub("", line.strip(), count=1)
    return _TRAILING_FOLIO_RE.sub("", title, count=1).strip()


def corroborated_folio_titles(lines: list[str], *, require_facing_pages: bool = False) -> set[str]:
    """Require changing folios plus a layout gap or facing-page placement."""
    candidates: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for line in lines:
        stripped = line.strip()
        if re.match(r"(?i)^(?:#|chapter\b|part\b|book\b|section\b)", stripped):
            continue
        match = re.match(r"^(\d+)\s+(.+)$", stripped)
        side = "left"
        if match is None:
            # ``stripped`` starts with a non-space, so a trailing folio always
            # has a title before it.
            match = _TRAILING_FOLIO_RE.search(stripped)
            side = "right"
        if match:
            number = match.group(1)
            title = numbered_folio_title(stripped).casefold()
            candidates[title].append((number, side, bool(re.search(r"\s{2,}", stripped))))
    return {
        title for title, occurrences in candidates.items()
        if len({number for number, _, _ in occurrences}) >= 2
        and (
            len({side for _, side, _ in occurrences}) == 2
            or (not require_facing_pages and all(gap for _, _, gap in occurrences))
        )
    }


def page_furniture_lines(text: str) -> set[int]:
    """Identify furniture only at explicit page edges or corroborated folios."""
    furniture: set[int] = set()
    candidates: dict[int, str] = {}
    lines = text.splitlines(keepends=True)
    # Without page breaks, one-sided "Smith    12" lines are as likely a table
    # as a running header; only facing-page folios corroborate one there.
    folio_titles = corroborated_folio_titles(lines, require_facing_pages="\f" not in text)
    page_edges: set[int] = set()
    if "\f" in text:
        start = 0
        for end, line in enumerate(lines):
            if "\f" in line or end == len(lines) - 1:
                non_empty = [i for i in range(start, end + 1) if lines[i].strip()]
                if non_empty:
                    page_edges.update((non_empty[0], non_empty[-1]))
                start = end + 1
    for index, line in enumerate(lines):
        line = line.strip()
        if re.fullmatch(r"\d+", line):
            if index in page_edges:
                furniture.add(index)
            continue
        if not line or len(line) > 100 or re.match(r"(?i)^(?:#|chapter\b|part\b|book\b|section\b)", line):
            continue
        title = numbered_folio_title(line)
        if title.casefold() in folio_titles and title != line:
            furniture.add(index)
            continue
        title = line
        if index not in page_edges:
            continue
        if (
            len(title.split()) > 12
            or not re.fullmatch(r"[^\W\d_][\w\s'’–—-]*", title)
            or not (title.isupper() or title.istitle())
            or (len(title.split()) == 1 and (not title.isupper() or len(title) < 4))
        ):
            continue
        key = re.sub(r"\s+", " ", title).casefold()
        candidates[index] = key
    counts = Counter(candidates.values())
    furniture.update(index for index, key in candidates.items() if counts[key] >= 2)
    return furniture


def strip_page_furniture(text: str, *, strip_page_numbers: bool = True) -> str:
    furniture = page_furniture_lines(text)
    return "".join(
        line for index, line in enumerate(text.splitlines(keepends=True))
        if index not in furniture or (not strip_page_numbers and line.strip().isdigit())
    )


def repair_line_break_hyphenation(text: str, *, word_forms: set[str] | None = None) -> str:
    """Join wrapped words using intact spellings and compound exceptions."""
    if word_forms is None:
        word_forms = hyphenation_word_forms(text)

    def _join(match: re.Match[str]) -> str:
        if match.group("right").casefold() in _SUSPENDED_COMPOUND_WORDS:
            return match.group(0)
        left, right = match.group("left"), match.group("right")
        if should_join_hyphenated_word(left, right, word_forms):
            return left + right
        if not right.islower():
            return match.group(0)
        return left + "-" + right

    return _LINE_BREAK_HYPHEN_RE.sub(_join, text)


def repair_text_layout(text: str) -> str:
    """Repair extraction artifacts while the original line boundaries remain."""
    return repair_line_break_hyphenation(strip_page_furniture(text))

"""Shared line-aware extraction repairs for ingestion and TTS cleaning."""

from __future__ import annotations

from collections import Counter
import re


def page_furniture_lines(text: str) -> set[int]:
    """Identify page numbers and short, title-like running headers/footers.

    Repetition alone is not enough for prose or dialogue: candidates must look
    like titles, without sentence punctuation or list/Markdown markers.
    """
    furniture: set[int] = set()
    candidates: dict[int, str] = {}
    for index, line in enumerate(text.splitlines()):
        line = line.strip()
        if re.fullmatch(r"\d+", line):
            furniture.add(index)
            continue
        if not line or line.startswith("#") or len(line) > 100:
            continue
        title = re.sub(r"^\d+\s+|\s+\d+$", "", line).strip()
        if (
            len(title.split()) > 12
            or not re.fullmatch(r"[^\W\d_][\w\s'’–—-]*", title)
            or not (title.isupper() or title.istitle())
            or (len(title.split()) == 1 and (not title.isupper() or len(title) < 4))
        ):
            continue
        key = re.sub(r"[\d\s]+", "", title).casefold()
        candidates[index] = key
        if title.isupper() and title != line:
            furniture.add(index)
    counts = Counter(candidates.values())
    furniture.update(index for index, key in candidates.items() if counts[key] >= 2)
    return furniture


def strip_page_furniture(text: str, *, strip_page_numbers: bool = True) -> str:
    furniture = page_furniture_lines(text)
    return "".join(
        line for index, line in enumerate(text.splitlines(keepends=True))
        if index not in furniture or (not strip_page_numbers and line.strip().isdigit())
    )


def repair_line_break_hyphenation(text: str) -> str:
    """Join wrapped words, preserving suspended compounds such as pre- and post-."""

    def _join(match: re.Match[str]) -> str:
        if match.group("right") in {"and", "or", "nor", "but"}:
            return match.group(0)
        return match.group("left") + match.group("right")

    return re.sub(
        r"(?P<left>[^\W\d_])-[ \t]*\r?\n[ \t]*(?P<right>[a-zà-öø-ÿ]+)\b",
        _join,
        text,
    )


def repair_text_layout(text: str) -> str:
    """Repair extraction artifacts while the original line boundaries remain."""
    return repair_line_break_hyphenation(strip_page_furniture(text))

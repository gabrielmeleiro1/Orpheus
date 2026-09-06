"""Foundational book title and output-directory identity rules."""

from __future__ import annotations

import re

MAX_BOOK_TITLE_PATH_BYTES = 120
UNTITLED_BOOK_DIR = "Untitled"
_WINDOWS_RESERVED_BASENAME_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)",
    re.IGNORECASE,
)


def _truncate_utf8_segment(value: str, max_bytes: int) -> str:
    """Truncate a path segment without splitting a UTF-8 sequence."""

    if len(value.encode("utf-8")) <= max_bytes:
        return value

    chars: list[str] = []
    used_bytes = 0
    for char in value:
        char_bytes = len(char.encode("utf-8"))
        if used_bytes + char_bytes > max_bytes:
            break
        chars.append(char)
        used_bytes += char_bytes
    return "".join(chars).rstrip() or UNTITLED_BOOK_DIR


def sanitize_book_title(book_title: str) -> str:
    """Normalize a title into the stable output-directory naming scheme."""

    normalized_title = str(book_title or "").strip().rstrip(" .")
    if not normalized_title:
        return UNTITLED_BOOK_DIR

    safe_title = "".join(
        char if char.isalnum() or char in " -_" else "_"
        for char in normalized_title
    ).strip(" .")
    if not safe_title:
        return UNTITLED_BOOK_DIR
    if _WINDOWS_RESERVED_BASENAME_RE.match(normalized_title):
        safe_title = f"_{safe_title}"
    return _truncate_utf8_segment(safe_title, MAX_BOOK_TITLE_PATH_BYTES)


def candidate_book_title_with_suffix(safe_title: str, suffix: str) -> str:
    """Append a collision suffix without exceeding the path byte budget."""

    suffix_bytes = len(suffix.encode("utf-8"))
    base_max_bytes = max(1, MAX_BOOK_TITLE_PATH_BYTES - suffix_bytes)
    return _truncate_utf8_segment(
        f"{_truncate_utf8_segment(safe_title, base_max_bytes)}{suffix}",
        MAX_BOOK_TITLE_PATH_BYTES,
    )

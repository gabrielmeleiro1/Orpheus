"""Helpers for per-book output workspace setup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


MAX_BOOK_TITLE_PATH_BYTES = 120
UNTITLED_BOOK_DIR = "Untitled"
_WINDOWS_RESERVED_BASENAME_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BookWorkspace:
    """Resolved filesystem layout for one converted book."""

    book_title: str
    safe_title: str
    output_dir: Path
    chapters_dir: Path


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
    """Normalize a title into the current output-directory naming scheme."""
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


def _candidate_title_with_suffix(safe_title: str, suffix: str) -> str:
    suffix_bytes = len(suffix.encode("utf-8"))
    base_max_bytes = max(1, MAX_BOOK_TITLE_PATH_BYTES - suffix_bytes)
    return _truncate_utf8_segment(
        f"{_truncate_utf8_segment(safe_title, base_max_bytes)}{suffix}",
        MAX_BOOK_TITLE_PATH_BYTES,
    )


def prepare_book_workspace(output_root: Path, book_title: str) -> BookWorkspace:
    """Create the book output directory tree and return the resolved paths."""
    safe_title = sanitize_book_title(book_title)
    output_root = Path(output_root)
    output_dir = output_root / safe_title
    if output_dir.exists():
        suffix = 2
        candidate_title = _candidate_title_with_suffix(safe_title, f" ({suffix})")
        while (output_root / candidate_title).exists():
            suffix += 1
            candidate_title = _candidate_title_with_suffix(safe_title, f" ({suffix})")
        output_dir = output_root / candidate_title
    output_dir.mkdir(parents=True, exist_ok=True)
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(exist_ok=True)
    return BookWorkspace(
        book_title=book_title,
        safe_title=safe_title,
        output_dir=output_dir,
        chapters_dir=chapters_dir,
    )


def initialize_book_workspace(
    pipeline: Any,
    document: Any,
    book_title: str,
) -> BookWorkspace:
    """Prepare output paths and persist the source artifact through the pipeline hook."""
    workspace = prepare_book_workspace(pipeline.config.output_dir, book_title)
    pipeline.stages.save_intermediate_source(document, workspace.output_dir)
    return workspace

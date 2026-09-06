"""Helpers for per-book output workspace setup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.contracts.book_identity import (
    UNTITLED_BOOK_DIR,
    candidate_book_title_with_suffix,
    sanitize_book_title,
)

__all__ = [
    "BookWorkspace",
    "UNTITLED_BOOK_DIR",
    "initialize_book_workspace",
    "prepare_book_workspace",
    "sanitize_book_title",
]


@dataclass(frozen=True)
class BookWorkspace:
    """Resolved filesystem layout for one converted book."""

    book_title: str
    safe_title: str
    output_dir: Path
    chapters_dir: Path


def prepare_book_workspace(output_root: Path, book_title: str) -> BookWorkspace:
    """Create the book output directory tree and return the resolved paths."""
    safe_title = sanitize_book_title(book_title)
    output_root = Path(output_root)
    output_dir = output_root / safe_title
    if output_dir.exists():
        suffix = 2
        candidate_title = candidate_book_title_with_suffix(
            safe_title,
            f" ({suffix})",
        )
        while (output_root / candidate_title).exists():
            suffix += 1
            candidate_title = candidate_book_title_with_suffix(
                safe_title,
                f" ({suffix})",
            )
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

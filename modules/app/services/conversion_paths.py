"""Path and playback metadata helpers for conversion service."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from modules.pipeline.book_workspace import sanitize_book_title
from modules.storage.maintenance import resolve_stored_path, workspace_root_for_db
from modules.storage.models import PlaybackStatus


def playback_metadata_for_pipeline_result(
    pipeline_result: Any,
) -> tuple[Optional[str], PlaybackStatus]:
    if getattr(pipeline_result, "output_path", None):
        return str(pipeline_result.output_path), PlaybackStatus.PACKAGED

    for chapter in getattr(pipeline_result, "chapters", []):
        chapter_path = getattr(chapter, "mp3_path", None)
        if chapter_path is None:
            continue
        if hasattr(chapter_path, "exists") and not chapter_path.exists():
            continue
        return str(chapter_path), PlaybackStatus.CHAPTER_AUDIO

    return None, PlaybackStatus.MISSING


def resolve_conversion_stored_path(
    raw_path: Optional[str],
    *,
    db_path: Path,
    output_dir: Path,
    temp_dir: Path,
) -> Optional[Path]:
    return resolve_stored_path(
        raw_path,
        workspace_root_for_db(Path(db_path)),
        output_root=output_dir,
        temp_root=temp_dir,
    )


def repair_output_dirs(
    book: Any,
    chapters: list[Any],
    *,
    db_path: Path,
    output_dir: Path,
    temp_dir: Path,
) -> tuple[Path, Path]:
    candidate_paths = []
    if getattr(book, "playback_path", None):
        candidate = resolve_conversion_stored_path(
            book.playback_path,
            db_path=db_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
        )
        if candidate is not None:
            candidate_paths.append(candidate)
    for chapter in chapters:
        candidate = resolve_conversion_stored_path(
            getattr(chapter, "mp3_path", None),
            db_path=db_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
        )
        if candidate is not None:
            candidate_paths.append(candidate)

    for candidate in candidate_paths:
        directory = candidate if candidate.is_dir() else candidate.parent
        if directory.name == "chapters":
            return directory.parent, directory
        return directory, directory / "chapters"

    book_dir = output_dir / sanitize_book_title(getattr(book, "title", f"book_{book.id}"))
    return book_dir, book_dir / "chapters"

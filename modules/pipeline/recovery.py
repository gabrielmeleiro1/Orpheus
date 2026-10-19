"""Durable inventory for interrupted source conversions.

The inventory is committed before synthesis. Encoders publish chapter files by
atomic rename, so an existing inventoried file is a completed checkpoint even if
the process died before delivering its result to the application service.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from modules.audio.ffmpeg import atomic_output_path
from modules.contracts.durable_files import atomic_write_json, sync_directory, sync_file
from modules.contracts.recovery_inventory import (
    INVENTORY_NAME as INVENTORY_NAME,
    SOURCE_DIRECTORY as SOURCE_DIRECTORY,
    chapter_audio_path as chapter_audio_path,
    recovery_audio_paths as recovery_audio_paths,
)
from modules.ingestion.filenames import source_filename
from .models import ChapterResult, ConversionResult


def prepare_recovery_source(source_path: Path, workspace: Any, chapters: list[Any], *, emit_mp3: bool) -> Path:
    """Keep the original bytes and intended audio paths independently of temp cleanup."""
    source_directory = workspace.output_dir / SOURCE_DIRECTORY
    source_directory.mkdir(exist_ok=True)
    durable_source = source_directory / source_filename(source_path.name)
    with atomic_output_path(durable_source) as pending:
        shutil.copyfile(source_path, pending)
        sync_file(pending)
    sync_directory(source_directory)
    payload = {
        'version': 1,
        'source': durable_source.name,
        'chapters': [
            {
                'number': int(chapter.number),
                'title': str(chapter.title),
                'audio': chapter_audio_path(chapter, workspace.chapters_dir, '.mp3' if emit_mp3 else '.m4a').name,
            }
            for chapter in chapters
        ],
    }
    atomic_write_json(workspace.output_dir / INVENTORY_NAME, payload)
    return durable_source


def recovery_snapshot(source_path: Path, *, title: str, author: str | None, results: list[ChapterResult]) -> ConversionResult:
    """Expose only published artifacts, keeping unproduced chapters explicitly missing."""
    results_by_number = {result.chapter_number: result for result in results}
    chapters = []
    for (number, chapter_title), path in recovery_audio_paths(source_path).items():
        result = results_by_number.get(number)
        chapters.append(ChapterResult(
            chapter_number=number,
            chapter_title=chapter_title,
            mp3_path=path if path.is_file() and path.stat().st_size > 0 else None,
            duration_ms=result.duration_ms if result is not None else 0,
        ))
    return ConversionResult(success=False, title=title, author=author, chapters=chapters)

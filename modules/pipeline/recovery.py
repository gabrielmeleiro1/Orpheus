"""Durable inventory for interrupted source conversions.

The inventory is committed before synthesis. Encoders publish chapter files by
atomic rename, so an existing inventoried file is a completed checkpoint even if
the process died before delivering its result to the application service.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from modules.audio.ffmpeg import atomic_output_path
from .models import ChapterResult, ConversionResult

INVENTORY_NAME = '.orpheus-recovery.json'
SOURCE_DIRECTORY = '.orpheus-source'


def chapter_audio_path(chapter: Any, output_dir: Path, suffix: str) -> Path:
    safe_title = ''.join(
        char if char.isalnum() or char in ' -_' else '_'
        for char in str(getattr(chapter, 'title', '') or '')
    )[:50]
    return output_dir / f'{int(chapter.number):02d}_{safe_title}{suffix}'


def prepare_recovery_source(source_path: Path, workspace: Any, chapters: list[Any], *, emit_mp3: bool) -> Path:
    """Keep the original bytes and intended audio paths independently of temp cleanup."""
    source_directory = workspace.output_dir / SOURCE_DIRECTORY
    source_directory.mkdir(exist_ok=True)
    durable_source = source_directory / source_path.name
    with atomic_output_path(durable_source) as pending:
        shutil.copyfile(source_path, pending)
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
    with atomic_output_path(workspace.output_dir / INVENTORY_NAME) as pending:
        pending.write_text(json.dumps(payload), encoding='utf-8')
    return durable_source


def recovery_audio_paths(source_path: Path) -> dict[tuple[int, str], Path]:
    """Read only the app-owned inventory, rejecting paths outside its chapters directory."""
    if source_path.parent.name != SOURCE_DIRECTORY:
        return {}
    try:
        payload = json.loads((source_path.parent.parent / INVENTORY_NAME).read_text(encoding='utf-8'))
        if payload.get('version') != 1 or payload.get('source') != source_path.name:
            return {}
        paths = {}
        for item in payload['chapters']:
            name = item['audio']
            if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
                return {}
            if Path(name).suffix.lower() not in {'.mp3', '.m4a'}:
                return {}
            paths[(int(item['number']), str(item['title']))] = source_path.parent.parent / 'chapters' / name
        return paths
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


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

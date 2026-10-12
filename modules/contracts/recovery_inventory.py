"""On-disk inventory and chapter filename rules for interrupted conversions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INVENTORY_NAME = '.orpheus-recovery.json'
SOURCE_DIRECTORY = '.orpheus-source'


def chapter_audio_path(chapter: Any, output_dir: Path, suffix: str) -> Path:
    safe_title = ''.join(
        char if char.isalnum() or char in ' -_' else '_'
        for char in str(getattr(chapter, 'title', '') or '')
    )[:50]
    return output_dir / f'{int(chapter.number):02d}_{safe_title}{suffix}'


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

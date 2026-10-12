"""Shared fixtures for library regressions."""

import pytest

from modules.app.services.library_service import LibraryService
from modules.storage.models import BookCreate, ChapterCreate, PlaybackStatus, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


@pytest.fixture
def library(tmp_path):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    service = LibraryService(
        repo, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )
    try:
        yield repo, service
    finally:
        repo.close()


def _book(repo, source, audio, title="Saved"):
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"saved audio")
    return repo.create_book_with_chapters(
        BookCreate(
            title=title, source_path=str(source), source_type=SourceType.TXT,
            total_chapters=1, playback_path=str(audio), playback_status=PlaybackStatus.PACKAGED,
        ),
        [ChapterCreate(book_id=0, chapter_number=1, title="First", mp3_path=str(audio))],
    )[0]

"""Regression coverage for library service."""

from unittest.mock import Mock
import os
import time

import pytest

from modules.app.services.library_service import LibraryService
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository

from tests._factories_group2_library import _book


def test_failed_book_rollback_survives_staging_sweep(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    service = LibraryService(repo, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    source = tmp_path / "temp" / "web_uploads" / "book.txt"
    source.parent.mkdir()
    source.write_text("recoverable")
    book = repo.create_book(BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT))
    monkeypatch.setattr(repo, "delete_book", Mock(side_effect=RuntimeError("database unavailable")))
    restore = service._restore_staged_artifacts
    monkeypatch.setattr(service, "_restore_staged_artifacts", lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="database unavailable"):
            service.delete_book(book.id)
        root = tmp_path / "temp" / "delete_staging"
        staged = next(source.parent.glob(".orpheus-delete-staging/*/*-book.txt"))
        assert staged.read_text() == "recoverable"
        old = time.time() - 3 * 86400
        for path in root.rglob("*"):
            os.utime(path, (old, old))
        assert service.sweep_delete_staging() == 0
        assert staged.read_text() == "recoverable"
        assert repo.get_book(book.id) is not None
        monkeypatch.setattr(service, "_restore_staged_artifacts", restore)
        assert service.sweep_delete_staging() == 1
        assert source.read_text() == "recoverable"
        assert not staged.exists()
        assert not list(root.iterdir())
    finally:
        repo.close()


@pytest.mark.parametrize("source_name,owned", [
    ("temp/web_uploads/source.txt", True),
    ("data/sources/source.txt", True),
    ("documents/source.txt", False),
    ("documents/temp/web_uploads/source.txt", False),
])
@pytest.mark.parametrize("shared", [False, True])
def test_deletion_removes_owned_sources_only_after_the_last_reference(
    tmp_path, library, source_name, owned, shared,
):
    repo, service = library
    source = tmp_path / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source text")
    audio = tmp_path / "output" / "first" / "book.m4b"
    first = _book(repo, source, audio)
    if shared:
        other_audio = tmp_path / "output" / "second" / "book.m4b"
        # Relative and absolute references name the same real file.
        second = _book(repo, source_name, other_audio, title="Second")

    assert service.delete_book(first.id) is True
    assert repo.get_book(first.id) is None
    assert repo.get_chapters(first.id) == []
    assert not audio.exists()
    assert source.exists() is (shared or not owned)
    if shared:
        assert service.get_book(second.id)["source_available"] is True
        assert other_audio.read_bytes() == b"saved audio"
        assert service.delete_book(second.id) is True
        assert not other_audio.exists()
    assert source.exists() is (not owned)
    if not owned:
        assert source.read_bytes() == b"source text"


@pytest.mark.parametrize("source_exists", [False, True])
def test_missing_audio_keeps_library_rows_and_stored_playback_metadata(
    tmp_path, library, source_exists,
):
    repo, service = library
    source = tmp_path / "source.txt"
    if source_exists:
        source.write_text("source text")
    audio = tmp_path / "output" / "saved" / "book.m4b"
    book = _book(repo, source, audio)
    audio.unlink()

    summary, = service.get_library_books()
    detail = service.get_book(book.id)
    assert summary.id == book.id
    assert summary.source_available is source_exists
    assert detail["source_available"] is source_exists
    # current behaviour: a missing file does not downgrade stored packaged status.
    assert detail["playback_status"] == "packaged"
    assert detail["playback_path"] == str(audio)
    assert detail["chapters"][0]["mp3_path"] == str(audio)
    assert service.get_book_playback_file(book.id) is None
    assert service.get_book_playback_file(book.id, 1) is None
    assert repo.count_books() == 1
    assert len(repo.get_chapters(book.id)) == 1

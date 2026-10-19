"""Regression coverage for storage maintenance."""

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from modules.storage.maintenance import (
    backup_database,
    prune_stale_records_and_files,
    restore_database_backup,
)
from modules.storage.sqlite_repo import SQLiteRepository

from tests._factories_group2_library import _book


def test_database_restore_restores_rows_without_restoring_source_or_audio(tmp_path, library):
    repo, service = library
    source = tmp_path / "temp" / "web_uploads" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source text")
    audio = tmp_path / "output" / "saved" / "book.m4b"
    book = _book(repo, source, audio)
    backup = backup_database(repo.db_path)

    assert service.delete_book(book.id) is True
    safety = restore_database_backup(repo, backup.name)

    assert safety.is_file()
    with closing(sqlite3.connect(safety)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone() == (0,)
    assert service.get_book(book.id)["source_path"] == str(source)
    assert len(repo.get_chapters(book.id)) == 1
    assert not source.exists()
    assert not audio.exists()
    assert service.get_book(book.id)["source_available"] is False
    assert service.get_book_playback_file(book.id) is None


def test_restore_after_shared_upload_relocation_keeps_available_source_pointers(tmp_path, library):
    repo, service = library
    source = tmp_path / "temp" / "web_uploads" / "shared.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"shared source")
    original_paths = [str(source), "temp/web_uploads/shared.txt"]
    books = [
        _book(repo, raw, tmp_path / "output" / str(index) / "book.m4b", title=str(index))
        for index, raw in enumerate(original_paths)
    ]
    backup = backup_database(repo.db_path)
    prune_stale_records_and_files(
        repo.db_path, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        aggressive=True,
    )
    relocated_paths = [repo.get_book(book.id).source_path for book in books]
    assert len(set(relocated_paths)) == 1
    relocated = Path(relocated_paths[0])
    assert relocated.parent == repo.db_path.parent / "sources"
    assert relocated.read_bytes() == b"shared source"
    assert not source.exists()
    assert all(service.get_book(book.id)["source_available"] for book in books)

    restore_database_backup(repo, backup.name)

    assert [repo.get_book(book.id).source_path for book in books] == relocated_paths
    assert all(service.get_book(book.id)["source_available"] for book in books)
    assert relocated.read_bytes() == b"shared source"
    assert all(service.get_book_playback_file(book.id).is_file() for book in books)


@pytest.mark.parametrize("legacy_version", [None, 0, 8])
def test_repeated_restore_renews_epoch_and_reopen_preserves_it(tmp_path, library, legacy_version):
    repo, _ = library
    book = _book(repo, "source.txt", tmp_path / "output" / "saved" / "book.m4b")
    original = repo.get_library_identity()
    backup = backup_database(repo.db_path)
    if legacy_version is not None:
        with closing(sqlite3.connect(backup)) as conn, conn:
            conn.execute("DROP TABLE library_metadata")
            conn.execute("DELETE FROM schema_migrations")
            if legacy_version:
                conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (legacy_version,))
    backup_bytes = backup.read_bytes()
    repo.update_book(book.id, title="Changed")
    assert repo.get_library_identity() == original
    epochs = {original["library_epoch"]}
    for generation in (1, 2):
        restore_database_backup(repo, backup.name)
        identity = repo.get_library_identity()
        assert identity["library_epoch"] not in epochs
        epochs.add(identity["library_epoch"])
        # A backup without identity adopts the live library's id; only the
        # epoch marks the replacement.
        assert identity["library_id"] == original["library_id"]
        assert repo.library_generation == generation
        assert repo.database_replaced is True
        assert repo.get_book(book.id).title == "Saved"
        reopened = SQLiteRepository(repo.db_path)
        try:
            assert reopened.get_library_identity() == identity
            assert reopened.library_generation == 0
        finally:
            reopened.close()
    assert backup.read_bytes() == backup_bytes

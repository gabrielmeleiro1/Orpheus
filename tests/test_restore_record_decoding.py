"""Restore must preserve the live library when a snapshot cannot be decoded."""

from contextlib import closing
import sqlite3

import pytest

from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


@pytest.mark.parametrize("field, value", [
    ("created_at", "not-a-date"),
    ("updated_at", "not-a-date"),
    ("source_type", None),
    ("playback_position_sec", "not-a-number"),
])
def test_restore_rejects_unreadable_books_before_publication(tmp_path, field, value):
    with closing(SQLiteRepository(
        tmp_path / "data" / "library.db",
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
    )) as repository:
        saved = repository.create_book(BookCreate(
            title="Saved", source_path="synthetic.txt", source_type=SourceType.TXT,
        ))
        snapshot = backup_database(repository.db_path)
        repository.update_book(saved.id, title="Current")
        identity = repository.get_library_identity()
        with closing(sqlite3.connect(snapshot)) as connection, connection:
            connection.execute(f"UPDATE books SET {field} = ?", (value,))
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

        with pytest.raises(ValueError, match="unreadable library records"):
            restore_database_backup(repository, snapshot.name)

        assert repository.get_library_identity() == identity
        assert repository.get_book(saved.id).title == "Current"
        assert repository.list_books()[0].title == "Current"


@pytest.fixture
def saved_snapshot(tmp_path):
    with closing(SQLiteRepository(
        tmp_path / "data" / "library.db",
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
    )) as repository:
        saved, _ = repository.create_book_with_chapters(
            BookCreate(title="Saved", source_path="synthetic.txt", source_type=SourceType.TXT, total_chapters=1),
            [ChapterCreate(book_id=0, chapter_number=1, title="First", duration_ms=1000)],
        )
        snapshot = backup_database(repository.db_path)
        repository.update_book(saved.id, title="Current")
        yield repository, saved.id, snapshot, repository.get_library_identity()


@pytest.mark.parametrize("table,field,value", [
    ("books", "playback_position_sec", float("inf")),
    ("books", "playback_position_sec", float("-inf")),
    ("books", "playback_position_sec", "NaN"),
    ("books", "total_chapters", float("inf")),
    ("books", "total_chapters", "many"),
    ("books", "title", b"\xff"),
    ("books", "author", b"\xff"),
    ("books", "source_path", b"\xff"),
    ("books", "playback_path", b"\xff"),
    ("books", "tags", b"\xff"),
    ("books", "conversion_runtime_options", b"\xff"),
    ("chapters", "chapter_number", "first"),
    ("chapters", "chapter_number", float("inf")),
    ("chapters", "duration_ms", float("inf")),
    ("chapters", "duration_ms", "long"),
    ("chapters", "packaged_start_ms", float("inf")),
    ("chapters", "title", b"\xff"),
    ("chapters", "mp3_path", b"\xff"),
])
def test_restore_rejects_invalid_record_types_and_nonfinite_values(saved_snapshot, table, field, value):
    repository, book_id, snapshot, identity = saved_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        connection.execute(f"UPDATE {table} SET {field} = ?", (value,))
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

    with pytest.raises(ValueError, match="unreadable library records"):
        restore_database_backup(repository, snapshot.name)

    assert repository.get_library_identity() == identity
    assert repository.get_book(book_id).title == "Current"
    assert repository.list_books()[0].title == "Current"
    assert repository.get_chapters(book_id)[0].title == "First"
    assert not list(snapshot.parent.glob("*safety*"))


def test_restore_preserves_nullable_legacy_fields_and_finite_numbers(saved_snapshot):
    repository, book_id, snapshot, identity = saved_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        connection.execute(
            "UPDATE books SET author=NULL, total_chapters=NULL, playback_path=NULL, "
            "playback_status=NULL, conversion_runtime_options=NULL, playback_position_sec=12.25"
        )
        connection.execute(
            "UPDATE chapters SET title=NULL, duration_ms=NULL, mp3_path=NULL, packaged_start_ms=NULL"
        )
    snapshot_bytes = snapshot.read_bytes()

    restore_database_backup(repository, snapshot.name)

    restored = repository.get_book(book_id)
    assert restored.title == "Saved"
    assert restored.author is None
    assert restored.total_chapters is None
    assert restored.playback_path is None
    assert restored.conversion_runtime_options is None
    assert restored.playback_position_sec == 12.25
    chapter = repository.get_chapters(book_id)[0]
    assert chapter.title is None
    assert chapter.duration_ms is None
    assert chapter.mp3_path is None
    assert chapter.packaged_start_ms is None
    assert repository.get_library_identity()["library_epoch"] != identity["library_epoch"]
    assert snapshot.read_bytes() == snapshot_bytes

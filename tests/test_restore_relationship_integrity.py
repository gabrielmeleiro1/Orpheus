"""Reject relationally corrupt backups before publishing a restored library."""

from contextlib import closing
import sqlite3

import pytest

from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def test_restore_rejects_orphan_chapter_and_preserves_current_library(tmp_path):
    with closing(SQLiteRepository(
        tmp_path / "data" / "library.db",
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
    )) as repository:
        saved, _ = repository.create_book_with_chapters(
            BookCreate(title="Saved", source_path="synthetic.txt", source_type=SourceType.TXT, total_chapters=1),
            [ChapterCreate(book_id=0, chapter_number=1, title="First")],
        )
        snapshot = backup_database(repository.db_path)
        repository.update_book(saved.id, title="Current")
        identity = repository.get_library_identity()
        with closing(sqlite3.connect(snapshot)) as connection, connection:
            connection.execute(
                "INSERT INTO chapters(book_id, chapter_number, title) VALUES (?, 1, 'Orphan')",
                (saved.id + 1,),
            )
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall()

        with pytest.raises(ValueError, match="relationships"):
            restore_database_backup(repository, snapshot.name)

        assert repository.get_library_identity() == identity
        assert repository.get_book(saved.id).title == "Current"
        assert repository._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        next_book, chapters = repository.create_book_with_chapters(
            BookCreate(title="Next", source_path="next.txt", source_type=SourceType.TXT, total_chapters=1),
            [ChapterCreate(book_id=0, chapter_number=1, title="Next chapter")],
        )
        assert next_book.id == saved.id + 1
        assert len(chapters) == 1

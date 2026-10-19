"""Restored snapshots must retain the schema guarantees used by library writes."""

from contextlib import closing
import sqlite3

import pytest

from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


@pytest.fixture
def library_snapshot(tmp_path):
    with closing(SQLiteRepository(
        tmp_path / "data" / "library.db",
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
    )) as repository:
        book, _ = repository.create_book_with_chapters(
            BookCreate(title="Saved", source_path="synthetic.txt", source_type=SourceType.TXT, total_chapters=1),
            [ChapterCreate(book_id=0, chapter_number=1, title="First")],
        )
        snapshot = backup_database(repository.db_path)
        repository.update_book(book.id, title="Current")
        yield repository, book.id, snapshot, repository.get_library_identity()


def replace_chapters(connection, *, foreign_key=True, cascade=True, unique=True, primary_key=True,
                     wrong_foreign_column=False, extra_unique_column=False, reversed_unique=False):
    constraints = []
    if foreign_key:
        column = "chapter_number" if wrong_foreign_column else "book_id"
        action = "CASCADE" if cascade else "NO ACTION"
        constraints.append(f"FOREIGN KEY ({column}) REFERENCES books(id) ON DELETE {action}")
    if unique:
        columns = "chapter_number, book_id" if reversed_unique else "book_id, chapter_number"
        if extra_unique_column:
            columns += ", title"
        constraints.append(f"UNIQUE({columns})")
    definition = "INTEGER PRIMARY KEY AUTOINCREMENT" if primary_key else "INTEGER UNIQUE"
    suffix = ", " + ", ".join(constraints) if constraints else ""
    connection.execute(f"""
        CREATE TABLE audit_chapters (
            id {definition}, book_id INTEGER NOT NULL, chapter_number INTEGER NOT NULL,
            title TEXT, duration_ms INTEGER, mp3_path TEXT, packaged_start_ms INTEGER{suffix}
        )
    """)
    connection.execute("INSERT INTO audit_chapters SELECT * FROM chapters")
    connection.execute("DROP TABLE chapters")
    connection.execute("ALTER TABLE audit_chapters RENAME TO chapters")


@pytest.mark.parametrize("damage", [
    "missing_fk", "missing_fk_with_orphan", "missing_cascade", "wrong_fk_column",
    "missing_unique", "partial_unique", "wider_unique", "missing_chapter_pk",
    "missing_book_pk", "text_book_pk", "descending_book_pk",
])
def test_restore_rejects_missing_schema_guarantees_before_publication(library_snapshot, damage):
    repository, book_id, snapshot, identity = library_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        if damage in {"missing_book_pk", "text_book_pk", "descending_book_pk"}:
            declaration = {
                "missing_book_pk": "INTEGER UNIQUE",
                "text_book_pk": "TEXT PRIMARY KEY",
                "descending_book_pk": "INTEGER PRIMARY KEY DESC",
            }[damage]
            original = connection.execute("SELECT sql FROM sqlite_master WHERE name='books'").fetchone()[0]
            assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in original
            changed = original.replace("CREATE TABLE books", "CREATE TABLE audit_books", 1).replace(
                "id INTEGER PRIMARY KEY AUTOINCREMENT", f"id {declaration}", 1,
            )
            connection.execute(changed)
            connection.execute("INSERT INTO audit_books SELECT * FROM books")
            connection.execute("DROP TABLE books")
            connection.execute("ALTER TABLE audit_books RENAME TO books")
        else:
            replace_chapters(
                connection,
                foreign_key=damage not in {"missing_fk", "missing_fk_with_orphan"},
                cascade=damage != "missing_cascade",
                unique=damage not in {"missing_unique", "partial_unique"},
                primary_key=damage != "missing_chapter_pk",
                wrong_foreign_column=damage == "wrong_fk_column",
                extra_unique_column=damage == "wider_unique",
            )
            if damage == "missing_fk_with_orphan":
                connection.execute(
                    "INSERT INTO chapters(book_id, chapter_number, title) VALUES (?, 1, 'Orphan')",
                    (book_id + 1,),
                )
            if damage == "partial_unique":
                connection.execute(
                    "CREATE UNIQUE INDEX partial_chapters ON chapters(book_id, chapter_number) WHERE chapter_number > 0"
                )
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(ValueError, match="incompatible library schema"):
        restore_database_backup(repository, snapshot.name)

    assert repository.get_library_identity() == identity
    assert repository.get_book(book_id).title == "Current"
    assert not list(snapshot.parent.glob("*safety*"))
    next_book, chapters = repository.create_book_with_chapters(
        BookCreate(title="Next", source_path="next.txt", source_type=SourceType.TXT, total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title="Next chapter")],
    )
    assert next_book.id == book_id + 1
    assert len(chapters) == 1
    assert repository.delete_book(next_book.id)
    assert repository.get_chapters(next_book.id) == []


@pytest.mark.parametrize("schema", ["current", "unversioned", "version_one", "reversed_unique"])
def test_restore_accepts_migrated_legacy_and_equivalent_constraints(library_snapshot, schema):
    repository, book_id, snapshot, identity = library_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        if schema in {"unversioned", "version_one"}:
            replace_chapters(connection, unique=False)
            connection.execute("DELETE FROM schema_migrations")
            if schema == "version_one":
                connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        elif schema == "reversed_unique":
            replace_chapters(connection, reversed_unique=True)
    snapshot_bytes = snapshot.read_bytes()

    restore_database_backup(repository, snapshot.name)

    assert repository.get_book(book_id).title == "Saved"
    assert repository.get_library_identity()["library_epoch"] != identity["library_epoch"]
    assert len(repository.get_chapters(book_id)) == 1
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        repository.create_chapter(ChapterCreate(book_id=book_id, chapter_number=1, title="Duplicate"))
    assert repository.delete_book(book_id)
    assert repository.get_chapters(book_id) == []
    assert snapshot.read_bytes() == snapshot_bytes

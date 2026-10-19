"""A saved library may contain data and constraints, but no executable triggers."""

from contextlib import closing
import sqlite3

import pytest

from modules.storage import maintenance
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
        snapshot = maintenance.backup_database(repository.db_path)
        repository.update_book(book.id, title="Current")
        yield repository, book.id, snapshot, repository.get_library_identity()


def install_trigger(connection, variant):
    if variant in {"books", "chapters"}:
        connection.execute(f"""
            CREATE TRIGGER audit_block_insert BEFORE INSERT ON {variant}
            BEGIN SELECT RAISE(ABORT, 'synthetic injected trigger'); END
        """)
    elif variant in {"metadata_insert", "metadata_update", "migrations"}:
        if variant == "migrations":
            connection.execute("DELETE FROM schema_migrations WHERE version > 8")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)")
            event = "AFTER INSERT ON schema_migrations"
        else:
            event = (
                "BEFORE INSERT ON library_metadata" if variant == "metadata_insert"
                else "AFTER UPDATE ON library_metadata"
            )
        connection.execute(f"""
            CREATE TRIGGER audit_delete_library {event}
            BEGIN DELETE FROM chapters; DELETE FROM books; END
        """)
    elif variant == "unrelated_table":
        connection.execute("CREATE TABLE audit_extra(value TEXT)")
        connection.execute("""
            CREATE TRIGGER audit_dormant_trigger AFTER INSERT ON audit_extra
            BEGIN DELETE FROM chapters; DELETE FROM books; END
        """)
    else:
        table = variant.removesuffix("_view")
        connection.execute(f"ALTER TABLE {table} RENAME TO audit_original")
        connection.execute(f"CREATE VIEW {table} AS SELECT * FROM audit_original")
        connection.execute(f"""
            CREATE TRIGGER audit_view_trigger INSTEAD OF INSERT ON {table}
            BEGIN SELECT RAISE(ABORT, 'synthetic injected trigger'); END
        """)


@pytest.mark.parametrize("variant", [
    "books", "chapters", "metadata_insert", "metadata_update", "migrations",
    "unrelated_table", "library_metadata_view", "schema_migrations_view",
])
def test_restore_rejects_snapshots_with_triggers(library_snapshot, variant):
    repository, book_id, snapshot, identity = library_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        install_trigger(connection, variant)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    snapshot_bytes = snapshot.read_bytes()

    with pytest.raises(ValueError, match="unsupported database triggers"):
        maintenance.restore_database_backup(repository, snapshot.name)

    assert repository.get_library_identity() == identity
    assert repository.get_book(book_id).title == "Current"
    assert len(repository.get_chapters(book_id)) == 1
    assert snapshot.read_bytes() == snapshot_bytes
    assert not list(snapshot.parent.glob("*safety*"))
    next_book, chapters = repository.create_book_with_chapters(
        BookCreate(title="Next", source_path="next.txt", source_type=SourceType.TXT, total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title="Next chapter")],
    )
    assert next_book.id == book_id + 1
    assert len(chapters) == 1


@pytest.mark.parametrize("table", ["books", "chapters", "library_metadata", "schema_migrations"])
def test_restore_rejects_views_substituted_for_required_tables(library_snapshot, table):
    repository, book_id, snapshot, identity = library_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        connection.execute(f"ALTER TABLE {table} RENAME TO audit_original")
        connection.execute(f"CREATE VIEW {table} AS SELECT * FROM audit_original")
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall() == []
    snapshot_bytes = snapshot.read_bytes()

    with pytest.raises(ValueError, match="not an Orpheus library database|incompatible library schema"):
        maintenance.restore_database_backup(repository, snapshot.name)

    assert repository.get_library_identity() == identity
    assert repository.get_book(book_id).title == "Current"
    assert len(repository.get_chapters(book_id)) == 1
    assert snapshot.read_bytes() == snapshot_bytes
    assert not list(snapshot.parent.glob("*safety*"))


@pytest.mark.parametrize("variant", ["books", "metadata_insert", "migrations"])
def test_restore_rechecks_schema_of_the_copied_snapshot(library_snapshot, monkeypatch, variant):
    repository, book_id, snapshot, identity = library_snapshot
    real_connect = sqlite3.connect
    copied_snapshots = []

    class SourceConnection(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            # Commit through another real connection after the source checks,
            # before SQLite's backup read transaction starts.
            with closing(real_connect(snapshot)) as writer, writer:
                install_trigger(writer, variant)
            copied_snapshots.append(snapshot.read_bytes())
            return super().backup(target, *args, **kwargs)

    def controlled_connect(database, *args, **kwargs):
        if kwargs.get("uri") and "mode=ro" in str(database):
            kwargs["factory"] = SourceConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", controlled_connect)

    with pytest.raises(ValueError, match="unsupported database triggers"):
        maintenance.restore_database_backup(repository, snapshot.name)

    assert len(copied_snapshots) == 1
    assert snapshot.read_bytes() == copied_snapshots[0]
    assert repository.get_library_identity() == identity
    assert repository.get_book(book_id).title == "Current"
    assert len(repository.get_chapters(book_id)) == 1
    assert not list(snapshot.parent.glob("*safety*"))
    next_book = repository.create_book(BookCreate(title="Next", source_path="next.txt", source_type=SourceType.TXT))
    assert next_book.id == book_id + 1


@pytest.mark.parametrize("schema", ["current", "version_eight", "unversioned"])
def test_restore_accepts_current_and_legacy_snapshots_without_triggers(library_snapshot, schema):
    repository, book_id, snapshot, identity = library_snapshot
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        if schema != "current":
            connection.execute("DROP TABLE library_metadata")
            connection.execute("DELETE FROM schema_migrations")
            if schema == "version_eight":
                connection.execute("INSERT INTO schema_migrations(version) VALUES (8)")
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall() == []
    snapshot_bytes = snapshot.read_bytes()

    maintenance.restore_database_backup(repository, snapshot.name)

    assert repository.get_book(book_id).title == "Saved"
    assert len(repository.get_chapters(book_id)) == 1
    assert repository.get_library_identity()["library_epoch"] != identity["library_epoch"]
    assert snapshot.read_bytes() == snapshot_bytes

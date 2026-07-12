"""
Test storage schema migrations.
"""

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.storage.models import BookCreate, ConversionMode, PlaybackStatus, SourceType
from modules.storage import migrations
from modules.storage.sqlite_repo import SQLiteRepository


def _create_legacy_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT,
                source_path TEXT NOT NULL,
                source_type TEXT CHECK(source_type IN ('pdf', 'epub')),
                total_chapters INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT,
                start_time_ms INTEGER,
                duration_ms INTEGER,
                mp3_path TEXT,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE TABLE processing_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                status TEXT CHECK(status IN ('pending', 'processing', 'completed', 'failed')) DEFAULT 'pending',
                progress REAL DEFAULT 0,
                current_stage TEXT,
                error_message TEXT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE INDEX idx_chapters_book_id ON chapters(book_id);
            CREATE INDEX idx_jobs_book_id ON processing_jobs(book_id);
            CREATE INDEX idx_jobs_status ON processing_jobs(status);
            CREATE INDEX idx_books_source_path ON books(source_path);
            """
        )
        conn.execute(
            """
            INSERT INTO books (title, author, source_path, source_type, total_chapters)
            VALUES ('legacy', 'author', '/tmp/source.pdf', 'pdf', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO chapters (book_id, chapter_number, title, start_time_ms, duration_ms, mp3_path)
            VALUES (1, 1, 'chapter', 1000, 5000, '/tmp/chapter.mp3')
            """
        )
        conn.execute(
            """
            INSERT INTO processing_jobs (book_id, status, progress, current_stage)
            VALUES (1, 'processing', 0.2, 'legacy')
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_storage_migrations() -> bool:
    test_dir = PROJECT_ROOT / "tests" / "data"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_db = test_dir / "test_storage_migrations.db"
    if test_db.exists():
        os.remove(test_db)

    print("\n" + "=" * 50)
    print("STORAGE MIGRATIONS TEST SUITE")
    print("=" * 50 + "\n")

    try:
        _create_legacy_schema(test_db)
        _ = SQLiteRepository(test_db)

        conn = sqlite3.connect(test_db)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "books" in tables
            assert "chapters" in tables
            assert "schema_migrations" in tables
            assert "processing_jobs" not in tables

            chapter_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(chapters)").fetchall()
            }
            assert "start_time_ms" not in chapter_columns
            assert {"id", "book_id", "chapter_number", "title", "duration_ms", "mp3_path"}.issubset(chapter_columns)

            book_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()
            }
            assert "playback_position_sec" in book_columns
            assert "playback_path" in book_columns
            assert "playback_status" in book_columns
            assert "conversion_mode" in book_columns
            assert "tags" in book_columns
            assert "favorite" in book_columns

            index_names = {
                row[1] for row in conn.execute("PRAGMA index_list(chapters)").fetchall()
            }
            assert "idx_chapters_book_chapter" in index_names or any(
                name.startswith("sqlite_autoindex_chapters_") for name in index_names
            )

            book_index_names = {
                row[1] for row in conn.execute("PRAGMA index_list(books)").fetchall()
            }
            assert "idx_books_created_at" in book_index_names
            assert "idx_books_source_path" in book_index_names

            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            assert version == 7

            playback_position = conn.execute(
                "SELECT playback_position_sec FROM books WHERE id = 1"
            ).fetchone()[0]
            assert float(playback_position) == 0.0
            assert conn.execute(
                "SELECT conversion_mode FROM books WHERE id = 1"
            ).fetchone()[0] == ConversionMode.AUDIOBOOK.value

            migrated_book_count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            migrated_chapter_count = conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
            assert migrated_book_count == 1
            assert migrated_chapter_count == 1
        finally:
            conn.close()

        print("✓ Legacy schema migrated to v7")
        print("✓ processing_jobs removed")
        print("✓ chapters.start_time_ms removed")
        print("✓ books.playback_position_sec added with default")
        print("✓ books.playback_path and books.playback_status added")
        print("✓ books.conversion_mode added with audiobook default")
        print("✓ expected indexes present")
    except Exception as exc:
        print(f"✗ Migration test failed: {exc}")
        return False

    print("\n" + "=" * 50)
    print("ALL TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_storage_migrations()
    sys.exit(0 if success else 1)


def test_legacy_schema_migrates_playback_position_column(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_schema(db_path)

    SQLiteRepository(db_path)

    conn = sqlite3.connect(db_path)
    try:
        book_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()
        }
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        playback_position = conn.execute(
            "SELECT playback_position_sec FROM books WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "playback_position_sec" in book_columns
    assert version == 7
    assert float(playback_position) == 0.0


def _create_v3_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT,
                source_path TEXT NOT NULL,
                source_type TEXT CHECK(source_type IN ('pdf', 'epub')),
                total_chapters INTEGER DEFAULT 0,
                playback_position_sec REAL NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT,
                duration_ms INTEGER,
                mp3_path TEXT,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_number)
            );

            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX idx_books_source_path ON books(source_path);
            CREATE INDEX idx_books_created_at ON books(created_at DESC);
            CREATE INDEX idx_chapters_book_chapter ON chapters(book_id, chapter_number);

            INSERT INTO schema_migrations(version) VALUES (3);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_v3_book_with_chapter(db_path: Path, *, title: str, mp3_path: str | None) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO books (title, author, source_path, source_type, total_chapters, playback_position_sec)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, "Author", "/tmp/source.pdf", "pdf", 1, 0.0),
        )
        conn.execute(
            """
            INSERT INTO chapters (book_id, chapter_number, title, duration_ms, mp3_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, 1, "Chapter 1", 1000, mp3_path),
        )
        conn.commit()
    finally:
        conn.close()


def test_v3_schema_migration_does_not_probe_packaged_playback_metadata(tmp_path):
    db_path = tmp_path / "data" / "legacy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _create_v3_schema(db_path)

    book_dir = tmp_path / "output" / "Legacy Book"
    chapter_path = book_dir / "chapters" / "01.mp3"
    chapter_path.parent.mkdir(parents=True, exist_ok=True)
    chapter_path.write_bytes(b"mp3")
    packaged_path = book_dir / "Legacy Book.m4b"
    packaged_path.write_bytes(b"m4b")

    _insert_v3_book_with_chapter(
        db_path,
        title="Legacy Book",
        mp3_path="output/Legacy Book/chapters/01.mp3",
    )

    repo = SQLiteRepository(db_path)
    book = repo.get_book(1)

    assert book is not None
    assert book.playback_path is None
    assert book.playback_status == PlaybackStatus.MISSING


def test_v3_schema_migration_does_not_probe_chapter_audio_metadata(tmp_path):
    db_path = tmp_path / "data" / "legacy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _create_v3_schema(db_path)

    chapter_path = tmp_path / "output" / "Legacy Book" / "chapters" / "01.mp3"
    chapter_path.parent.mkdir(parents=True, exist_ok=True)
    chapter_path.write_bytes(b"mp3")

    _insert_v3_book_with_chapter(
        db_path,
        title="Legacy Book",
        mp3_path=str(chapter_path),
    )

    repo = SQLiteRepository(db_path)
    book = repo.get_book(1)

    assert book is not None
    assert book.playback_path is None
    assert book.playback_status == PlaybackStatus.MISSING


def test_v3_schema_backfills_missing_when_no_playable_audio_exists(tmp_path):
    db_path = tmp_path / "data" / "legacy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _create_v3_schema(db_path)

    _insert_v3_book_with_chapter(
        db_path,
        title="Legacy Book",
        mp3_path="output/Legacy Book/chapters/01.mp3",
    )

    repo = SQLiteRepository(db_path)
    book = repo.get_book(1)

    assert book is not None
    assert book.playback_path is None
    assert book.playback_status == PlaybackStatus.MISSING


def test_legacy_schema_migrates_conversion_mode_with_audiobook_default(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_schema(db_path)

    repo = SQLiteRepository(db_path)
    book = repo.get_book(1)

    conn = sqlite3.connect(db_path)
    try:
        book_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()
        }
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        stored_conversion_mode = conn.execute(
            "SELECT conversion_mode FROM books WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "conversion_mode" in book_columns
    assert version == 7
    assert stored_conversion_mode == ConversionMode.AUDIOBOOK.value
    assert book is not None
    assert book.conversion_mode == ConversionMode.AUDIOBOOK


def test_schema_v6_adds_library_organization_columns(tmp_path):
    db_path = tmp_path / "fresh.db"

    repo = SQLiteRepository(db_path)
    book = repo.create_book(
        BookCreate(
            title="Organized",
            author="Tester",
            source_path="/tmp/organized.md",
            source_type=SourceType.MD,
            tags="fiction,night",
            favorite=True,
        )
    )

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        stored = conn.execute("SELECT tags, favorite FROM books WHERE id = ?", (book.id,)).fetchone()
    finally:
        conn.close()
        repo.close()

    assert {"tags", "favorite", "conversion_runtime_options"}.issubset(columns)
    assert version == 7
    assert stored == ("fiction,night", 1)


def test_interrupted_v5_migration_rolls_back_without_partial_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _create_v3_schema(db_path)
    _insert_v3_book_with_chapter(db_path, title="Legacy Book", mp3_path=None)

    original_migrate_to_v5 = migrations._migrate_to_v5

    def interrupted_migrate_to_v5(conn):
        original_migrate_to_v5(conn)
        raise RuntimeError("simulated interruption after v5 rebuild")

    monkeypatch.setattr(migrations, "_migrate_to_v5", interrupted_migrate_to_v5)

    conn = sqlite3.connect(db_path)
    try:
        try:
            migrations.run_migrations(conn)
        except RuntimeError as exc:
            assert "simulated interruption" in str(exc)
        else:
            raise AssertionError("migration interruption did not propagate")

        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        book_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()
        }
        book_count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        chapter_count = conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    finally:
        conn.close()

    assert version == 4
    assert "playback_path" in book_columns
    assert "playback_status" in book_columns
    assert "conversion_mode" not in book_columns
    assert book_count == 1
    assert chapter_count == 1


def test_legacy_rebuild_of_modern_db_preserves_tags_favorite_and_runtime_options(tmp_path):
    """A modern (v7-shaped) DB that lost its schema_migrations table takes the
    legacy rebuild path; it must carry organization metadata and persisted
    conversion runtime options forward instead of resetting them."""
    db_path = tmp_path / "modern.db"

    conn = sqlite3.connect(db_path)
    try:
        migrations.run_migrations(conn)
        conn.execute(
            """
            INSERT INTO books (
                title, source_path, source_type, conversion_mode,
                tags, favorite, conversion_runtime_options
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Kept Book",
                "/tmp/kept.epub",
                "epub",
                "audiobook",
                "fiction,long",
                1,
                '{"voice": "af_bella"}',
            ),
        )
        conn.commit()
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    try:
        version = migrations.run_migrations(conn)
        assert version == migrations.LATEST_SCHEMA_VERSION
        row = conn.execute(
            "SELECT tags, favorite, conversion_runtime_options FROM books"
        ).fetchone()
    finally:
        conn.close()

    assert row == ("fiction,long", 1, '{"voice": "af_bella"}')

"""
Test storage schema migrations.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.storage.models import ConversionMode, PlaybackStatus
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
    assert version == migrations.LATEST_SCHEMA_VERSION
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
    assert version == migrations.LATEST_SCHEMA_VERSION
    assert stored_conversion_mode == ConversionMode.AUDIOBOOK.value
    assert book is not None
    assert book.conversion_mode == ConversionMode.AUDIOBOOK


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


@pytest.mark.parametrize("metadata_sql", [
    "DROP TABLE schema_migrations",
    "DELETE FROM schema_migrations",
])
def test_legacy_rebuild_preserves_all_supported_chapter_fields(tmp_path, metadata_sql):
    db_path = tmp_path / "modern.db"
    chapters = [
        (7, 1, 1, "Opening", 1234, "/tmp/opening.mp3", 0),
        (9, 1, 2, "Continuation", 5678, "/tmp/continuation.mp3", 1234),
        (12, 1, 3, None, None, None, None),
    ]
    columns = "id, book_id, chapter_number, title, duration_ms, mp3_path, packaged_start_ms"
    with sqlite3.connect(db_path) as conn:
        migrations.run_migrations(conn)
        conn.execute("INSERT INTO books (id, title, source_path) VALUES (1, 'Book', '/tmp/book.pdf')")
        conn.executemany(f"INSERT INTO chapters ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?)", chapters)
        conn.execute(metadata_sql)

    with sqlite3.connect(db_path) as conn:
        assert migrations.run_migrations(conn) == migrations.LATEST_SCHEMA_VERSION
        assert conn.execute(f"SELECT {columns} FROM chapters ORDER BY id").fetchall() == chapters
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_rebuild_keeps_first_duplicate_chapter_with_its_packaged_start(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE chapters ADD COLUMN packaged_start_ms INTEGER")
        conn.execute("UPDATE chapters SET packaged_start_ms = 1234")
        conn.execute("""
            INSERT INTO chapters (book_id, chapter_number, title, packaged_start_ms)
            VALUES (1, 1, 'Duplicate', 5678)
        """)

    with sqlite3.connect(db_path) as conn:
        migrations.run_migrations(conn)
        assert conn.execute(
            "SELECT id, title, packaged_start_ms FROM chapters"
        ).fetchall() == [(1, "chapter", 1234)]
        assert "start_time_ms" not in {
            row[1] for row in conn.execute("PRAGMA table_info(chapters)")
        }


def test_configure_connection_sets_busy_timeout():
    """A busy_timeout of 0 makes a competing writer trip an immediate
    'database is locked'; configure_connection must set a non-zero timeout
    deterministically regardless of how the connection was opened (defect 8)."""
    # Open with timeout=0 to defeat Python's default 5s busy_timeout, proving
    # configure_connection sets the value itself rather than relying on defaults.
    conn = sqlite3.connect(":memory:", timeout=0)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        migrations.configure_connection(conn)
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert timeout_ms == migrations.BUSY_TIMEOUT_MS
    assert timeout_ms > 0


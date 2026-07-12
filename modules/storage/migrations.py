"""
SQLite schema migrations for storage layer.
"""

from __future__ import annotations

import sqlite3

LATEST_SCHEMA_VERSION = 7


def configure_connection(conn: sqlite3.Connection) -> None:
    """
    Apply runtime PRAGMA settings for reliability and throughput.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in columns)


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row else 0


def _bootstrap_schema_v5(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT,
            source_path TEXT NOT NULL,
            source_type TEXT CHECK(source_type IN ('pdf', 'epub', 'txt', 'md')),
            conversion_mode TEXT NOT NULL DEFAULT 'audiobook' CHECK(conversion_mode IN ('audiobook', 'document_audio')),
            total_chapters INTEGER DEFAULT 0,
            playback_path TEXT,
            playback_status TEXT CHECK(playback_status IN ('packaged', 'chapter_audio', 'missing')),
            playback_position_sec REAL NOT NULL DEFAULT 0,
            conversion_runtime_options TEXT,
            tags TEXT NOT NULL DEFAULT '',
            favorite INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            chapter_number INTEGER NOT NULL,
            title TEXT,
            duration_ms INTEGER,
            mp3_path TEXT,
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
            UNIQUE(book_id, chapter_number)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_source_path ON books(source_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_created_at ON books(created_at DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chapters_book_chapter ON chapters(book_id, chapter_number)"
    )


def _apply_migration(
    conn: sqlite3.Connection,
    version: int,
    migration,
    *,
    disable_foreign_keys: bool = False,
) -> None:
    if disable_foreign_keys:
        conn.execute("PRAGMA foreign_keys = OFF")

    try:
        conn.execute("BEGIN IMMEDIATE")
        migration(conn)

        if disable_foreign_keys:
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"foreign key violations after migration {version}: {violations}")

        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if disable_foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")


def _apply_bootstrap(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        _bootstrap_schema_v5(conn)
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (LATEST_SCHEMA_VERSION,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """
    Migrate from legacy schema to v2:
    - remove processing_jobs
    - remove chapters.start_time_ms
    - add UNIQUE(book_id, chapter_number)
    - add sort-friendly indexes
    """
    if not _table_exists(conn, "books"):
        _bootstrap_schema_v5(conn)
        return

    if not _table_exists(conn, "chapters"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT,
                duration_ms INTEGER,
                mp3_path TEXT,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_number)
            )
            """
        )
    else:
        # Always rebuild chapters so duplicates are safely deduped during migration.
        conn.execute(
            """
            CREATE TABLE chapters_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT,
                duration_ms INTEGER,
                mp3_path TEXT,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_number)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO chapters_new (
                id, book_id, chapter_number, title, duration_ms, mp3_path
            )
            SELECT id, book_id, chapter_number, title, duration_ms, mp3_path
            FROM chapters
            ORDER BY id
            """
        )
        conn.execute("DROP TABLE chapters")
        conn.execute("ALTER TABLE chapters_new RENAME TO chapters")

    conn.execute("DROP TABLE IF EXISTS processing_jobs")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_book_id")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_status")
    conn.execute("DROP INDEX IF EXISTS idx_chapters_book_id")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_source_path ON books(source_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_created_at ON books(created_at DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chapters_book_chapter ON chapters(book_id, chapter_number)"
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Add persisted playback position metadata to books."""
    if not _table_exists(conn, "books"):
        _bootstrap_schema_v5(conn)
        return

    if not _column_exists(conn, "books", "playback_position_sec"):
        conn.execute(
            """
            ALTER TABLE books
            ADD COLUMN playback_position_sec REAL NOT NULL DEFAULT 0
            """
        )


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Add persisted playback path and playback status metadata to books."""
    if not _table_exists(conn, "books"):
        _bootstrap_schema_v5(conn)
        return

    if not _column_exists(conn, "books", "playback_path"):
        conn.execute(
            """
            ALTER TABLE books
            ADD COLUMN playback_path TEXT
            """
        )

    if not _column_exists(conn, "books", "playback_status"):
        conn.execute(
            """
            ALTER TABLE books
            ADD COLUMN playback_status TEXT CHECK(playback_status IN ('packaged', 'chapter_audio', 'missing'))
            """
        )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """Expand source types and add persisted conversion_mode metadata."""
    if not _table_exists(conn, "books"):
        _bootstrap_schema_v5(conn)
        return

    has_playback_position = _column_exists(conn, "books", "playback_position_sec")
    has_playback_path = _column_exists(conn, "books", "playback_path")
    has_playback_status = _column_exists(conn, "books", "playback_status")
    has_conversion_mode = _column_exists(conn, "books", "conversion_mode")
    # A modern database rebuilt through the legacy path (schema_migrations
    # lost) already has these later-version columns; carry their values
    # forward instead of resetting them.
    has_tags = _column_exists(conn, "books", "tags")
    has_favorite = _column_exists(conn, "books", "favorite")
    has_runtime_options = _column_exists(conn, "books", "conversion_runtime_options")

    select_playback_position = "playback_position_sec" if has_playback_position else "0"
    select_playback_path = "playback_path" if has_playback_path else "NULL"
    select_playback_status = "playback_status" if has_playback_status else "'missing'"
    select_conversion_mode = "conversion_mode" if has_conversion_mode else "'audiobook'"
    select_tags = "COALESCE(tags, '')" if has_tags else "''"
    select_favorite = "COALESCE(favorite, 0)" if has_favorite else "0"
    select_runtime_options = "conversion_runtime_options" if has_runtime_options else "NULL"

    conn.execute("DROP INDEX IF EXISTS idx_books_source_path")
    conn.execute("DROP INDEX IF EXISTS idx_books_created_at")
    conn.execute(
        """
        CREATE TABLE books_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT,
                source_path TEXT NOT NULL,
                source_type TEXT CHECK(source_type IN ('pdf', 'epub', 'txt', 'md')),
                conversion_mode TEXT NOT NULL DEFAULT 'audiobook' CHECK(conversion_mode IN ('audiobook', 'document_audio')),
                total_chapters INTEGER DEFAULT 0,
                playback_path TEXT,
                playback_status TEXT CHECK(playback_status IN ('packaged', 'chapter_audio', 'missing')),
                playback_position_sec REAL NOT NULL DEFAULT 0,
                conversion_runtime_options TEXT,
                tags TEXT NOT NULL DEFAULT '',
                favorite INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
            INSERT INTO books_new (
                id,
                title,
                author,
                source_path,
                source_type,
                conversion_mode,
                total_chapters,
                playback_path,
                playback_status,
                playback_position_sec,
                conversion_runtime_options,
                tags,
                favorite,
                created_at,
                updated_at
            )
            SELECT
                id,
                title,
                author,
                source_path,
                source_type,
                COALESCE({select_conversion_mode}, 'audiobook'),
                total_chapters,
                {select_playback_path},
                COALESCE({select_playback_status}, 'missing'),
                COALESCE({select_playback_position}, 0),
                {select_runtime_options},
                {select_tags},
                {select_favorite},
                created_at,
                updated_at
            FROM books
        """
    )
    conn.execute("DROP TABLE books")
    conn.execute("ALTER TABLE books_new RENAME TO books")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_source_path ON books(source_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_created_at ON books(created_at DESC)")


def run_migrations(conn: sqlite3.Connection) -> int:
    """
    Apply storage migrations and return resulting schema version.
    """
    configure_connection(conn)
    _ensure_migrations_table(conn)
    version = _current_version(conn)

    # Legacy database without migration metadata.
    if version == 0 and (_table_exists(conn, "books") or _table_exists(conn, "chapters")):
        _apply_migration(conn, 2, _migrate_to_v2)
        _apply_migration(conn, 3, _migrate_to_v3)
        _apply_migration(conn, 4, _migrate_to_v4)
        _apply_migration(conn, 5, _migrate_to_v5, disable_foreign_keys=True)
        _apply_migration(conn, 6, _migrate_to_v6)
        _apply_migration(conn, 7, _migrate_to_v7)
        return LATEST_SCHEMA_VERSION

    # Fresh database path.
    if version == 0:
        _apply_bootstrap(conn)
        return LATEST_SCHEMA_VERSION

    if version < 2:
        _apply_migration(conn, 2, _migrate_to_v2)
        version = 2

    if version < 3:
        _apply_migration(conn, 3, _migrate_to_v3)
        version = 3

    if version < 4:
        _apply_migration(conn, 4, _migrate_to_v4)
        version = 4

    if version < 5:
        _apply_migration(conn, 5, _migrate_to_v5, disable_foreign_keys=True)
        version = 5

    if version < 6:
        _apply_migration(conn, 6, _migrate_to_v6)
        version = 6

    if version < 7:
        _apply_migration(conn, 7, _migrate_to_v7)
        version = 7

    return version


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """Add lightweight library organization metadata."""
    if not _table_exists(conn, "books"):
        _bootstrap_schema_v5(conn)
        return

    if not _column_exists(conn, "books", "tags"):
        conn.execute(
            """
            ALTER TABLE books
            ADD COLUMN tags TEXT NOT NULL DEFAULT ''
            """
        )

    if not _column_exists(conn, "books", "favorite"):
        conn.execute(
            """
            ALTER TABLE books
            ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0
            """
        )


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """Persist conversion runtime settings needed for consistent repair."""
    if not _table_exists(conn, "books"):
        _bootstrap_schema_v5(conn)
        return

    if not _column_exists(conn, "books", "conversion_runtime_options"):
        conn.execute(
            """
            ALTER TABLE books
            ADD COLUMN conversion_runtime_options TEXT
            """
        )

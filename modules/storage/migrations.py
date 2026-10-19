"""
SQLite schema migrations for storage layer.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
import os
from pathlib import Path

LATEST_SCHEMA_VERSION = 9
MANAGED_ROOT_KEYS = ("sources", "output", "temp")

# Wait this long for a competing writer's lock before surfacing
# 'database is locked'. Maintenance opens its own connections outside the
# repository lock, so without a busy_timeout a backup taken during an active
# conversion commit fails immediately.
BUSY_TIMEOUT_MS = 5000


def remap_absolute_path(raw: str, roots: list[tuple[Path, Path]]) -> str:
    """Move only absolute descendants, without following filesystem links."""
    path = Path(raw)
    if not path.is_absolute():
        return raw
    normalized = Path(os.path.normcase(os.path.normpath(raw)))
    for old, new in sorted(roots, key=lambda pair: len(pair[0].parts), reverse=True):
        old = Path(os.path.normcase(os.path.normpath(str(old))))
        try:
            relative = normalized.relative_to(old)
        except ValueError:
            continue
        if not relative.parts:
            continue
        # Preserve filename case even when Windows compares roots without it.
        suffix = Path(*Path(os.path.normpath(raw)).parts[len(old.parts):])
        return str(new / suffix)
    return raw


def remap_stored_paths(conn: sqlite3.Connection, roots: list[tuple[Path, Path]]) -> None:
    """Rewrite the stored path columns in the caller's transaction."""
    if not roots:
        return
    for table, columns in (("books", ("source_path", "playback_path")), ("chapters", ("mp3_path",))):
        for column in columns:
            if not _column_exists(conn, table, column):
                continue
            for row_id, raw in conn.execute(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall():
                remapped = remap_absolute_path(raw, roots)
                if remapped != raw:
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (remapped, row_id))


def store_managed_roots(conn: sqlite3.Connection, roots: tuple[Path, ...]) -> None:
    layout = dict(zip(MANAGED_ROOT_KEYS, map(str, roots), strict=True))
    conn.execute(
        "INSERT INTO library_metadata(key, value) VALUES ('managed_roots', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(layout),),
    )


class FutureSchemaError(RuntimeError):
    """Raised when the on-disk schema is newer than this build understands."""


def configure_connection(conn: sqlite3.Connection) -> None:
    """
    Apply runtime PRAGMA settings for reliability and throughput.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


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
            packaged_start_ms INTEGER,
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
        _migrate_to_v9(conn)
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
                packaged_start_ms INTEGER,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_number)
            )
            """
        )
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
        copied_columns = ", ".join(
            row[1]
            for row in conn.execute("PRAGMA table_info(chapters_new)")
            if row[1] in existing_columns
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO chapters_new (
                {copied_columns}
            )
            SELECT {copied_columns}
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


def check_schema_version(conn: sqlite3.Connection) -> int:
    version = _current_version(conn) if _table_exists(conn, "schema_migrations") else 0
    # A database written by a newer build passes every ``version < N`` gate
    # untouched, leaving the app operating on a schema it does not understand.
    # Refuse to open it with a clear message instead of silently proceeding.
    if version > LATEST_SCHEMA_VERSION:
        raise FutureSchemaError(
            "This library database was created by a newer version of Orpheus "
            f"(schema version {version}); this build supports up to schema "
            f"version {LATEST_SCHEMA_VERSION}. Please update Orpheus to open it."
        )
    return version


def run_migrations(conn: sqlite3.Connection) -> int:
    """
    Apply storage migrations and return resulting schema version.
    """
    configure_connection(conn)
    _ensure_migrations_table(conn)
    version = check_schema_version(conn)

    # Fresh database path. A legacy database without migration metadata
    # (version 0 with tables) takes every step below.
    if version == 0 and not (_table_exists(conn, "books") or _table_exists(conn, "chapters")):
        _apply_bootstrap(conn)
        return LATEST_SCHEMA_VERSION

    for target, disable_foreign_keys in MIGRATIONS:
        if version < target:
            _apply_migration(conn, target, migration_step(target), disable_foreign_keys=disable_foreign_keys)
            version = target
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


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """Persist chapter starts on the packaged audio timeline."""
    if not _column_exists(conn, "chapters", "packaged_start_ms"):
        conn.execute("ALTER TABLE chapters ADD COLUMN packaged_start_ms INTEGER")


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """Give existing and new libraries a durable identity and replacement epoch."""
    conn.execute("CREATE TABLE IF NOT EXISTS library_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany(
        "INSERT OR IGNORE INTO library_metadata(key, value) VALUES (?, ?)",
        [(key, uuid.uuid4().hex) for key in ("library_id", "library_epoch")],
    )


# Ordered (version, disable_foreign_keys) steps; step N runs ``_migrate_to_vN``
# in its own transaction together with its schema_migrations row.
MIGRATIONS = ((2, False), (3, False), (4, False), (5, True), (6, False), (7, False), (8, False), (9, False))


def migration_step(version: int):
    """Return the migration function for ``version``, looked up at call time."""
    return globals()[f"_migrate_to_v{version}"]


def prepare_restore_identity(
    conn: sqlite3.Connection, epoch: str, live_library_id: str | None = None,
) -> None:
    """Repair identity in private staging while retaining the saved library ID."""
    conn.execute("CREATE TABLE IF NOT EXISTS library_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    identities = conn.execute("SELECT value FROM library_metadata WHERE key='library_id'").fetchall()
    if len(identities) > 1:
        raise ValueError("Backup has an ambiguous library identity")
    library_id = identities[0][0] if identities else None
    if not isinstance(library_id, str) or not library_id.strip():
        library_id = (
            live_library_id if isinstance(live_library_id, str) and live_library_id.strip()
            else uuid.uuid4().hex
        )
        conn.execute(
            "INSERT INTO library_metadata(key, value) VALUES ('library_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (library_id,),
        )
    conn.execute(
        "INSERT INTO library_metadata(key, value) VALUES ('library_epoch', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (epoch,),
    )
    for key, expected in (("library_id", library_id), ("library_epoch", epoch)):
        rows = conn.execute("SELECT value FROM library_metadata WHERE key=?", (key,)).fetchall()
        if len(rows) != 1 or rows[0][0] != expected:
            raise ValueError("Backup cannot preserve a valid library identity")

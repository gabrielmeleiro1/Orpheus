"""
SQLite Repository Implementation
===============================
Concrete implementation of IBookRepository using SQLite.
"""

from __future__ import annotations

import logging
import json
import math
import os as os
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import sqlite3

from modules.contracts.durable_files import atomic_write_json
from modules.storage.db_ownership import acquire_database
from modules.storage.managed_roots import claim_sources_root
from modules.storage.restore_recovery import recover_restore_rollback
from modules.storage.migrations import FutureSchemaError, check_schema_version, run_migrations
from modules.storage.deletion import preserve_delete_transactions, sync_directory, sync_file
from modules.storage.maintenance import resolve_stored_path, workspace_root_for_db
from modules.storage.permissions import ensure_owner_only_directory, owner_only
from modules.storage.repository import IBookRepository
from modules.storage.models import (
    Book,
    BookCreate,
    BookSummary,
    BookFilters,
    Chapter,
    ChapterCreate,
    ConversionMode,
    PlaybackStatus,
    SourceType,
)

logger = logging.getLogger(__name__)

_INSERT_BOOK_SQL = """
INSERT INTO books (
    title,
    author,
    source_path,
    source_type,
    conversion_mode,
    total_chapters,
    playback_path,
    playback_status,
    conversion_runtime_options,
    tags,
    favorite
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _normalize_search(value: str | None) -> str:
    return unicodedata.normalize("NFC", value or "").casefold()


# SQLite does not type-check TIMESTAMP/REAL/INTEGER columns, so an old build,
# a manual edit or a partial write can leave text a field cannot decode. Reads
# keep such a row visible without that value; restore validation stays strict.
_reported_unreadable_values: set[tuple[str, int, str]] = set()


def _report_unreadable(table: str, row_id: Any, column: str, value: Any) -> None:
    key = (table, row_id, column)
    if key in _reported_unreadable_values:
        return
    _reported_unreadable_values.add(key)
    logger.warning(
        "Library %s row %s has an unreadable %s value %r; it is shown without that value.",
        table, row_id, column, value,
    )


def _decode_timestamp(row: sqlite3.Row, column: str, *, table: str = "books", strict: bool = False) -> datetime | None:
    try:
        return datetime.fromisoformat(row[column])
    except (TypeError, ValueError):
        if strict:
            raise
        _report_unreadable(table, row["id"], column, row[column])
        return None


def _decode_position(row: sqlite3.Row, *, strict: bool = False) -> float:
    raw = row["playback_position_sec"] or 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if strict:
            raise
        value = math.nan
    if strict or math.isfinite(value):
        return value
    _report_unreadable("books", row["id"], "playback_position_sec", raw)
    return 0.0


def _decode_optional_int(raw: Any, chapter_id: Any, column: str) -> int | None:
    if raw is None or (isinstance(raw, int) and not isinstance(raw, bool)):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    _report_unreadable("chapters", chapter_id, column, raw)
    return None


def _is_explicit_sqlite_corruption(error: sqlite3.DatabaseError) -> bool:
    """Return true only for SQLite's explicit corruption classifications."""
    error_code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(error_code, int):
        return False
    primary_code = error_code & 0xFF
    return primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def _unique_quarantine_path(base_path: Path) -> Path:
    """Return a sibling path without ever selecting an existing quarantine."""
    if not base_path.exists():
        return base_path
    counter = 1
    while True:
        candidate = base_path.with_name(f"{base_path.name}_{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


_QUARANTINE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _write_quarantine_record(path: Path, record: dict) -> None:
    atomic_write_json(
        path, record, temporary_path=path.with_name(path.name + ".tmp"),
        before_write=owner_only, cleanup="none", sync_parent=sync_directory,
        replace=Path.replace,
    )


class LibraryReplacedError(Exception):
    """The selected library was replaced before an operation acquired ownership."""

    code = "library_replaced"

    def __init__(self):
        super().__init__("The library was replaced. Reload the selected book and try again.")


class LibraryReadOnlyError(RuntimeError):
    """The library database cannot be written, so it cannot be opened or migrated."""

    code = "library_read_only"

    def __init__(self, path: Path):
        super().__init__(
            f"The library database is read-only: {path}. "
            "Make the file and its folder writable, then restart Orpheus."
        )


class SQLiteRepository(IBookRepository):
    """
    SQLite implementation of the book repository interface.

    Uses a single persistent connection with a threading lock instead of
    opening/closing a new connection per operation. PRAGMAs are applied
    once at init time.
    """

    def __init__(
        self,
        db_path: Path | str = "data/audiobooks.db",
        *,
        output_dir: Path | str | None = None,
        temp_dir: Path | str | None = None,
    ):
        """
        Initialize SQLite repository.

        Args:
            db_path: Path to SQLite database file
        """
        self._ownership = acquire_database(db_path)
        self._db_path = self._ownership.path
        self._lock = self._ownership.lock
        try:
            with self._lock:
                ensure_owner_only_directory(self.db_path.parent)
                recover_restore_rollback(self.db_path)
                self._resume_quarantine()
                self.database_replaced = self.quarantine is not None
                self.library_generation = 0
                workspace_root = workspace_root_for_db(self.db_path)
                self._output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else workspace_root / "output"
                self._temp_dir = Path(temp_dir).expanduser().resolve() if temp_dir is not None else workspace_root / "temp"
                self._open_connection()
                library_id = self._storage_library_id()
                self._claim_storage(library_id)
                # PRAGMAs are applied by run_migrations() -> configure_connection().
                self._init_schema()
                migrated_id = self.get_library_identity()["library_id"]
                if migrated_id != library_id:
                    self._claim_storage(migrated_id)
        except BaseException as error:
            if hasattr(self, "_conn"):
                self._conn.close()
            self._ownership.close()
            if isinstance(error, sqlite3.OperationalError) and (
                getattr(error, "sqlite_errorcode", None) or 0
            ) & 0xFF == sqlite3.SQLITE_READONLY:
                raise LibraryReadOnlyError(self.db_path) from error
            raise

    def _open_connection(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        owner_only(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("normalize_search", 1, _normalize_search, deterministic=True)

    def _storage_library_id(self) -> str | None:
        # Read identity under the database lease before storage claims or any
        # migrations and quarantine can change files in the managed roots.
        try:
            check_schema_version(self._conn)
            if not self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'library_metadata'"
            ).fetchone():
                return None
            row = self._conn.execute("SELECT value FROM library_metadata WHERE key = 'library_id'").fetchone()
            return row[0] if row else None
        except sqlite3.DatabaseError as error:
            if not _is_explicit_sqlite_corruption(error):
                raise
            return None

    def _claim_storage(self, library_id: str | None) -> None:
        self._fresh_storage_claim: tuple[Path, ...] = ()
        sources = self.db_path.parent / "sources"
        claim_sources_root(
            self.db_path, sources, library_id=library_id,
            connection=self._conn, additional_roots=(self._output_dir, self._temp_dir),
            temp_root=self._temp_dir,
        )
        self._fresh_storage_claim = tuple(
            Path(root).expanduser().resolve() for root in (sources, self._output_dir, self._temp_dir)
        )

    def take_fresh_storage_claim(self) -> tuple[Path, ...]:
        """The roots the latest claim verified, handed out once.

        That claim inspected every ownership record in and above these roots
        under the cross-process claim gate. The single caller that runs right
        after it (the library service being built, or the sweep that follows a
        restore) may rely on it instead of walking the same trees again; any
        later check walks them itself.
        """
        with self._lock:
            roots, self._fresh_storage_claim = getattr(self, "_fresh_storage_claim", ()), ()
        return roots

    @property
    def db_path(self) -> Path:
        return self._db_path

    @db_path.setter
    def db_path(self, path: Path | str) -> None:
        if hasattr(self, "_db_path"):
            raise AttributeError("The database path is immutable")
        self._db_path = Path(path).expanduser().resolve()

    @contextmanager
    def _connection(self):
        """Context manager that yields the shared connection under a lock."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                # The connection is shared: an interrupted operation (including
                # KeyboardInterrupt) must not leave writes for the next commit.
                self._conn.rollback()
                raise

    @contextmanager
    def library_snapshot(self, expected_epoch: str | None = None, *, mutation: bool = False):
        """Keep library identity and the operations using it in one restore boundary."""
        with self._lock:
            identity = self.get_library_identity()
            if expected_epoch is not None and expected_epoch != identity["library_epoch"]:
                raise LibraryReplacedError()
            if mutation and expected_epoch is None:
                logger.warning("Library mutation without expected library epoch")
            yield identity

    def get_library_identity(self) -> dict[str, str]:
        with self._lock:
            return dict(self._conn.execute(
                "SELECT key, value FROM library_metadata WHERE key IN ('library_id', 'library_epoch')"
            ))

    def close(self) -> None:
        """Close the persistent database connection."""
        with self._lock:
            self._fresh_storage_claim = ()
            self._conn.close()
            self._ownership.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            # Construction may have stopped before the connection was created.
            pass

    @property
    def quarantine(self) -> dict[str, str] | None:
        status_path = self.db_path.with_name(self.db_path.name + ".quarantine.json")
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.exception("Could not read database quarantine status at %s", status_path)
            return None

    def _quarantine_intent_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".quarantine-intent.json")

    def _clear_quarantine_intent(self) -> None:
        self._quarantine_intent_path().unlink()
        sync_directory(self.db_path.parent)

    def _move_quarantine_files(self, intent: dict, *, rollback: bool = False) -> None:
        entries = reversed(intent["files"]) if rollback else intent["files"]
        for entry in entries:
            original, quarantined = Path(entry["source"]), Path(entry["destination"])
            source, target = (quarantined, original) if rollback else (original, quarantined)
            if source.exists():
                if target.exists():
                    raise OSError(f"Both database quarantine paths exist: {source}, {target}")
                sync_file(source)
                source.replace(target)
                sync_directory(source.parent)
            elif not target.exists() and entry["required"]:
                raise OSError(f"Database quarantine file is missing: {original}")

    def _complete_quarantine(self, intent: dict) -> None:
        status_path = self.db_path.with_name(self.db_path.name + ".quarantine.json")
        _write_quarantine_record(status_path, intent["status"])
        intent["phase"] = "complete"
        _write_quarantine_record(self._quarantine_intent_path(), intent)
        self._clear_quarantine_intent()

    def _resume_quarantine(self) -> None:
        """Reconcile the recorded main/WAL/SHM set before SQLite can open it."""
        try:
            intent = json.loads(self._quarantine_intent_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        db_path = self.db_path
        if intent.get("version") != 1 or intent.get("phase") not in {"moving", "rollback", "complete"}:
            raise ValueError("Unrecognized database quarantine intent")
        entries = intent.get("files", [])
        # Older intents moved only the main, WAL and SHM files.
        if len(entries) not in (3, len(_QUARANTINE_SUFFIXES)):
            raise ValueError("Incomplete database quarantine intent")
        for entry, suffix in zip(entries, _QUARANTINE_SUFFIXES):
            source, target = Path(entry["source"]), Path(entry["destination"])
            if (source != Path(str(db_path) + suffix) or target.parent != db_path.parent
                    or not target.name.startswith(db_path.name + ".corrupt-")
                    or source.is_symlink() or target.is_symlink()):
                raise ValueError("Invalid database quarantine file mapping")
        if intent["phase"] == "rollback":
            self._move_quarantine_files(intent, rollback=True)
            self._clear_quarantine_intent()
        else:
            if intent["phase"] == "moving":
                self._move_quarantine_files(intent)
            self._complete_quarantine(intent)

    def mark_database_replaced(self) -> None:
        self.database_replaced = True
        self.library_generation += 1

    def _init_schema(self):
        """Initialize or migrate database schema.

        A corrupt/truncated/non-SQLite file raises ``sqlite3.DatabaseError`` here.
        Rather than let that propagate out of the (lazily constructed) repository
        and turn every API request into an opaque 500 with no path to recovery, we
        confirm the damage, quarantine the unreadable file aside, and recreate an
        empty library so the app comes back up. A ``FutureSchemaError`` (a real
        user's data written by a newer Orpheus build) is deliberately *not* caught
        — it must surface so the user is told to update, never silently wiped.
        """
        try:
            with self._connection() as conn:
                run_migrations(conn)
        except FutureSchemaError:
            raise
        except sqlite3.DatabaseError as error:
            if not _is_explicit_sqlite_corruption(error):
                self._conn.close()
                raise
            self._quarantine_corrupt_database(error)

    def _quarantine_corrupt_database(self, error: sqlite3.DatabaseError) -> None:
        """Rename an unreadable database aside and recreate a fresh empty one."""
        try:
            integrity_row = self._conn.execute("PRAGMA integrity_check").fetchone()
            integrity = integrity_row[0] if integrity_row else "unknown"
        except sqlite3.DatabaseError:
            # A file too damaged for integrity_check itself is corruption enough.
            integrity = "unreadable"

        try:
            self._conn.close()
        except sqlite3.DatabaseError:
            pass

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        quarantine_path = _unique_quarantine_path(
            self.db_path.with_name(f"{self.db_path.name}.corrupt-{stamp}")
        )
        preserve_delete_transactions(self._temp_dir / "delete_staging")
        files = []
        # A rollback journal left beside the damaged file would be replayed
        # into the fresh database, so it moves with the database it belongs to.
        for suffix in _QUARANTINE_SUFFIXES:
            source = self.db_path.with_name(self.db_path.name + suffix)
            destination = _unique_quarantine_path(quarantine_path.with_name(quarantine_path.name + suffix))
            files.append({
                "source": str(source.resolve()),
                "destination": str(destination.resolve()),
                "required": source.exists(),
            })
        intent = {
            "version": 1,
            "phase": "moving",
            "files": files,
            "status": {
                "path": str(quarantine_path.resolve()),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        }
        _write_quarantine_record(self._quarantine_intent_path(), intent)
        try:
            self._move_quarantine_files(intent)
        except OSError:
            intent["phase"] = "rollback"
            _write_quarantine_record(self._quarantine_intent_path(), intent)
            try:
                self._move_quarantine_files(intent, rollback=True)
                self._clear_quarantine_intent()
            except OSError:
                logger.exception("Database quarantine rollback will resume at startup")
            raise
        self._complete_quarantine(intent)
        self.mark_database_replaced()

        logger.warning(
            "Storage database at %s was unreadable (integrity_check: %s; %s); "
            "quarantined it to %s and recreated an empty library.",
            self.db_path,
            integrity,
            error,
            quarantine_path,
        )

        self._open_connection()
        with self._connection() as conn:
            run_migrations(conn)

    def _workspace_root(self) -> Path:
        return workspace_root_for_db(self.db_path)

    def _resolve_stored_path(self, raw_path: str) -> Path:
        resolved = resolve_stored_path(
            raw_path,
            self._workspace_root(),
            output_root=self._output_dir,
            temp_root=self._temp_dir,
        )
        if resolved is None:
            raise ValueError("raw_path must not be empty")
        return resolved

    @staticmethod
    def _select_book_row(conn: sqlite3.Connection, book_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        ).fetchone()

    @staticmethod
    def _row_to_book(row: sqlite3.Row, *, strict: bool = False) -> Book:
        """Convert database row to Book dataclass.

        ``strict`` raises on undecodable timestamps and positions (restore
        validation); reads instead keep the book with those values cleared.
        """
        return Book(
            id=row["id"],
            title=row["title"],
            author=row["author"],
            source_path=row["source_path"],
            source_type=SourceType(row["source_type"]),
            conversion_mode=ConversionMode(
                row["conversion_mode"] or ConversionMode.AUDIOBOOK.value
            ),
            total_chapters=row["total_chapters"],
            playback_path=row["playback_path"],
            playback_status=PlaybackStatus(
                row["playback_status"] or PlaybackStatus.MISSING.value
            ),
            playback_position_sec=_decode_position(row, strict=strict),
            created_at=_decode_timestamp(row, "created_at", strict=strict),
            updated_at=_decode_timestamp(row, "updated_at", strict=strict),
            conversion_runtime_options=row["conversion_runtime_options"],
            tags=row["tags"] or "",
            favorite=bool(row["favorite"] or 0),
        )

    @staticmethod
    def _row_to_chapter(row: sqlite3.Row) -> Chapter:
        """Convert database row to Chapter dataclass."""
        return Chapter(
            id=row["id"],
            book_id=row["book_id"],
            chapter_number=row["chapter_number"],
            title=row["title"],
            duration_ms=_decode_optional_int(row["duration_ms"], row["id"], "duration_ms"),
            mp3_path=row["mp3_path"],
            packaged_start_ms=_decode_optional_int(row["packaged_start_ms"], row["id"], "packaged_start_ms"),
        )

    @staticmethod
    def _book_insert_values(book: BookCreate) -> tuple[Any, ...]:
        return (
            book.title,
            book.author,
            book.source_path,
            book.source_type.value,
            book.conversion_mode.value,
            book.total_chapters,
            book.playback_path,
            book.playback_status.value,
            book.conversion_runtime_options,
            book.tags,
            int(bool(book.favorite)),
        )

    # ==================== Book Operations ====================

    def create_book(self, book: BookCreate) -> Book:
        """Create a new book record."""
        with self._connection() as conn:
            cursor = conn.execute(_INSERT_BOOK_SQL, self._book_insert_values(book))
            row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_book(row)

    def create_books(
        self,
        books: list[BookCreate],
        *,
        batch_size: int = 500,
    ) -> int:
        """
        Insert many books in bounded transactions via the public schema.

        Returns the number of rows inserted.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if not books:
            return 0
        inserted = 0
        for start in range(0, len(books), batch_size):
            chunk = books[start : start + batch_size]
            rows = [self._book_insert_values(book) for book in chunk]
            with self._connection() as conn:
                conn.executemany(_INSERT_BOOK_SQL, rows)
            inserted += len(rows)
        return inserted

    def create_book_with_chapters(
        self,
        book: BookCreate,
        chapters: list[ChapterCreate],
    ) -> tuple[Book, list[Chapter]]:
        """
        Create a book and its chapters in a single transaction.
        """
        with self._connection() as conn:
            cursor = conn.execute(_INSERT_BOOK_SQL, self._book_insert_values(book))
            book_id = int(cursor.lastrowid)

            if chapters:
                rows = [
                    (
                        book_id,
                        chapter.chapter_number,
                        chapter.title,
                        chapter.duration_ms,
                        chapter.mp3_path,
                        chapter.packaged_start_ms,
                    )
                    for chapter in chapters
                ]
                conn.executemany(
                    """
                    INSERT INTO chapters (book_id, chapter_number, title, duration_ms, mp3_path, packaged_start_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            book_row = self._select_book_row(conn, book_id)
            chapter_rows = conn.execute(
                """
                SELECT * FROM chapters
                WHERE book_id = ?
                ORDER BY chapter_number
                """,
                (book_id,),
            ).fetchall()

            return self._row_to_book(book_row), [
                self._row_to_chapter(row) for row in chapter_rows
            ]

    def get_book(self, book_id: int) -> Optional[Book]:
        """Get a book by ID."""
        with self._connection() as conn:
            row = self._select_book_row(conn, book_id)
            if row:
                return self._row_to_book(row)
            return None

    def get_book_with_chapters(
        self, book_id: int
    ) -> Optional[tuple[Book, list[Chapter]]]:
        """Get a book and its ordered chapters in one repository call."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    b.*,
                    c.id AS chapter_id,
                    c.book_id AS chapter_book_id,
                    c.chapter_number AS chapter_number,
                    c.title AS chapter_title,
                    c.duration_ms AS chapter_duration_ms,
                    c.mp3_path AS chapter_mp3_path,
                    c.packaged_start_ms AS chapter_packaged_start_ms
                FROM books b
                LEFT JOIN chapters c ON c.book_id = b.id
                WHERE b.id = ?
                ORDER BY c.chapter_number
                """,
                (book_id,),
            ).fetchall()
            if not rows:
                return None

            book = self._row_to_book(rows[0])
            chapters = [
                Chapter(
                    id=row["chapter_id"],
                    book_id=row["chapter_book_id"],
                    chapter_number=row["chapter_number"],
                    title=row["chapter_title"],
                    duration_ms=_decode_optional_int(row["chapter_duration_ms"], row["chapter_id"], "duration_ms"),
                    mp3_path=row["chapter_mp3_path"],
                    packaged_start_ms=_decode_optional_int(
                        row["chapter_packaged_start_ms"], row["chapter_id"], "packaged_start_ms",
                    ),
                )
                for row in rows
                if row["chapter_id"] is not None
            ]
            return book, chapters

    def get_book_by_source_path(self, source_path: str) -> Optional[Book]:
        """Get a book by its source file path."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM books WHERE source_path = ?",
                (source_path,),
            ).fetchone()
            if row:
                return self._row_to_book(row)
            return None

    def list_books(self, filters: Optional[BookFilters] = None) -> list[BookSummary]:
        """List books with optional filtering."""
        filters = filters or BookFilters()

        where_clauses = ["1=1"]
        params: list[object] = []

        if filters.search_query:
            where_clauses.append(r"(normalize_search(title) LIKE ? ESCAPE '\' OR normalize_search(author) LIKE ? ESCAPE '\')")
            escaped_query = (
                _normalize_search(filters.search_query).replace("\\", "\\\\")
                .replace("%", r"\%")
                .replace("_", r"\_")
            )
            search_pattern = f"%{escaped_query}%"
            params.extend([search_pattern, search_pattern])

        if filters.source_type:
            where_clauses.append("source_type = ?")
            params.append(filters.source_type.value)

        if filters.conversion_mode:
            where_clauses.append("conversion_mode = ?")
            params.append(filters.conversion_mode.value)

        if filters.favorite is not None:
            where_clauses.append("favorite = ?")
            params.append(1 if filters.favorite else 0)

        where_sql = " AND ".join(where_clauses)
        query = f"""
            WITH filtered_books AS (
                SELECT *
                FROM books
                WHERE {where_sql}
                ORDER BY created_at DESC, id ASC
                LIMIT ? OFFSET ?
            )
            SELECT
                b.id,
                b.title,
                b.author,
                b.total_chapters,
                b.created_at,
                b.source_path,
                b.conversion_mode,
                b.tags,
                b.favorite,
                b.playback_path,
                CASE
                    WHEN b.playback_status = ?
                         AND b.playback_path IS NOT NULL
                         AND TRIM(b.playback_path) <> ''
                    THEN b.total_chapters
                    ELSE COALESCE(c.completed_chapters, 0)
                END AS completed_chapters
            FROM filtered_books b
            LEFT JOIN (
                SELECT
                    book_id,
                    SUM(
                        CASE
                            WHEN mp3_path IS NOT NULL AND TRIM(mp3_path) <> '' THEN 1
                            ELSE 0
                        END
                    ) AS completed_chapters
                FROM chapters
                WHERE book_id IN (SELECT id FROM filtered_books)
                GROUP BY book_id
            ) c ON c.book_id = b.id
            ORDER BY b.created_at DESC, b.id ASC
        """
        params.extend([filters.limit, filters.offset, PlaybackStatus.PACKAGED.value])

        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                BookSummary(
                    id=row["id"],
                    title=row["title"],
                    author=row["author"],
                    total_chapters=row["total_chapters"],
                    created_at=_decode_timestamp(row, "created_at"),
                    completed_chapters=row["completed_chapters"],
                    source_path=row["source_path"],
                    conversion_mode=ConversionMode(
                        row["conversion_mode"] or ConversionMode.AUDIOBOOK.value
                    ),
                    tags=row["tags"] or "",
                    favorite=bool(row["favorite"] or 0),
                    playback_path=row["playback_path"],
                )
                for row in rows
            ]

    def count_books(self) -> int:
        """Return the total number of indexed books."""
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS book_count FROM books").fetchone()
            return int(row["book_count"] or 0)

    def list_source_paths(self, exclude_book_id: int | None = None) -> list[str]:
        """Read reference data without aggregating chapters or building summaries."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_path FROM books WHERE (? IS NULL OR id <> ?)",
                (exclude_book_id, exclude_book_id),
            )
            return [row[0] for row in rows if row[0]]

    def list_book_sources(self) -> list[tuple[int, str, str]]:
        """``(id, title, source_path)`` for every book, in library order."""
        with self._connection() as conn:
            return [
                (row[0], row[1], row[2])
                for row in conn.execute("SELECT id, title, source_path FROM books ORDER BY created_at DESC, id ASC")
            ]

    def list_chapter_audio_paths(self, book_ids: list[int]) -> dict[int, list[str]]:
        """Recorded chapter audio paths per book, in chapter order, in one query per 500 books."""
        paths: dict[int, list[str]] = {}
        ids = list(dict.fromkeys(book_ids))
        with self._connection() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                rows = conn.execute(
                    f"""
                    SELECT book_id, mp3_path FROM chapters
                    WHERE book_id IN ({",".join("?" * len(chunk))})
                      AND mp3_path IS NOT NULL AND mp3_path <> ''
                    ORDER BY book_id, chapter_number
                    """,
                    chunk,
                )
                for book_id, mp3_path in rows:
                    paths.setdefault(book_id, []).append(mp3_path)
        return paths

    def list_artifact_paths(self, exclude_book_id: int) -> list[str]:
        """Read every retained reference in one query under the repository lock."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT source_path FROM books WHERE id <> ?
                UNION SELECT playback_path FROM books WHERE id <> ?
                UNION SELECT mp3_path FROM chapters WHERE book_id <> ?
                """,
                (exclude_book_id, exclude_book_id, exclude_book_id),
            )
            return [row[0] for row in rows if row[0]]

    def update_book(self, book_id: int, **updates) -> Optional[Book]:
        """Update book fields."""
        allowed_fields = {
            "title",
            "author",
            "total_chapters",
            "source_path",
            "tags",
            "favorite",
            "playback_path",
            "playback_status",
            "conversion_mode",
            "conversion_runtime_options",
        }
        filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}

        if not filtered_updates:
            return self.get_book(book_id)

        normalized_updates = {}
        for key, value in filtered_updates.items():
            if key == "favorite":
                normalized_updates[key] = 1 if bool(value) else 0
            elif key == "playback_status" and isinstance(value, PlaybackStatus):
                normalized_updates[key] = value.value
            elif key == "conversion_mode" and isinstance(value, ConversionMode):
                normalized_updates[key] = value.value
            else:
                normalized_updates[key] = value

        set_clause = ", ".join(f"{k} = ?" for k in normalized_updates.keys())
        set_clause += ", updated_at = CURRENT_TIMESTAMP"

        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE books SET {set_clause} WHERE id = ?",
                (*normalized_updates.values(), book_id),
            )
            if cursor.rowcount == 0:
                return None

            row = self._select_book_row(conn, book_id)
            return self._row_to_book(row) if row else None

    def update_book_playback_position(
        self, book_id: int, position_sec: float
    ) -> Optional[Book]:
        """Persist playback position for a book."""
        normalized_position = float(position_sec)
        if not math.isfinite(normalized_position):
            raise ValueError("position_sec must be finite")
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE books
                SET playback_position_sec = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (max(0.0, normalized_position), book_id),
            )
            if cursor.rowcount == 0:
                return None

            row = self._select_book_row(conn, book_id)
            return self._row_to_book(row) if row else None

    @staticmethod
    def _repair_update_value(update: Any, field: str) -> Any:
        if isinstance(update, dict):
            return update[field]
        return getattr(update, field)

    def apply_repair_result(
        self,
        book_id: int,
        *,
        repaired_chapter_updates: list[Any],
        playback_path: str,
        playback_status: PlaybackStatus | str,
        total_chapters: int,
    ) -> Book:
        """Atomically apply repaired chapters and final packaged book metadata."""
        if isinstance(playback_status, PlaybackStatus):
            playback_status_value = playback_status.value
        else:
            playback_status_value = str(playback_status)

        with self._connection() as conn:
            for update in repaired_chapter_updates:
                chapter_id = int(self._repair_update_value(update, "chapter_id"))
                duration_ms = self._repair_update_value(update, "duration_ms")
                mp3_path = self._repair_update_value(update, "mp3_path")
                updates = ["mp3_path = ?"]
                params: list[Any] = [mp3_path]
                if isinstance(update, dict):
                    has_packaged_start = "packaged_start_ms" in update
                else:
                    has_packaged_start = hasattr(update, "packaged_start_ms")
                if has_packaged_start:
                    updates.append("packaged_start_ms = ?")
                    params.append(self._repair_update_value(update, "packaged_start_ms"))
                if duration_ms is not None:
                    updates.insert(0, "duration_ms = ?")
                    params.insert(0, duration_ms)

                cursor = conn.execute(
                    f"""
                    UPDATE chapters
                    SET {", ".join(updates)}
                    WHERE id = ? AND book_id = ?
                    """,
                    (*params, chapter_id, book_id),
                )
                if cursor.rowcount != 1:
                    raise LookupError(
                        f"Chapter not found for repair result: {chapter_id}"
                    )

            cursor = conn.execute(
                """
                UPDATE books
                SET playback_path = ?,
                    playback_status = ?,
                    total_chapters = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (playback_path, playback_status_value, total_chapters, book_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"Book not found for repair result: {book_id}")

            row = self._select_book_row(conn, book_id)
            if row is None:
                raise LookupError(f"Book not found for repair result: {book_id}")
            return self._row_to_book(row)

    def delete_book(self, book_id: int) -> bool:
        """Delete a book and its chapters."""
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
            return cursor.rowcount > 0

    # ==================== Chapter Operations ====================

    def create_chapter(self, chapter: ChapterCreate) -> Chapter:
        """Create a new chapter record."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chapters (book_id, chapter_number, title, duration_ms, mp3_path, packaged_start_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chapter.book_id,
                    chapter.chapter_number,
                    chapter.title,
                    chapter.duration_ms,
                    chapter.mp3_path,
                    chapter.packaged_start_ms,
                ),
            )
            row = conn.execute(
                "SELECT * FROM chapters WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_chapter(row)

    def get_chapters(self, book_id: int) -> list[Chapter]:
        """Get all chapters for a book."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chapters
                WHERE book_id = ?
                ORDER BY chapter_number
                """,
                (book_id,),
            ).fetchall()
            return [self._row_to_chapter(row) for row in rows]

    def update_chapter(
        self,
        chapter_id: int,
        duration_ms: Optional[int] = None,
        mp3_path: Optional[str] = None,
    ) -> Optional[Chapter]:
        """Update chapter metadata."""
        updates = []
        params: list[object] = []

        if duration_ms is not None:
            updates.append("duration_ms = ?")
            params.append(duration_ms)

        if mp3_path is not None:
            updates.append("mp3_path = ?")
            params.append(mp3_path)

        with self._connection() as conn:
            if not updates:
                row = conn.execute(
                    "SELECT * FROM chapters WHERE id = ?", (chapter_id,)
                ).fetchone()
                return self._row_to_chapter(row) if row else None

            cursor = conn.execute(
                f"UPDATE chapters SET {', '.join(updates)} WHERE id = ?",
                (*params, chapter_id),
            )
            if cursor.rowcount == 0:
                return None

            row = conn.execute(
                "SELECT * FROM chapters WHERE id = ?", (chapter_id,)
            ).fetchone()
            return self._row_to_chapter(row) if row else None

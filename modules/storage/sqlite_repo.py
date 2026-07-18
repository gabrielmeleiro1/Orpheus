"""
SQLite Repository Implementation
===============================
Concrete implementation of IBookRepository using SQLite.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import sqlite3

from modules.storage.migrations import run_migrations
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
        self.db_path = Path(db_path)
        ensure_owner_only_directory(self.db_path.parent)
        workspace_root = workspace_root_for_db(self.db_path)
        self._output_dir = Path(output_dir).expanduser() if output_dir is not None else workspace_root / "output"
        self._temp_dir = Path(temp_dir).expanduser() if temp_dir is not None else workspace_root / "temp"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        owner_only(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # PRAGMAs are applied by run_migrations() -> configure_connection().
        self._init_schema()

    @contextmanager
    def _connection(self):
        """Context manager that yields the shared connection under a lock."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        """Close the persistent database connection."""
        with self._lock:
            self._conn.close()

    def _init_schema(self):
        """Initialize or migrate database schema."""
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
    def _row_to_book(row: sqlite3.Row) -> Book:
        """Convert database row to Book dataclass."""
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
            playback_position_sec=float(row["playback_position_sec"] or 0),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
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
            duration_ms=row["duration_ms"],
            mp3_path=row["mp3_path"],
        )

    # ==================== Book Operations ====================

    def create_book(self, book: BookCreate) -> Book:
        """Create a new book record."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
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
                """,
                (
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
                ),
            )
            row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_book(row)

    def create_book_with_chapters(
        self,
        book: BookCreate,
        chapters: list[ChapterCreate],
    ) -> tuple[Book, list[Chapter]]:
        """
        Create a book and its chapters in a single transaction.
        """
        with self._connection() as conn:
            cursor = conn.execute(
                """
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
                """,
                (
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
                ),
            )
            book_id = int(cursor.lastrowid)

            if chapters:
                rows = [
                    (
                        book_id,
                        chapter.chapter_number,
                        chapter.title,
                        chapter.duration_ms,
                        chapter.mp3_path,
                    )
                    for chapter in chapters
                ]
                conn.executemany(
                    """
                    INSERT INTO chapters (book_id, chapter_number, title, duration_ms, mp3_path)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            book_row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
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
            row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
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
                    c.mp3_path AS chapter_mp3_path
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
                    duration_ms=row["chapter_duration_ms"],
                    mp3_path=row["chapter_mp3_path"],
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
            where_clauses.append(r"(title LIKE ? ESCAPE '\' OR author LIKE ? ESCAPE '\')")
            escaped_query = (
                filters.search_query.replace("\\", "\\\\")
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
                    created_at=datetime.fromisoformat(row["created_at"]),
                    completed_chapters=row["completed_chapters"],
                    source_path=row["source_path"],
                    conversion_mode=ConversionMode(
                        row["conversion_mode"] or ConversionMode.AUDIOBOOK.value
                    ),
                    tags=row["tags"] or "",
                    favorite=bool(row["favorite"] or 0),
                )
                for row in rows
            ]

    def count_books(self) -> int:
        """Return the total number of indexed books."""
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS book_count FROM books").fetchone()
            return int(row["book_count"] or 0)

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

            row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            return self._row_to_book(row) if row else None

    def update_book_playback_position(
        self, book_id: int, position_sec: float
    ) -> Optional[Book]:
        """Persist playback position for a book."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE books
                SET playback_position_sec = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (max(0.0, float(position_sec)), book_id),
            )
            if cursor.rowcount == 0:
                return None

            row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
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

            row = conn.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
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
                INSERT INTO chapters (book_id, chapter_number, title, duration_ms, mp3_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    chapter.book_id,
                    chapter.chapter_number,
                    chapter.title,
                    chapter.duration_ms,
                    chapter.mp3_path,
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

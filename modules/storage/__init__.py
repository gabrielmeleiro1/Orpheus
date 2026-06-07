"""
Storage Module
==============
Handles SQLite database operations and file management.

Repository Pattern:
    - IBookRepository: Abstract interface for storage
    - SQLiteRepository: Concrete SQLite implementation
    - Future: PostgreSQLRepository for production use
"""

# Repository Pattern
from .repository import IBookRepository
from .sqlite_repo import SQLiteRepository
from .models import (
    Book,
    BookCreate,
    BookSummary,
    BookFilters,
    Chapter,
    ChapterCreate,
    PlaybackStatus,
    SourceType,
    ConversionResult,
)
from .maintenance import (
    backup_database,
    audit_storage_state,
    prune_stale_records_and_files,
)

__all__ = [
    # Repository Pattern
    "IBookRepository",
    "SQLiteRepository",
    # Models
    "Book",
    "BookCreate",
    "BookSummary",
    "BookFilters",
    "Chapter",
    "ChapterCreate",
    "PlaybackStatus",
    "SourceType",
    "ConversionResult",
    # Maintenance
    "backup_database",
    "audit_storage_state",
    "prune_stale_records_and_files",
]

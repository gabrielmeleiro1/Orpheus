"""
Repository Pattern Interface
============================
Abstract base class for book storage operations.
Enables swapping storage backends (SQLite, PostgreSQL, etc.)
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from modules.storage.models import (
    Book,
    BookCreate,
    BookSummary,
    BookFilters,
    Chapter,
    ChapterCreate,
)


class IBookRepository(ABC):
    """
    Abstract repository interface for book storage.
    
    Implementations:
        - SQLiteRepository: Local SQLite storage (current)
        - PostgreSQLRepository: Production PostgreSQL (future)
        - MockRepository: For testing
    """
    
    # ==================== Book Operations ====================
    
    @abstractmethod
    def create_book(self, book: BookCreate) -> Book:
        """
        Create a new book record.
        
        Args:
            book: Book creation data
            
        Returns:
            Created book with assigned ID
        """
        pass

    @abstractmethod
    def create_book_with_chapters(
        self,
        book: BookCreate,
        chapters: list[ChapterCreate],
    ) -> tuple[Book, list[Chapter]]:
        """
        Create a book and all chapter rows in a single transaction.

        Args:
            book: Book creation data
            chapters: Chapter rows to insert for the created book

        Returns:
            Tuple of created book and chapter rows
        """
        pass
    
    @abstractmethod
    def get_book(self, book_id: int) -> Optional[Book]:
        """
        Get a book by ID.
        
        Args:
            book_id: Book identifier
            
        Returns:
            Book if found, None otherwise
        """
        pass

    @abstractmethod
    def get_book_with_chapters(self, book_id: int) -> Optional[tuple[Book, list[Chapter]]]:
        """
        Get a book and its chapters in one repository call.

        Args:
            book_id: Book identifier

        Returns:
            Tuple of book and ordered chapters if found, None otherwise
        """
        pass
    
    @abstractmethod
    def get_book_by_source_path(self, source_path: str) -> Optional[Book]:
        """
        Get a book by its source file path.
        
        Args:
            source_path: Path to source file
            
        Returns:
            Book if found, None otherwise
        """
        pass
    
    @abstractmethod
    def list_books(self, filters: Optional[BookFilters] = None) -> list[BookSummary]:
        """
        List books with optional filtering.
        
        Args:
            filters: Query filters (search, date range, pagination)
            
        Returns:
            List of book summaries
        """
        pass

    @abstractmethod
    def count_books(self) -> int:
        """Return the total number of indexed books without applying list filters."""
        pass
    
    @abstractmethod
    def update_book(self, book_id: int, **updates) -> Optional[Book]:
        """
        Update book fields.
        
        Args:
            book_id: Book to update
            **updates: Fields to update
            
        Returns:
            Updated book if found
        """
        pass

    @abstractmethod
    def update_book_playback_position(self, book_id: int, position_sec: float) -> Optional[Book]:
        """
        Persist the last known playback position for a book.

        Args:
            book_id: Book to update
            position_sec: Playback position in seconds

        Returns:
            Updated book if found
        """
        pass

    @abstractmethod
    def apply_repair_result(
        self,
        book_id: int,
        *,
        repaired_chapter_updates: list[Any],
        playback_path: str,
        playback_status: Any,
        total_chapters: int,
    ) -> Book:
        """
        Atomically apply repaired chapter metadata and final book playback metadata.

        Args:
            book_id: Book repaired by the pipeline
            repaired_chapter_updates: Objects with chapter_id, duration_ms, and mp3_path
            playback_path: Final packaged playback artifact path
            playback_status: Final playback status
            total_chapters: Chapter count reported by the repair result

        Returns:
            Updated book.

        Raises:
            LookupError: If the book or any chapter row is missing.
        """
        pass
    
    @abstractmethod
    def delete_book(self, book_id: int) -> bool:
        """
        Delete a book and its chapters.
        
        Args:
            book_id: Book to delete
            
        Returns:
            True if deleted, False if not found
        """
        pass
    
    # ==================== Chapter Operations ====================
    
    @abstractmethod
    def create_chapter(self, chapter: ChapterCreate) -> Chapter:
        """
        Create a new chapter record.
        
        Args:
            chapter: Chapter creation data
            
        Returns:
            Created chapter with assigned ID
        """
        pass
    
    @abstractmethod
    def get_chapters(self, book_id: int) -> list[Chapter]:
        """
        Get all chapters for a book.
        
        Args:
            book_id: Book identifier
            
        Returns:
            List of chapters ordered by chapter_number
        """
        pass
    
    @abstractmethod
    def update_chapter(
        self,
        chapter_id: int,
        duration_ms: Optional[int] = None,
        mp3_path: Optional[str] = None
    ) -> Optional[Chapter]:
        """
        Update chapter metadata.
        
        Args:
            chapter_id: Chapter to update
            duration_ms: New duration in milliseconds
            mp3_path: New MP3 file path
            
        Returns:
            Updated chapter if found
        """
        pass

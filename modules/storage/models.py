"""
Storage Models
==============
Dataclasses for repository pattern data transfer objects.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum


class SourceType(str, Enum):
    """Book source file types."""
    PDF = "pdf"
    EPUB = "epub"
    TXT = "txt"
    MD = "md"


class ConversionMode(str, Enum):
    """High-level conversion product modes."""

    AUDIOBOOK = "audiobook"
    DOCUMENT_AUDIO = "document_audio"


class PlaybackStatus(str, Enum):
    """Canonical playback artifact availability for a book."""

    PACKAGED = "packaged"
    CHAPTER_AUDIO = "chapter_audio"
    MISSING = "missing"


@dataclass
class BookCreate:
    """Data required to create a book record."""
    title: str
    source_path: str
    source_type: SourceType
    conversion_mode: ConversionMode = ConversionMode.AUDIOBOOK
    author: Optional[str] = None
    total_chapters: int = 0
    playback_path: Optional[str] = None
    playback_status: PlaybackStatus = PlaybackStatus.MISSING
    conversion_runtime_options: Optional[str] = None
    tags: str = ""
    favorite: bool = False


@dataclass
class Book:
    """Book record with full data."""
    id: int
    title: str
    author: Optional[str]
    source_path: str
    source_type: SourceType
    conversion_mode: ConversionMode
    total_chapters: int
    playback_path: Optional[str]
    playback_status: PlaybackStatus
    playback_position_sec: float
    created_at: datetime
    updated_at: datetime
    conversion_runtime_options: Optional[str] = None
    tags: str = ""
    favorite: bool = False


@dataclass
class BookSummary:
    """Lightweight book summary for library views."""
    id: int
    title: str
    author: Optional[str]
    total_chapters: int
    created_at: datetime
    completed_chapters: int = 0
    source_path: Optional[str] = None
    conversion_mode: ConversionMode = ConversionMode.AUDIOBOOK
    tags: str = ""
    favorite: bool = False
    source_available: Optional[bool] = None


@dataclass
class ChapterCreate:
    """Data required to create a chapter record."""
    book_id: int
    chapter_number: int
    title: str
    duration_ms: Optional[int] = None
    mp3_path: Optional[str] = None


@dataclass
class Chapter:
    """Chapter record with full data."""
    id: int
    book_id: int
    chapter_number: int
    title: str
    duration_ms: Optional[int]
    mp3_path: Optional[str]


@dataclass
class BookFilters:
    """Filters for book queries."""
    search_query: Optional[str] = None
    source_type: Optional[SourceType] = None
    conversion_mode: Optional[ConversionMode] = None
    favorite: Optional[bool] = None
    limit: int = 50
    offset: int = 0


@dataclass
class ConversionResult:
    """Result of a conversion operation."""
    success: bool
    book_id: Optional[int] = None
    output_path: Optional[str] = None
    total_duration_ms: int = 0
    error_message: Optional[str] = None
    chapters_completed: int = 0
    chapters_total: int = 0

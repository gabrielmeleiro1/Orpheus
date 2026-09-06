"""Source identities shared by ingestion, application, and storage."""

from enum import Enum


class SourceType(str, Enum):
    """Book source file types."""
    PDF = "pdf"
    EPUB = "epub"
    TXT = "txt"
    MD = "md"

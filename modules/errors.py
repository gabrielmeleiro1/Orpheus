"""
Error Handling Module
=====================
Custom exceptions and error handling utilities for Orpheus.
Provides consistent error codes and messages for edge cases.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from modules.contracts.public_errors import PublicError


# EPUB archives are inspected from ZIP metadata before any member is
# decompressed.  These bounds are deliberately generous for ordinary books,
# while preventing a small archive from expanding into unbounded memory.
MAX_EPUB_MEMBER_COUNT = 4096
MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 1000
MIN_EPUB_RATIO_CHECK_BYTES = 1 * 1024 * 1024


class ErrorCode(Enum):
    """Error codes for the audiobook creator."""
    # Ingestion errors (E001-E099)
    E001 = "PDF parsing failed"
    E002 = "EPUB structure invalid"
    E003 = "File corrupted"
    E004 = "Empty chapter detected"
    E005 = "Chapter too long"
    E007 = "PDF OCR required"
    
    # TTS errors (E100-E199)
    E100 = "TTS model load failed"
    E101 = "Speech synthesis failed"
    
    # Audio errors (E200-E299)
    E203 = "FFmpeg not found"
    
    # System errors (E300-E399)
    E300 = "VRAM overflow"
    


@dataclass
class AudiobookError(Exception):
    """Base exception for Orpheus with error codes."""
    code: ErrorCode
    message: str
    details: Optional[str] = None
    file_path: Optional[Path] = None
    
    def __str__(self) -> str:
        base = f"[{self.code.name}] {self.code.value}: {self.message}"
        if self.details:
            base += f" ({self.details})"
        if self.file_path:
            base += f" - File: {self.file_path}"
        return base


class PDFParsingError(AudiobookError):
    """Error during PDF parsing."""
    def __init__(self, message: str, details: str = None, file_path: Path = None):
        super().__init__(
            code=ErrorCode.E001,
            message=message,
            details=details,
            file_path=file_path
        )


class EPUBParsingError(AudiobookError):
    """Error during EPUB parsing."""
    def __init__(self, message: str, details: str = None, file_path: Path = None):
        super().__init__(
            code=ErrorCode.E002,
            message=message,
            details=details,
            file_path=file_path
        )


class CorruptedFileError(AudiobookError):
    """Error when file is corrupted."""
    def __init__(self, message: str, file_path: Path = None):
        super().__init__(
            code=ErrorCode.E003,
            message=message,
            file_path=file_path
        )


class EmptyChapterError(AudiobookError):
    """Error when chapter has no content."""
    def __init__(self, chapter_number: int, chapter_title: str = None):
        super().__init__(
            code=ErrorCode.E004,
            message=f"Chapter {chapter_number} is empty",
            details=chapter_title
        )


class ChapterTooLongError(AudiobookError):
    """Error when chapter exceeds maximum word count."""
    def __init__(self, chapter_number: int, word_count: int, max_words: int = 50000):
        super().__init__(
            code=ErrorCode.E005,
            message=f"Chapter {chapter_number} has {word_count:,} words",
            details=f"Maximum allowed: {max_words:,} words"
        )


class OCRRequiredPDFError(AudiobookError):
    """Error when a PDF appears to require OCR before text extraction."""

    DEFAULT_MESSAGE = (
        "OCR required: this PDF is likely scanned/image-only or has too little "
        "extractable text and is not text-extractable without OCR."
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        details: str | None = None,
        file_path: Path | None = None,
        confidence: float | None = None,
        sampled_pages: int | None = None,
        text_chars: int | None = None,
    ):
        if details is None:
            detail_parts = []
            if confidence is not None:
                detail_parts.append(f"confidence={confidence:.2f}")
            if sampled_pages is not None:
                detail_parts.append(f"sampled_pages={sampled_pages}")
            if text_chars is not None:
                detail_parts.append(f"text_chars={text_chars}")
            details = ", ".join(detail_parts) or None
        super().__init__(
            code=ErrorCode.E007,
            message=message or self.DEFAULT_MESSAGE,
            details=details,
            file_path=file_path,
        )


class ConversionCleanupPendingError(RuntimeError):
    """A start was refused while a terminal job still owns resources."""

    def __init__(self) -> None:
        super().__init__("The previous conversion is still finishing cleanup.")

    @property
    def public_error(self) -> PublicError:
        return PublicError("conversion_cleanup_pending", str(self), 409)


class SourceInputError(ValueError):
    """A source the user chose cannot be used; carries a stable code and plain message."""

    code = "invalid_source"

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status

    @property
    def public_error(self) -> PublicError:
        return PublicError(self.code, str(self), self.status)


class SourceFileNotFoundError(SourceInputError):
    code = "source_not_found"

    def __init__(self, path: Path | str) -> None:
        super().__init__(f"Orpheus could not find {path}. It may have been moved or deleted.")


class UnsupportedSourceTypeError(SourceInputError):
    code = "unsupported_source_type"

    def __init__(self) -> None:
        super().__init__("This file type is not supported. Choose a PDF, EPUB, TXT or Markdown file.")


class WorkerWarmupUnavailableError(RuntimeError):
    """A worker warmup was refused because models or storage are in use."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    @property
    def public_error(self) -> PublicError:
        return PublicError(self.code, str(self), 409)


class TTSModelError(AudiobookError):
    """Error loading TTS model."""
    def __init__(self, message: str, model_name: str = None):
        super().__init__(
            code=ErrorCode.E100,
            message=message,
            details=model_name
        )


class TTSInstallationError(TTSModelError, RuntimeError):
    """Repairable installation failure, also catchable by runtime callers."""

    @property
    def public_error(self) -> PublicError:
        return PublicError("tts_installation_error", self.message, 409)

    def __str__(self) -> str:
        return self.message


class SynthesisError(AudiobookError):
    """Error during speech synthesis."""
    def __init__(self, message: str, chunk_index: int = None):
        super().__init__(
            code=ErrorCode.E101,
            message=message,
            details=f"Chunk {chunk_index}" if chunk_index else None
        )


class FFmpegNotFoundError(AudiobookError):
    """Error when FFmpeg is not installed."""
    
    INSTALL_INSTRUCTIONS = """FFmpeg is required but not found in PATH.

Installation instructions:
  macOS:    brew install ffmpeg
  Ubuntu:   sudo apt update && sudo apt install ffmpeg
  Windows:  winget install Gyan.FFmpeg
            or download from: https://ffmpeg.org/download.html

After installation, ensure 'ffmpeg' is available in your system PATH."""
    
    def __init__(self):
        super().__init__(
            code=ErrorCode.E203,
            message="FFmpeg is required but not found",
            details=self.INSTALL_INSTRUCTIONS
        )


class VRAMOverflowError(AudiobookError):
    """Error when VRAM is exhausted."""
    def __init__(self, model_name: str = None):
        super().__init__(
            code=ErrorCode.E300,
            message="VRAM overflow - model too large for available memory",
            details=model_name
        )


# ===========================================
# Utility Functions
# ===========================================

def validate_pdf(file_path: Path) -> bool:
    """
    Validate that a file is a valid PDF.
    
    Args:
        file_path: Path to the PDF file
        
    Returns:
        True if valid, raises exception otherwise
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    # Check file size (minimum valid PDF is ~25 bytes)
    file_size = file_path.stat().st_size
    if file_size < 25:
        raise CorruptedFileError("File too small to be a valid PDF", file_path)

    # Check PDF header
    with open(file_path, 'rb') as f:
        header = f.read(8)
        if not header.startswith(b'%PDF-'):
            raise CorruptedFileError("Invalid PDF header - file may be corrupted", file_path)

        # Read the last 1KB; a relative seek from the end raises OSError for
        # valid PDFs smaller than the window.
        f.seek(max(0, file_size - 1024))
        tail = f.read()
        if b'%%EOF' not in tail:
            raise CorruptedFileError("Missing PDF EOF marker - file may be truncated", file_path)
    
    return True


def validate_epub(file_path: Path) -> bool:
    """
    Validate that a file is a valid EPUB.
    
    Args:
        file_path: Path to the EPUB file
        
    Returns:
        True if valid, raises exception otherwise
    """
    import zipfile
    
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    # Check if it's a valid ZIP file (EPUBs are ZIP archives)
    if not zipfile.is_zipfile(file_path):
        raise CorruptedFileError("Not a valid ZIP archive - EPUB may be corrupted", file_path)
    
    # Check for required EPUB structure
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            # ZIP metadata contains the uncompressed sizes, so enforce bounds
            # before reading/decompressing even the first content member.
            infos = zf.infolist()
            if len(infos) > MAX_EPUB_MEMBER_COUNT:
                raise EPUBParsingError(
                    "EPUB archive contains too many members",
                    details=(
                        f"Found {len(infos):,}; maximum allowed: "
                        f"{MAX_EPUB_MEMBER_COUNT:,}"
                    ),
                    file_path=file_path,
                )

            total_uncompressed = 0
            for info in infos:
                member_size = info.file_size
                if member_size > MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES:
                    raise EPUBParsingError(
                        f"EPUB archive member exceeds uncompressed size limit: {info.filename}",
                        details=(
                            f"{member_size:,} bytes; maximum allowed: "
                            f"{MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES:,}"
                        ),
                        file_path=file_path,
                    )

                total_uncompressed += member_size
                if total_uncompressed > MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES:
                    raise EPUBParsingError(
                        "EPUB archive exceeds total uncompressed size limit",
                        details=(
                            f"{total_uncompressed:,} bytes; maximum allowed: "
                            f"{MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES:,}"
                        ),
                        file_path=file_path,
                    )

                # Ignore tiny metadata files where a high ratio is harmless;
                # large highly-compressible members are the ZIP-bomb signal.
                if member_size >= MIN_EPUB_RATIO_CHECK_BYTES:
                    compressed_size = info.compress_size
                    if (
                        compressed_size == 0
                        or member_size > compressed_size * MAX_EPUB_COMPRESSION_RATIO
                    ):
                        ratio = "infinite" if compressed_size == 0 else f"{member_size / compressed_size:,.1f}"
                        raise EPUBParsingError(
                            f"EPUB archive member has suspicious compression ratio: {info.filename}",
                            details=(
                                f"ratio {ratio}; maximum allowed: "
                                f"{MAX_EPUB_COMPRESSION_RATIO:,}"
                            ),
                            file_path=file_path,
                        )

            names = [info.filename for info in infos]
            
            # Must have mimetype file
            if 'mimetype' not in names:
                raise EPUBParsingError("Missing mimetype file", file_path=file_path)
            
            # Check mimetype content.  Encrypted members, malformed payloads,
            # and invalid encodings must stay inside the typed validation
            # boundary rather than leaking zipfile/Unicode exceptions.
            try:
                mimetype = zf.read('mimetype').decode('utf-8').strip()
            except Exception as exc:
                raise EPUBParsingError(
                    "Unable to read EPUB mimetype",
                    details=str(exc),
                    file_path=file_path,
                ) from exc
            if mimetype != 'application/epub+zip':
                raise EPUBParsingError(
                    f"Invalid mimetype: {mimetype}",
                    details="Expected: application/epub+zip",
                    file_path=file_path
                )
            
            # Must have META-INF/container.xml
            if 'META-INF/container.xml' not in names:
                raise EPUBParsingError(
                    "Missing container.xml",
                    details="Required EPUB structure file not found",
                    file_path=file_path
                )
    except zipfile.BadZipFile:
        raise CorruptedFileError("Corrupted ZIP structure in EPUB", file_path)
    
    return True


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing/replacing invalid characters.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename safe for all operating systems
    """
    import re
    
    # Characters not allowed in filenames on various OSes
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    
    # Replace invalid characters with underscores
    sanitized = re.sub(invalid_chars, '_', filename)
    
    # Remove leading/trailing spaces and dots
    sanitized = sanitized.strip(' .')
    
    # Collapse multiple underscores
    sanitized = re.sub(r'_+', '_', sanitized)
    
    # Ensure filename is not empty
    if not sanitized:
        sanitized = "untitled"
    
    # Limit length (255 bytes for most filesystems, keep some margin)
    if len(sanitized.encode('utf-8')) > 200:
        # Truncate while preserving a real extension. Text after a dot in a
        # title ("Mr. Smith ...") is not an extension and may exceed the budget.
        name, ext = Path(sanitized).stem, Path(sanitized).suffix
        if len(ext.encode('utf-8')) > 16 or ' ' in ext:
            name, ext = sanitized, ''
        max_name_len = 200 - len(ext.encode('utf-8'))
        name = name.encode('utf-8')[:max_name_len].decode('utf-8', errors='ignore')
        # Windows drops trailing dots and spaces, so the cut must not end on one.
        sanitized = (name.rstrip(' .') or "untitled") + ext

    return sanitized

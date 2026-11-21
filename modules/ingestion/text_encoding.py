"""Shared text-file decoding helpers for ingestion paths."""

from __future__ import annotations

import codecs
from pathlib import Path


_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF8, "utf-8-sig"),
)

# utf-8-sig is intentionally absent: BOM-prefixed data is handled above, and
# utf-8-sig accepts exactly the byte strings utf-8 does.
_FALLBACK_ENCODINGS = ("utf-8", "cp1252")


def _decode_with(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


# A UTF-16 file shows its NUL pattern from the start; a bounded prefix decides
# without copying a large file twice.
_UTF16_SNIFF_BYTES = 64 * 1024


def _looks_like_utf16(data: bytes) -> str | None:
    if len(data) < 4:
        return None

    data = data[:_UTF16_SNIFF_BYTES]
    even_bytes = data[0::2]
    odd_bytes = data[1::2]
    if not even_bytes or not odd_bytes:
        return None

    even_nul_ratio = even_bytes.count(0) / len(even_bytes)
    odd_nul_ratio = odd_bytes.count(0) / len(odd_bytes)
    if odd_nul_ratio > 0.25 and even_nul_ratio < 0.05:
        return "utf-16-le"
    if even_nul_ratio > 0.25 and odd_nul_ratio < 0.05:
        return "utf-16-be"
    return None


def decode_source_text(data: bytes) -> str:
    """Decode user-provided text bytes without preserving UTF-16 NUL artifacts.

    NUL characters are dropped: they carry no speech, and C-string consumers
    downstream (FFmpeg metadata, phonemizers) truncate text at them.
    """
    return _decode_source_bytes(data).replace("\x00", "")


def _decode_source_bytes(data: bytes) -> str:
    if not data:
        return ""

    for bom, encoding in _BOM_ENCODINGS:
        if data.startswith(bom):
            decoded = _decode_with(data, encoding)
            if decoded is not None:
                return decoded

    utf16_encoding = _looks_like_utf16(data)
    if utf16_encoding:
        decoded = _decode_with(data, utf16_encoding)
        if decoded is not None:
            return decoded

    for encoding in _FALLBACK_ENCODINGS:
        decoded = _decode_with(data, encoding)
        if decoded is not None:
            return decoded

    # latin-1 maps every byte, so it always decodes.
    return data.decode("latin-1")


def read_source_text(path: Path | str) -> str:
    """Read a user text/markdown source using shared source encoding detection."""
    return decode_source_text(Path(path).read_bytes())

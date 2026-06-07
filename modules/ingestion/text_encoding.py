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

_FALLBACK_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def _decode_with(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def _looks_like_utf16(data: bytes) -> str | None:
    if len(data) < 4:
        return None

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
    """Decode user-provided text bytes without preserving UTF-16 NUL artifacts."""
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

    return data.decode("utf-8", errors="replace")


def read_source_text(path: Path | str) -> str:
    """Read a user text/markdown source using shared source encoding detection."""
    return decode_source_text(Path(path).read_bytes())

"""Typed error-boundary regressions for hostile EPUB metadata."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from modules.errors import EPUBParsingError, validate_epub
from modules.ingestion.epub_parser import EPUBParser


def _mark_zip_members_encrypted(path: Path) -> None:
    payload = bytearray(path.read_bytes())
    index = 0
    while index <= len(payload) - 4:
        signature = bytes(payload[index : index + 4])
        if signature == b"PK\x03\x04":
            flags_offset = 6
        elif signature == b"PK\x01\x02":
            flags_offset = 8
        else:
            index += 1
            continue
        flags = struct.unpack_from("<H", payload, index + flags_offset)[0]
        struct.pack_into("<H", payload, index + flags_offset, flags | 0x1)
        index += 1
    path.write_bytes(payload)


def _write_required_epub_members(path: Path, mimetype: bytes) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            "<container/>",
            compress_type=zipfile.ZIP_STORED,
        )


def test_encrypted_mimetype_stays_inside_typed_epub_boundary(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.epub"
    _write_required_epub_members(source, b"application/epub+zip")
    _mark_zip_members_encrypted(source)

    with pytest.raises(EPUBParsingError, match="Unable to read EPUB mimetype"):
        validate_epub(source)
    with pytest.raises(EPUBParsingError, match="Unable to read EPUB mimetype"):
        EPUBParser(source)


def test_invalid_mimetype_encoding_stays_inside_typed_epub_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid-encoding.epub"
    _write_required_epub_members(source, b"\xff\xfe")

    with pytest.raises(EPUBParsingError, match="Unable to read EPUB mimetype"):
        validate_epub(source)
    with pytest.raises(EPUBParsingError, match="Unable to read EPUB mimetype"):
        EPUBParser(source)

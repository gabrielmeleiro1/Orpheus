from __future__ import annotations

from pathlib import Path
import struct
import zipfile

import pytest

import modules.errors as error_module
from modules.errors import EPUBParsingError, validate_epub
from modules.ingestion.epub_parser import EPUBParser
from modules.ingestion.preflight import preflight_source


def _write_epub_archive(path: Path, members: dict[str, bytes], *, compression=zipfile.ZIP_STORED) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", "<container/>", compress_type=zipfile.ZIP_STORED)
        for name, content in members.items():
            archive.writestr(name, content, compress_type=compression)


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


def test_validate_epub_rejects_large_deflated_member_before_reading_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "zip-bomb.epub"
    _write_epub_archive(source, {"chapter.xhtml": b"A" * 20_000_000}, compression=zipfile.ZIP_DEFLATED)

    def fail_if_read(*_args, **_kwargs):
        raise AssertionError("archive member was decompressed before metadata validation")

    monkeypatch.setattr(zipfile.ZipFile, "read", fail_if_read)

    with pytest.raises(EPUBParsingError, match="uncompressed size|compression ratio"):
        validate_epub(source)


@pytest.mark.parametrize(
    ("limit_name", "member_count", "member_size"),
    [
        ("MAX_EPUB_MEMBER_COUNT", 4, 1),
        ("MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES", 1, 9),
        ("MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES", 1, 9),
        ("MAX_EPUB_COMPRESSION_RATIO", 1, 1024),
    ],
)
def test_validate_epub_enforces_archive_metadata_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    member_count: int,
    member_size: int,
) -> None:
    source = tmp_path / f"{limit_name}.epub"
    limit = 3 if limit_name != "MAX_EPUB_COMPRESSION_RATIO" else 100
    monkeypatch.setattr(error_module, limit_name, limit)
    payload = b"A" * member_size
    compression = zipfile.ZIP_DEFLATED if limit_name == "MAX_EPUB_COMPRESSION_RATIO" else zipfile.ZIP_STORED
    members = {f"chapter-{index}.xhtml": payload for index in range(member_count)}
    if limit_name == "MAX_EPUB_COMPRESSION_RATIO":
        members = {"chapter.xhtml": b"A" * (2 * 1024 * 1024)}
    _write_epub_archive(source, members, compression=compression)

    with pytest.raises(EPUBParsingError):
        validate_epub(source)


def test_epub_parser_and_preflight_share_archive_limit_validation(tmp_path: Path) -> None:
    source = tmp_path / "zip-bomb.epub"
    _write_epub_archive(source, {"chapter.xhtml": b"A" * 20_000_000}, compression=zipfile.ZIP_DEFLATED)

    with pytest.raises(EPUBParsingError):
        EPUBParser(source)

    bypass_attempt = EPUBParser(source, validate=False)
    with pytest.raises(EPUBParsingError):
        bypass_attempt.parse()

    payload = preflight_source(source)
    assert payload["chapter_count"] == 0
    assert any("archive" in warning.lower() for warning in payload["warnings"])


def test_validate_epub_normalizes_encrypted_mimetype_errors(tmp_path: Path) -> None:
    source = tmp_path / "encrypted-mimetype.epub"
    _write_epub_archive(source, {})
    _mark_zip_members_encrypted(source)

    with pytest.raises(EPUBParsingError):
        validate_epub(source)
    with pytest.raises(EPUBParsingError):
        EPUBParser(source)


def test_validate_epub_normalizes_invalid_mimetype_encoding(tmp_path: Path) -> None:
    source = tmp_path / "invalid-mimetype.epub"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", b"\xff\xfe", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", "<container/>", compress_type=zipfile.ZIP_STORED)

    with pytest.raises(EPUBParsingError):
        validate_epub(source)
    with pytest.raises(EPUBParsingError):
        EPUBParser(source)

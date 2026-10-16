"""Unconvertible sources are explained in plain words; library wording stays in details."""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

import pytest

from modules.errors import AudiobookError
from modules.ingestion.markdown_parser import MarkdownParser
from modules.ingestion.pdf_parser import PDFParser
from modules.ingestion.preflight import preflight_source
from modules.ingestion.source_problems import DAMAGED_PDF, EMPTY_FILE, PASSWORD_PROTECTED_PDF
from modules.ingestion.text_parser import TextParser

pymupdf = pytest.importorskip("pymupdf")


def _password_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Secret chapter text.")
    path.write_bytes(doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner"))
    doc.close()
    return path


def _drm_epub(path: Path) -> Path:
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    encryption = (
        '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
        '<enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData>'
        "</enc:EncryptedData></encryption>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Locked</dc:title>'
        '<dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language></metadata>'
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("META-INF/encryption.xml", encryption)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", random.Random(2).randbytes(4_000))
    return path


@pytest.mark.parametrize(
    ("name", "content", "code", "reason", "has_details"),
    [
        ("empty.pdf", b"", "source_empty", EMPTY_FILE, False),
        ("empty.txt", b"", "source_empty", EMPTY_FILE, False),
        ("empty.md", b"", "source_empty", EMPTY_FILE, False),
        (
            "corrupt.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 200 + b"\n%%EOF\n",
            "pdf_damaged", DAMAGED_PDF, True,
        ),
        ("password.pdf", "password", "pdf_password_protected", PASSWORD_PROTECTED_PDF, True),
        (
            "broken.epub", b"PK\x03\x04" + b"\x00" * 64,
            "epub_damaged", "This EPUB is damaged or is not an EPUB file", False,
        ),
        ("locked.epub", "drm", "epub_drm_protected", "This EPUB is DRM-protected", True),
    ],
)
def test_preflight_names_the_blocking_problem_in_the_api_error_shape(
    tmp_path, name, content, code, reason, has_details,
):
    path = tmp_path / name
    if content == "password":
        _password_pdf(path)
    elif content == "drm":
        _drm_epub(path)
    else:
        path.write_bytes(content)

    payload = preflight_source(path)

    error = payload["blocking_error"]
    assert payload["chapter_count"] == 0
    # The same {code, message, ...details} shape an API error detail has.
    assert set(error) == ({"code", "message", "details"} if has_details else {"code", "message"})
    assert error["code"] == code
    assert error["message"].startswith(reason)
    assert payload["warnings"] == [error["message"]]
    assert not any("malformed or unreadable" in warning for warning in payload["warnings"])


def test_readable_sources_have_no_blocking_error(tmp_path):
    path = tmp_path / "ok.txt"
    path.write_text("The ferry crossed the grey water before the storm arrived.", encoding="utf-8")

    assert preflight_source(path)["blocking_error"] is None


@pytest.mark.parametrize(
    ("parser", "name", "content", "message"),
    [
        (PDFParser, "password.pdf", None, PASSWORD_PROTECTED_PDF),
        (PDFParser, "empty.pdf", b"", EMPTY_FILE),
        (PDFParser, "corrupt.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 200 + b"\n%%EOF\n", DAMAGED_PDF),
        (TextParser, "empty.txt", b"", EMPTY_FILE),
        (MarkdownParser, "empty.md", b"", EMPTY_FILE),
    ],
)
def test_conversion_time_errors_use_the_same_plain_message(tmp_path, parser, name, content, message):
    path = tmp_path / name
    if content is None:
        _password_pdf(path)
    else:
        path.write_bytes(content)

    with pytest.raises(AudiobookError) as raised:
        parser(path).parse()

    assert raised.value.message == message

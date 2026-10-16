"""DRM-encrypted EPUBs fail with a clear error instead of narrating ciphertext."""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

import pytest

from modules.errors import EPUBParsingError
from modules.ingestion.epub_parser import EPUBParser
from modules.ingestion.preflight import preflight_source

_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)
_OPF = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Locked</dc:title>'
    '<dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language></metadata>'
    '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="f1" href="font.otf" media-type="font/otf"/></manifest>'
    '<spine><itemref idref="c1"/></spine></package>'
)
_CHAPTER = (
    '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>c</title></head><body><h1>Chapter One</h1>"
    + "<p>The lamps were lit along the quay as the ferry came in.</p>" * 20
    + "</body></html>"
)


def _encryption_xml(entries: list[tuple[str, str]]) -> str:
    blocks = "".join(
        f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{algorithm}"/>'
        f'<enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>'
        "</enc:EncryptedData>"
        for algorithm, uri in entries
    )
    return (
        '<?xml version="1.0"?><encryption '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        f'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">{blocks}</encryption>'
    )


def _write_epub(path: Path, *, chapter: bytes | str, encryption: str | None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _CONTAINER)
        if encryption is not None:
            archive.writestr("META-INF/encryption.xml", encryption)
        archive.writestr("OEBPS/content.opf", _OPF)
        archive.writestr("OEBPS/ch1.xhtml", chapter)
        archive.writestr("OEBPS/font.otf", random.Random(1).randbytes(512))
    return path


def test_drm_encrypted_chapter_is_refused_with_a_clear_message(tmp_path):
    path = _write_epub(
        tmp_path / "locked.epub",
        chapter=random.Random(2).randbytes(4_000),
        encryption=_encryption_xml([
            ("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "OEBPS/ch1.xhtml"),
        ]),
    )

    with pytest.raises(EPUBParsingError, match="DRM-protected") as raised:
        EPUBParser(path).parse()
    assert "OEBPS/ch1.xhtml" in (raised.value.details or "")

    warnings = preflight_source(path)["warnings"]
    assert any("DRM-protected" in warning for warning in warnings)


def test_font_obfuscation_alone_is_not_treated_as_drm(tmp_path):
    path = _write_epub(
        tmp_path / "fonts.epub",
        chapter=_CHAPTER,
        encryption=_encryption_xml([
            ("http://www.idpf.org/2008/embedding", "OEBPS/font.otf"),
            ("http://ns.adobe.com/pdf/enc#RC", "OEBPS/font.otf"),
        ]),
    )

    document = EPUBParser(path).parse()

    assert "ferry came in" in document.chapters[0].content

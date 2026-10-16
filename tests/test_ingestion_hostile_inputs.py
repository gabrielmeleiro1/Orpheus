"""Hostile source files fail fast or degrade without leaking or garbling text."""

from __future__ import annotations

import zipfile
from pathlib import Path

from modules.errors import EPUBParsingError
from modules.ingestion.epub_parser import EPUBParser
from modules.ingestion.text_encoding import decode_source_text
from modules.ingestion.text_parser import TextParser

_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)


def _opf(title: str = "Book", doctype: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>{doctype}'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:identifier id="id">x</dc:identifier>'
        '<dc:language>en</dc:language></metadata>'
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )


def _chapter(body: str, doctype: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>{doctype}'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head>'
        f"<body><h1>Chapter One</h1>{body}"
        + "<p>The ferry crossed the grey water before the storm arrived.</p>" * 10
        + "</body></html>"
    )


def _epub(path: Path, *, opf: str, chapter: str) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", chapter)
    return path


def test_nul_bytes_are_dropped_from_decoded_text(tmp_path):
    assert decode_source_text(b"Hello\x00 world") == "Hello world"
    assert decode_source_text("café\x00".encode("utf-16")) == "café"
    source = tmp_path / "nul.txt"
    source.write_bytes(b"The quiet harbour.\x00 " * 50)

    document = TextParser(source).parse()

    assert "\x00" not in document.chapters[0].content
    assert "The quiet harbour." in document.chapters[0].content


def test_external_entities_are_never_expanded_into_book_text(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("CANARY-SECRET", encoding="utf-8")
    entity = f'<!DOCTYPE html [<!ENTITY leak SYSTEM "{secret.resolve().as_uri()}">]>'
    path = _epub(
        tmp_path / "xxe.epub",
        opf=_opf(title="&leak;", doctype=entity.replace("html", "package")),
        chapter=_chapter("<p>&leak; harbour</p>", doctype=entity),
    )

    try:
        document = EPUBParser(path).parse()
    except EPUBParsingError:
        return
    texts = [document.title or ""] + [chapter.content + chapter.title for chapter in document.chapters]
    assert not any("CANARY-SECRET" in text for text in texts)

"""Malformed EPUB packages fail with messages that name the problem."""

from __future__ import annotations

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
_CHAPTER = (
    '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>c</title></head><body><h1>Chapter One</h1>"
    + "<p>The ferry crossed the grey water before the storm arrived.</p>" * 10
    + "</body></html>"
)
_MANIFEST = '<manifest><item id="c1" href="{href}" media-type="application/xhtml+xml"/></manifest>'
_SPINE = '<spine><itemref idref="{idref}"/></spine>'


def _opf(manifest: str = "", spine: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Broken</dc:title>'
        '<dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language></metadata>'
        f"{manifest}{spine}</package>"
    )


def _deep_ncx(depth: int) -> str:
    points = "".join(f'<navPoint id="n{i}" playOrder="{i}">' for i in range(depth))
    return (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<head/><docTitle><text>T</text></docTitle><navMap>{points}"
        + "</navPoint>" * depth
        + "</navMap></ncx>"
    )


def _epub(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", members.pop("container", _CONTAINER))
        for name, data in members.items():
            archive.writestr(name, data)
    return path


_CASES = {
    "missing spine": (
        {"OEBPS/content.opf": _opf(_MANIFEST.format(href="ch1.xhtml")), "OEBPS/ch1.xhtml": _CHAPTER},
        "has no spine",
    ),
    "missing manifest": (
        {"OEBPS/content.opf": _opf(spine=_SPINE.format(idref="c1")), "OEBPS/ch1.xhtml": _CHAPTER},
        "has no manifest",
    ),
    "missing package file": (
        {"OEBPS/ch1.xhtml": _CHAPTER},
        "package file OEBPS/content.opf is missing",
    ),
    "container without rootfile": (
        {"container": _CONTAINER.replace("<rootfile ", "<other "), "OEBPS/ch1.xhtml": _CHAPTER},
        "does not name its package file",
    ),
    "package not XML": (
        {"OEBPS/content.opf": "<package><manifest>", "OEBPS/ch1.xhtml": _CHAPTER},
        "not well-formed XML",
    ),
    "chapter missing from archive": (
        {"OEBPS/content.opf": _opf(_MANIFEST.format(href="ch1.xhtml"), _SPINE.format(idref="c1"))},
        "missing from the archive: OEBPS/ch1.xhtml",
    ),
    "path traversal": (
        {
            "OEBPS/content.opf": _opf(_MANIFEST.format(href="../../evil.xhtml"), _SPINE.format(idref="c1")),
            "../../evil.xhtml": _CHAPTER,
        },
        "outside its own archive",
    ),
    "drive-letter name": (
        {"OEBPS/content.opf": _opf(_MANIFEST.format(href="C:/evil.xhtml"), _SPINE.format(idref="c1"))},
        "outside its own archive",
    ),
    "spine refers only to unknown items": (
        {
            "OEBPS/content.opf": _opf(_MANIFEST.format(href="ch1.xhtml"), _SPINE.format(idref="ghost")),
            "OEBPS/ch1.xhtml": _CHAPTER,
        },
        "not in its manifest: ghost",
    ),
}


@pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
def test_malformed_package_errors_name_the_problem(tmp_path, case):
    members, expected = _CASES[case]
    path = _epub(tmp_path / "broken.epub", dict(members))

    with pytest.raises(EPUBParsingError) as raised:
        EPUBParser(path).parse()

    assert expected in raised.value.message
    assert "NoneType" not in str(raised.value) and "index out of range" not in raised.value.message
    warnings = preflight_source(path)["warnings"]
    assert any(expected in warning for warning in warnings), warnings


def test_unreadable_table_of_contents_is_skipped_not_fatal(tmp_path, monkeypatch):
    from ebooklib import epub

    real_read = epub.read_epub

    def flaky_read(name, options=None):
        if not (options or {}).get("ignore_ncx"):
            raise IndexError("list index out of range")
        return real_read(name, options=options)

    monkeypatch.setattr(epub, "read_epub", flaky_read)
    opf = _opf(
        _MANIFEST.format(href="ch1.xhtml").replace(
            "</manifest>", '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>',
        ),
        '<spine toc="ncx"><itemref idref="c1"/></spine>',
    )
    path = _epub(tmp_path / "toc.epub", {
        "OEBPS/content.opf": opf, "OEBPS/ch1.xhtml": _CHAPTER, "OEBPS/toc.ncx": _deep_ncx(3),
    })

    document = EPUBParser(path).parse()

    assert "ferry crossed" in document.chapters[0].content


def test_metadata_failure_after_toc_retry_has_a_clear_message(tmp_path, monkeypatch):
    from ebooklib import epub

    def broken_read(name, options=None):
        raise TypeError("'NoneType' object is not iterable")

    monkeypatch.setattr(epub, "read_epub", broken_read)
    path = _epub(tmp_path / "meta.epub", {
        "OEBPS/content.opf": _opf(_MANIFEST.format(href="ch1.xhtml"), _SPINE.format(idref="c1")),
        "OEBPS/ch1.xhtml": _CHAPTER,
    })

    with pytest.raises(EPUBParsingError) as raised:
        EPUBParser(path).parse()

    assert "table of contents or package metadata is malformed" in raised.value.message
    assert "NoneType" not in raised.value.message
    assert "NoneType" in (raised.value.details or "")


def test_dangling_spine_reference_is_skipped_when_other_chapters_resolve(tmp_path):
    spine = '<spine><itemref idref="ghost"/><itemref idref="c1"/></spine>'
    path = _epub(tmp_path / "dangling.epub", {
        "OEBPS/content.opf": _opf(_MANIFEST.format(href="ch1.xhtml"), spine),
        "OEBPS/ch1.xhtml": _CHAPTER,
    })

    document = EPUBParser(path).parse()

    assert len(document.chapters) == 1

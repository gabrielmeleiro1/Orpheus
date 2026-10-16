"""Cover art extraction from library sources."""

import zipfile
import zlib

import pytest

from fastapi.testclient import TestClient

from modules.ingestion.cover_art import (
    PDF_THUMBNAIL_HEIGHT_PX,
    PDF_THUMBNAIL_WIDTH_PX,
    extract_cover,
)
from modules.storage.models import BookCreate, SourceType
from modules.web.server import create_app

from tests import test_conversion_upload_lifecycle as _upload_lifecycle


controller = _upload_lifecycle.controller

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"cover-pixels"
CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _write_epub(path, manifest: str, metadata: str = "", image_name: str = "images/cover.png"):
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata>{metadata}</metadata>
  <manifest>{manifest}</manifest>
</package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr(f"OEBPS/{image_name}", PNG_BYTES)
    return path


def test_epub3_cover_image_property(tmp_path):
    source = _write_epub(
        tmp_path / "book.epub",
        '<item id="img" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
    )
    cover = extract_cover(source)
    assert cover is not None
    assert cover.media_type == "image/png"
    assert cover.data == PNG_BYTES


def test_epub2_cover_meta(tmp_path):
    source = _write_epub(
        tmp_path / "book.epub",
        '<item id="front" href="images/front%20page.png" media-type="image/png"/>',
        metadata='<meta name="cover" content="front"/>',
        image_name="images/front page.png",
    )
    cover = extract_cover(source)
    assert cover is not None and cover.data == PNG_BYTES


def test_epub_without_cover_and_unsafe_href(tmp_path):
    plain = _write_epub(tmp_path / "plain.epub", '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>')
    assert extract_cover(plain) is None
    escaping = _write_epub(
        tmp_path / "escape.epub",
        '<item id="img" href="../../outside.png" media-type="image/png" properties="cover-image"/>',
    )
    assert extract_cover(escaping) is None


def test_pdf_first_page_thumbnail(tmp_path):
    import pymupdf

    source = tmp_path / "doc.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=400)
        page.insert_text((40, 60), "Title page")
        document.save(source)
    cover = extract_cover(source)
    assert cover is not None
    assert cover.media_type == "image/png"
    assert cover.data.startswith(b"\x89PNG")


@pytest.mark.parametrize("width,height", [(100, 3000), (10000, 100)])
def test_pdf_thumbnail_bounds_both_dimensions(tmp_path, width, height):
    import pymupdf

    source = tmp_path / "oversized.pdf"
    with pymupdf.open() as document:
        document.new_page(width=width, height=height)
        document.save(source)

    cover = extract_cover(source)

    assert cover is not None
    pixmap = pymupdf.Pixmap(cover.data)
    assert 0 < pixmap.width <= PDF_THUMBNAIL_WIDTH_PX
    assert 0 < pixmap.height <= PDF_THUMBNAIL_HEIGHT_PX


def test_missing_corrupt_and_plain_sources(tmp_path):
    assert extract_cover(None) is None
    assert extract_cover(tmp_path / "missing.epub") is None
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip")
    assert extract_cover(broken) is None
    text = tmp_path / "notes.txt"
    text.write_text("hello")
    assert extract_cover(text) is None


def test_corrupt_compressed_cover_falls_back_instead_of_failing_library_request(controller, tmp_path):
    source = _write_epub(
        tmp_path / "broken-cover.epub",
        '<item id="img" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
    )
    with zipfile.ZipFile(source) as archive:
        members = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("OEBPS/images/cover.png")
    contents = bytearray(source.read_bytes())
    data_offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    # DEFLATE's reserved block type gives a decompressor error rather than a
    # ZIP central-directory/CRC error. The rest of the EPUB remains readable.
    contents[data_offset] = 0x07
    source.write_bytes(contents)
    with zipfile.ZipFile(source) as archive:
        with pytest.raises(zlib.error):
            archive.read(info.filename)
    book = controller.repository.create_book(BookCreate(
        title="Damaged cover", source_path=str(source), source_type=SourceType.EPUB,
    ))

    with TestClient(create_app(controller=controller)) as client:
        response = client.get(f"/api/library/{book.id}/cover")

    assert response.status_code == 204
    assert response.content == b""


def test_cover_route(controller, tmp_path):
    source = _write_epub(
        tmp_path / "routed.epub",
        '<item id="img" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
    )
    with_cover = controller.repository.create_book(BookCreate(title="Cover", source_path=str(source), source_type=SourceType.EPUB))
    text = tmp_path / "plain.txt"
    text.write_text("text")
    without_cover = controller.repository.create_book(BookCreate(title="Plain", source_path=str(text), source_type=SourceType.TXT))
    with TestClient(create_app(controller=controller)) as client:
        response = client.get(f"/api/library/{with_cover.id}/cover")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert response.content == PNG_BYTES
        no_cover = client.get(f"/api/library/{without_cover.id}/cover")
        assert no_cover.status_code == 204
        assert no_cover.content == b""
        assert client.get("/api/library/999999/cover").status_code == 404


def test_cover_route_resolves_library_relative_source(controller):
    source = _write_epub(
        controller.config.data_dir / "sources" / "relative.epub",
        '<item id="img" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
    )
    relative = source.relative_to(controller.config.data_dir.parent)
    book = controller.repository.create_book(BookCreate(
        title="Relative source", source_path=str(relative), source_type=SourceType.EPUB,
    ))

    with TestClient(create_app(controller=controller)) as client:
        response = client.get(f"/api/library/{book.id}/cover")

    assert response.status_code == 200
    assert response.content == PNG_BYTES

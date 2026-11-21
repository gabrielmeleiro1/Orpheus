"""Cover art for library books, read from the original source file.

EPUB files usually ship a cover image in their package manifest; PDFs get a
thumbnail of their first page. Plain text and Markdown have no cover, so the
desktop UI falls back to its generated cover for those.
"""

from __future__ import annotations

import posixpath
import zipfile
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

# Covers are thumbnails; refuse anything large enough to be a decompression trap.
MAX_COVER_BYTES = 8 * 1024 * 1024
PDF_THUMBNAIL_WIDTH_PX = 480
PDF_THUMBNAIL_HEIGHT_PX = 720

_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"


@dataclass(frozen=True)
class CoverImage:
    data: bytes
    media_type: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_COVER_BYTES:
        return None
    # The header size is only a claim; bound what decompression may produce.
    with archive.open(info) as member:
        data = member.read(MAX_COVER_BYTES + 1)
    return data if len(data) <= MAX_COVER_BYTES else None


def _opf_path(archive: zipfile.ZipFile) -> str | None:
    container = _read_member(archive, "META-INF/container.xml")
    if container is None:
        return None
    root = ElementTree.fromstring(container)
    for rootfile in root.iter(f"{_CONTAINER_NS}rootfile"):
        path = rootfile.get("full-path")
        if path:
            return path
    return None


def _href_path(href: str) -> str:
    """The archive path part of a manifest href (no query or fragment)."""
    return unquote(href.split("#", 1)[0].split("?", 1)[0])


def _image_media_type(item: ElementTree.Element) -> str | None:
    """The supported image type of a manifest item, by extension or declared type."""
    extension = posixpath.splitext(_href_path(item.get("href") or ""))[1].lower()
    if extension in _IMAGE_MEDIA_TYPES:
        return _IMAGE_MEDIA_TYPES[extension]
    declared = (item.get("media-type") or "").strip().lower()
    return declared if declared in _IMAGE_MEDIA_TYPES.values() else None


def _epub_cover_item(opf: ElementTree.Element) -> ElementTree.Element | None:
    # Every source of the cover must name an image: EPUB 2 books often point
    # <meta name="cover"> at the XHTML page that wraps the cover image.
    items = [
        element for element in opf.iter()
        if _local(element.tag) == "item" and element.get("href") and _image_media_type(element)
    ]
    # EPUB 3 marks the cover in the manifest itself.
    for item in items:
        if "cover-image" in (item.get("properties") or "").split():
            return item
    # EPUB 2 points at it from <meta name="cover" content="<manifest id>">.
    cover_id = next(
        (element.get("content") for element in opf.iter()
         if _local(element.tag) == "meta" and element.get("name") == "cover"),
        None,
    )
    if cover_id:
        for item in items:
            if item.get("id") == cover_id:
                return item
    # Last resort: an image that calls itself the cover.
    for item in items:
        media_type = item.get("media-type") or ""
        label = f"{item.get('id') or ''} {item.get('href') or ''}".lower()
        if media_type.startswith("image/") and "cover" in label:
            return item
    return None


def _epub_cover(path: Path) -> CoverImage | None:
    with zipfile.ZipFile(path) as archive:
        opf_path = _opf_path(archive)
        if not opf_path:
            return None
        opf_bytes = _read_member(archive, opf_path)
        if opf_bytes is None:
            return None
        item = _epub_cover_item(ElementTree.fromstring(opf_bytes))
        if item is None:
            return None
        href = _href_path(item.get("href") or "")
        # A leading slash means the container root, not the filesystem.
        base = "" if href.startswith("/") else posixpath.dirname(opf_path)
        member = posixpath.normpath(posixpath.join(base, href.lstrip("/")))
        media_type = _image_media_type(item)
        if media_type is None or member == ".." or member.startswith("../"):
            return None
        data = _read_member(archive, member)
        return CoverImage(data, media_type) if data else None


def _pdf_cover(path: Path) -> CoverImage | None:
    import pymupdf

    with pymupdf.open(path) as document:
        if document.page_count < 1:
            return None
        page = document[0]
        width = page.rect.width or PDF_THUMBNAIL_WIDTH_PX
        height = page.rect.height or PDF_THUMBNAIL_HEIGHT_PX
        # Long scroll pages and oversized media boxes must remain thumbnails.
        # A lower zoom bound would allow arbitrarily large raster allocations.
        zoom = min(4.0, PDF_THUMBNAIL_WIDTH_PX / width, PDF_THUMBNAIL_HEIGHT_PX / height)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return CoverImage(pixmap.tobytes("png"), "image/png")


@lru_cache(maxsize=64)
def _cached_cover(path: str, _mtime_ns: int, _size: int) -> CoverImage | None:
    source = Path(path)
    suffix = source.suffix.lower()
    try:
        if suffix == ".epub":
            return _epub_cover(source)
        if suffix == ".pdf":
            return _pdf_cover(source)
    except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error, ElementTree.ParseError):
        return None
    return None


def extract_cover(source_path: str | Path | None) -> CoverImage | None:
    """Return the source's cover image, or None when it has none or is unreadable."""
    if not source_path:
        return None
    source = Path(source_path)
    try:
        stat = source.stat()
    except OSError:
        return None
    if not source.is_file():
        return None
    # Keyed by mtime and size so a replaced source never serves a stale cover.
    return _cached_cover(str(source), stat.st_mtime_ns, stat.st_size)

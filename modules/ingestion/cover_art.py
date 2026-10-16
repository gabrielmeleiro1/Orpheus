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
    return archive.read(info)


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


def _epub_cover_href(opf: ElementTree.Element) -> str | None:
    items = [element for element in opf.iter() if _local(element.tag) == "item"]
    # EPUB 3 marks the cover in the manifest itself.
    for item in items:
        if "cover-image" in (item.get("properties") or "").split():
            return item.get("href")
    # EPUB 2 points at it from <meta name="cover" content="<manifest id>">.
    cover_id = next(
        (element.get("content") for element in opf.iter()
         if _local(element.tag) == "meta" and element.get("name") == "cover"),
        None,
    )
    if cover_id:
        for item in items:
            if item.get("id") == cover_id:
                return item.get("href")
    # Last resort: an image that calls itself the cover.
    for item in items:
        media_type = item.get("media-type") or ""
        label = f"{item.get('id') or ''} {item.get('href') or ''}".lower()
        if media_type.startswith("image/") and "cover" in label:
            return item.get("href")
    return None


def _epub_cover(path: Path) -> CoverImage | None:
    with zipfile.ZipFile(path) as archive:
        opf_path = _opf_path(archive)
        if not opf_path:
            return None
        opf_bytes = _read_member(archive, opf_path)
        if opf_bytes is None:
            return None
        href = _epub_cover_href(ElementTree.fromstring(opf_bytes))
        if not href:
            return None
        member = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(href)))
        media_type = _IMAGE_MEDIA_TYPES.get(posixpath.splitext(member)[1].lower())
        if media_type is None or member.startswith("../"):
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

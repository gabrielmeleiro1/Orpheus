"""Structural EPUB checks that turn malformed packages into clear errors.

EbookLib assumes a well-formed package and fails on a broken one with raw
Python exceptions ("'NoneType' object is not iterable", "list index out of
range"). These checks name the actual problem before it gets that far.
Callers run ``modules.errors.validate_epub`` first, so archive size limits and
the mimetype/container entries have already been verified.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from modules.errors import EPUBParsingError

logger = logging.getLogger(__name__)

_CONTAINER_PATH = "META-INF/container.xml"
_MAX_LISTED = 3


def _safe_parser() -> etree.XMLParser:
    # No DTD loading, entity expansion, or network access for untrusted metadata.
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:_MAX_LISTED])
    return shown + (f" and {len(names) - _MAX_LISTED} more" if len(names) > _MAX_LISTED else "")


def _fail(message: str, file_path: Path, details: str | None = None) -> EPUBParsingError:
    return EPUBParsingError(message=message, details=details, file_path=file_path)


def _parse_xml(archive: zipfile.ZipFile, member: str, file_path: Path, label: str):
    try:
        return etree.fromstring(archive.read(member), parser=_safe_parser())
    except etree.XMLSyntaxError as exc:
        raise _fail(f"The EPUB {label} ({member}) is not well-formed XML.", file_path, str(exc)) from exc


def _package_member(href: str, package_dir: str) -> str | None:
    """Resolve a manifest href inside the archive, or None if it escapes it."""
    path = unquote(href.split("#", 1)[0])
    if not path or path.startswith(("/", "\\")) or ":" in path.split("/", 1)[0]:
        return None
    resolved = posixpath.normpath(posixpath.join(package_dir, path))
    if resolved == ".." or resolved.startswith("../"):
        return None
    return resolved


def check_epub_package(file_path: Path | str) -> None:
    """Raise ``EPUBParsingError`` naming the first structural defect, if any."""
    file_path = Path(file_path)
    with zipfile.ZipFile(file_path) as archive:
        members = set(archive.namelist())
        container = _parse_xml(archive, _CONTAINER_PATH, file_path, "container file")
        rootfile = next(iter(container.iter("{*}rootfile")), None)
        package_path = (rootfile.get("full-path") if rootfile is not None else "") or ""
        if not package_path:
            raise _fail(
                "The EPUB container does not name its package file (content.opf).", file_path,
            )
        if package_path not in members:
            raise _fail(
                f"The EPUB package file {package_path} is missing from the archive.", file_path,
            )
        package = _parse_xml(archive, package_path, file_path, "package file")

    package_dir = posixpath.dirname(package_path)
    manifest = package.find("{*}manifest")
    items = list(manifest.iter("{*}item")) if manifest is not None else []
    if not items:
        raise _fail(
            "The EPUB has no manifest (the list of its files), so its chapters cannot be found.",
            file_path,
        )
    spine = package.find("{*}spine")
    itemrefs = list(spine.iter("{*}itemref")) if spine is not None else []
    if not itemrefs:
        raise _fail(
            "The EPUB has no spine (its reading order), so its chapters cannot be ordered.",
            file_path,
        )

    hrefs_by_id: dict[str, str] = {}
    unsafe: list[str] = []
    missing: list[str] = []
    for item in items:
        href = item.get("href") or ""
        member = _package_member(href, package_dir)
        if member is None:
            unsafe.append(href or "(empty)")
        elif member not in members:
            missing.append(member)
        hrefs_by_id[item.get("id") or ""] = href
    if unsafe:
        raise _fail(
            "The EPUB lists files outside its own archive, which is not allowed: "
            f"{_listed(unsafe)}.",
            file_path,
        )
    if missing:
        raise _fail(
            f"The EPUB lists files that are missing from the archive: {_listed(missing)}.",
            file_path,
        )
    unknown = [ref.get("idref") or "(empty)" for ref in itemrefs if ref.get("idref") not in hrefs_by_id]
    if unknown and len(unknown) < len(itemrefs):
        # Readers skip a reference to an item the manifest does not list.
        logger.warning(
            "EPUB reading order refers to items missing from its manifest (%s); skipping them",
            _listed(unknown),
        )
    elif unknown:
        raise _fail(
            "The EPUB reading order refers to chapters that are not in its manifest: "
            f"{_listed(unknown)}.",
            file_path,
        )


def read_epub_book(file_path: Path | str):
    """Check the package structure, then load it with EbookLib.

    A broken table of contents (NCX) only costs chapter titles, so the book is
    read again without it rather than refused.
    """
    from ebooklib import epub

    file_path = Path(file_path)
    check_epub_package(file_path)
    try:
        return epub.read_epub(str(file_path))
    except Exception as exc:
        first_error = exc
    try:
        book = epub.read_epub(str(file_path), options={"ignore_ncx": True})
    except Exception as exc:
        raise _fail(
            "The EPUB could not be read: its table of contents or package metadata is malformed.",
            file_path,
            f"{type(exc).__name__}: {exc}",
        ) from exc
    logger.warning(
        "EPUB table of contents is unreadable (%s: %s); continuing without it",
        type(first_error).__name__,
        first_error,
    )
    return book

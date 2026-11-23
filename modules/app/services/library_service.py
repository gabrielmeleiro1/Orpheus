"""Library CRUD/query service for AppController."""

from __future__ import annotations


import os
import errno
import json
import logging
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from modules.contracts.limits import MAX_UPLOAD_BYTES
from modules.contracts.source_extensions import SUPPORTED_SOURCE_SUFFIXES
from modules.contracts.upload_errors import UploadTooLargeError
from modules.errors import UnsupportedSourceTypeError, sanitize_filename
from modules.ingestion.cover_art import CoverImage, extract_cover
from modules.ingestion.filenames import source_filename
from modules.ingestion.preflight import preflight_source as inspect_source
from modules.pipeline.book_workspace import sanitize_book_title
from modules.storage.deletion import (
    DELETE_MANIFEST,
    DELETE_PRESERVE_MARKER,
    LOCAL_DELETE_STAGING,
    UNOWNED_FOLDER_BOOK_ID,
    apply_delete_staging_decision,
    delete_staging_policy,
    finish_delete_transaction,
    mark_delete_transaction_committed,
    preserve_delete_transaction,
    read_delete_transaction,
    remove_staged_artifacts,
    remove_unmanifested_staging,
    restore_staged_artifacts,
    sync_directory,
    write_delete_transaction,
)
from modules.storage.maintenance import (
    _find_ondisk_playback_artifact,
    is_app_owned_durable_source,
    library_quarantined,
    resolve_app_owned_sources_root,
    resolve_stored_path,
)
from modules.storage.models import BookFilters, BookSummary, ConversionMode, PlaybackStatus
from modules.storage.repository import IBookRepository
from modules.storage.permissions import ensure_owner_only_directory, owner_only

MANAGED_UPLOAD_ROOT_NAME = "web_uploads"
DELETE_STAGING_DIR_NAME = "delete_staging"
UPLOAD_HANDLE_PREFIX = "upload:"
SUPPORTED_UPLOAD_SUFFIXES = SUPPORTED_SOURCE_SUFFIXES
UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024
DEFAULT_PASTE_TITLE = "Pasted Document"
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S+", re.MULTILINE)

logger = logging.getLogger(__name__)

# Delete server-owned uploads left untouched for longer than this (seconds).
WEB_UPLOAD_TTL_SECONDS = 7 * 24 * 60 * 60
# Minimum spacing between the opportunistic sweeps new uploads trigger.
UPLOAD_SWEEP_INTERVAL_SECONDS = 10 * 60

# Re-exported for backward compatibility; canonical definitions live in
# ``modules.contracts``.
__all__ = ["MAX_UPLOAD_BYTES", "UploadTooLargeError", "LibraryService"]

# Path.resolve() opens every component on Windows. A deletion's containment
# analysis compares hundreds of chapter paths against the same few roots while
# it holds the repository lock, so it resolves each path once per analysis.
_resolution_memo = threading.local()


@contextmanager
def _memoized_path_resolution(seed: dict[Path, Path] | None = None):
    if getattr(_resolution_memo, "paths", None) is not None:
        yield
        return
    _resolution_memo.paths = seed if seed is not None else {}
    try:
        yield
    finally:
        _resolution_memo.paths = None


def _resolve(path: Path) -> Path:
    paths = getattr(_resolution_memo, "paths", None)
    if paths is None:
        return path.resolve()
    resolved = paths.get(path)
    if resolved is None:
        resolved = paths[path] = path.resolve()
        # Resolution is idempotent; callers often re-check resolved paths.
        paths.setdefault(resolved, resolved)
    return resolved


def _default_folder_opener(folder: Path) -> None:
    # The shell "open" verb launches files with their associated program (and
    # runs executables), so it must only ever receive an existing directory.
    if not os.path.isdir(folder):
        raise RuntimeError(f"failed to open folder: {folder}")
    if os.name == "nt":
        # ``explorer.exe`` exits non-zero even when it opens the folder, so a
        # checked subprocess call would report failure on every success.
        # Canonical filesystem paths can use a verbatim prefix that the shell
        # cannot interpret. Change only the shell argument, not stored paths.
        shell_path = str(folder)
        if shell_path.startswith("\\\\?\\UNC\\"):
            shell_path = "\\\\" + shell_path[8:]
        elif shell_path.startswith("\\\\?\\") and shell_path[5:7] == ":\\":
            shell_path = shell_path[4:]
        try:
            os.startfile(shell_path)  # noqa: S606 - opening a local folder
        except OSError as exc:
            raise RuntimeError(f"failed to open folder: {folder}") from exc
        return

    if sys.platform == "darwin":
        command = ["open", str(folder)]
    else:
        command = ["xdg-open", str(folder)]

    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"failed to open folder: {folder}") from exc


class LibraryService:
    """Encapsulates book/chapter repository operations."""

    def __init__(
        self,
        repository: IBookRepository,
        output_dir: Path | str = "output",
        temp_dir: Path | str = "temp",
        storage_root: Path | str | None = None,
        folder_opener=None,
    ):
        self._repository = repository
        self._output_dir = Path(output_dir).expanduser().resolve()
        self._temp_dir = Path(temp_dir).expanduser().resolve()
        self._storage_root = (
            Path(storage_root).expanduser().resolve()
            if storage_root is not None
            else self._output_dir.resolve().parent
        )
        self._last_upload_sweep: float | None = None
        # One ownership walk covers construction and the startup sweep below;
        # the repository's opening claim, made just before, is that walk.
        self._verify_storage_ownership(after_fresh_claim=True)
        ensure_owner_only_directory(self._output_dir)
        ensure_owner_only_directory(self._temp_dir)
        self._folder_opener = folder_opener or _default_folder_opener
        self._sweep_delete_staging()

    def sweep_delete_staging(self) -> int:
        # After a restore this is the check that reports a failed claim, so
        # it walks the roots unless that restore's claim just succeeded.
        self._verify_storage_ownership(after_fresh_claim=True)
        return self._sweep_delete_staging()

    def _sweep_delete_staging(self) -> int:
        """Settle leftover deletion transactions; ownership is already verified."""
        from modules.storage.maintenance import (
            AUDIO_ARTIFACT_SUFFIXES, DEFAULT_CLEANUP_GRACE_SECONDS, _recency_filter,
        )

        root = self._delete_staging_root()
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            return 0
        removed = 0
        try:
            for entry in list(root.iterdir()):
                if not (entry / DELETE_MANIFEST).exists():
                    continue
                references_checked = False
                try:
                    book_id, artifacts = read_delete_transaction(
                        entry, allowed_roots=(self._output_dir, self._temp_dir, self._app_owned_sources_root()),
                    )
                    with self.library_snapshot():
                        detail = self._repository.get_book_with_chapters(book_id)
                        paths = self._other_book_artifact_paths(book_id)
                        book_paths = set()
                        if detail is not None:
                            book, chapters = detail
                            book_paths.update(
                                path for raw in [book.source_path, book.playback_path, *(chapter.mp3_path for chapter in chapters)]
                                if (path := self._resolve_stored_path(raw)) is not None
                            )
                            paths.update(book_paths)
                        decision, referenced, protected = delete_staging_policy(
                            entry, artifacts, paths, book_exists=detail is not None,
                            book_has_paths=bool(book_paths),
                            database_replaced=getattr(self._repository, "database_replaced", False),
                        )
                        references_checked = True
                        removed += int(apply_delete_staging_decision(
                            entry, artifacts, decision, referenced, protected,
                            make_durable=self._make_delete_durable,
                            restore=self._restore_staged_artifacts, remove=self._commit_staged_artifacts,
                        ))
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, sqlite3.Error):
                    logger.warning("Failed to reconcile deletion transaction: %s", entry, exc_info=True)
                    try:
                        preserve = entry / DELETE_PRESERVE_MARKER
                        if (
                            not references_checked and entry.is_dir() and not entry.is_symlink() and not entry.is_junction()
                            and not preserve.is_symlink() and not preserve.is_junction()
                        ):
                            preserve_delete_transaction(entry)
                    except OSError:
                        logger.warning("Failed to preserve deletion transaction: %s", entry, exc_info=True)
            cutoff = time.time() - DEFAULT_CLEANUP_GRACE_SECONDS
            for entry in root.iterdir():
                try:
                    if entry.is_symlink() or entry.is_junction() or not entry.is_dir():
                        continue
                    if (entry / DELETE_MANIFEST).exists() or not _recency_filter([entry], cutoff):
                        continue
                    with self.library_snapshot():
                        removed += int(remove_unmanifested_staging(
                            entry, self._other_book_artifact_paths(0), audio_suffixes=AUDIO_ARTIFACT_SUFFIXES,
                        ))
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, sqlite3.Error):
                    logger.debug("Failed to sweep deletion staging: %s", entry, exc_info=True)
        except OSError:
            logger.debug("Failed to list deletion staging", exc_info=True)
        return removed

    def _verify_storage_ownership(
        self, *, after_fresh_claim: bool = False, removing: tuple[Path, ...] | None = None,
    ) -> None:
        """Refuse to act on roots that another library's records reach.

        ``after_fresh_claim`` accepts the repository's claim of the same roots
        when nothing ran since it (see ``take_fresh_storage_claim``).
        ``removing`` limits the walk to the trees a removal traverses.
        """
        from modules.storage.managed_roots import claim_managed_roots, verify_removal_scope

        database = getattr(self._repository, "db_path", None)
        if database is None:
            return
        roots = (self._output_dir, self._temp_dir, self._app_owned_sources_root())
        if after_fresh_claim:
            take = getattr(self._repository, "take_fresh_storage_claim", None)
            claimed = take() if callable(take) else ()
            if claimed and {root.resolve() for root in roots} <= set(claimed):
                return
        if removing is not None:
            verify_removal_scope(database, roots, removing)
        else:
            claim_managed_roots(database, *roots)

    def _workspace_root(self) -> Path:
        return self._storage_root.resolve()

    def _resolve_stored_path(self, raw_path: Optional[str]) -> Optional[Path]:
        if not raw_path:
            return None
        # Absolute stored paths do not depend on the workspace. Preserve their
        # lexical form here; callers retain their existing canonicalization and
        # ownership checks before accessing or deleting artifacts.
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        return resolve_stored_path(
            raw_path,
            self._workspace_root(),
            output_root=self._output_dir,
            temp_root=self._temp_dir,
        )

    def _upload_root(self) -> Path:
        return self._temp_dir / MANAGED_UPLOAD_ROOT_NAME

    def _sweep_stale_uploads_when_due(self) -> None:
        # Uploads call this on every request; the week-long TTL does not need
        # a directory walk per upload.
        now = time.monotonic()
        last = getattr(self, "_last_upload_sweep", None)
        if last is not None and now - last < UPLOAD_SWEEP_INTERVAL_SECONDS:
            return
        self._last_upload_sweep = now
        self.sweep_stale_uploads()

    def sweep_stale_uploads(self, ttl_seconds: float = WEB_UPLOAD_TTL_SECONDS) -> int:
        try:
            with getattr(self, "upload_cleanup_guard", nullcontext)():
                return self._sweep_stale_uploads(ttl_seconds)
        except RuntimeError:
            # A reserved/running conversion may still be copying its upload.
            return 0

    def delete_upload(self, handle: str) -> dict:
        """Remove an opaque upload handle unless a library book still uses it."""
        if not handle.startswith(UPLOAD_HANDLE_PREFIX):
            raise ValueError("uploaded source handle is invalid")
        name = handle.removeprefix(UPLOAD_HANDLE_PREFIX)
        if not name or Path(name).name != name or name in {".", ".."} or any(char in name for char in "/\\:"):
            raise ValueError("uploaded source handle is invalid")
        path = self._upload_root() / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("uploaded source handle is invalid")
        guard = getattr(self, "upload_cleanup_guard", None)
        with guard(path) if guard else nullcontext():
            if self._is_source_referenced_by_books(path):
                raise RuntimeError("A library book still references this upload")
            path.unlink(missing_ok=True)
        return {"deleted": True}

    def _sweep_stale_uploads(self, ttl_seconds: float) -> int:
        """Opportunistically delete web uploads older than ``ttl_seconds``.

        Only files directly under the server-owned ``web_uploads`` root are
        considered. Returns the number of files removed. Never raises; sweep
        failures are best-effort and logged.
        """
        upload_root = self._upload_root()
        if not upload_root.exists():
            return 0
        database = getattr(self._repository, "db_path", None)
        if database is not None and library_quarantined(database):
            return 0

        cutoff = time.time() - max(0.0, float(ttl_seconds))
        removed = 0
        try:
            entries = list(upload_root.iterdir())
        except OSError:
            logger.debug("Failed to list web uploads for sweep", exc_info=True)
            return 0

        for entry in entries:
            try:
                # A symlink is not an uploaded file. Calling is_file/stat on it
                # would follow the link (and owner_only could chmod its target),
                # so leave links for the maintenance pass, which unlinks them
                # without dereferencing.
                if entry.is_symlink() or not entry.is_file():
                    continue
                owner_only(entry)
                if entry.stat().st_mtime >= cutoff:
                    continue
                if self._is_source_referenced_by_books(entry):
                    logger.debug("Preserving stale upload still referenced by a book: %s", entry)
                    continue
                entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                logger.debug("Failed to remove stale web upload: %s", entry, exc_info=True)
        return removed

    @staticmethod
    def _source_handle(stored_name: str) -> str:
        return f"{UPLOAD_HANDLE_PREFIX}{stored_name}"

    @staticmethod
    def _safe_markdown_filename(title: str) -> str:
        # A title is not a file name: "Mr. Smith" has no extension to strip.
        safe_stem = sanitize_filename(title).strip() or "pasted_document"
        return source_filename(f"{safe_stem}.md")

    @staticmethod
    def _normalize_pasted_markdown(text: str, title: str) -> str:
        normalized = text.strip()
        if MARKDOWN_HEADING_RE.match(normalized):
            return normalized + "\n"
        return f"# {title}\n\n{normalized}\n"

    @staticmethod
    def _copy_upload_with_size_limit(file_obj, stored_path: Path, max_bytes: int) -> int:
        bytes_written = 0
        try:
            with stored_path.open("wb") as out_file:
                while chunk := file_obj.read(UPLOAD_COPY_CHUNK_BYTES):
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise UploadTooLargeError(
                            f"Uploaded source exceeds {max_bytes} bytes"
                        )
                    out_file.write(chunk)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        return bytes_written

    @staticmethod
    def _fallback_preflight(original_name: str, suffix: str, size_bytes: int, exc: Exception) -> dict:
        return {
            "title": Path(original_name).stem,
            "author": None,
            "chapter_count": 0,
            "source_type": suffix.lstrip("."),
            "sample": "",
            "warnings": [f"Source may be malformed or unreadable: {exc}"],
            "size_bytes": size_bytes,
            "requires_ocr": False,
            "pdf_text_layer": None,
        }

    def upload_source(
        self,
        *,
        filename: str,
        file_obj,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> dict:
        self._sweep_stale_uploads_when_due()

        original_name = Path(filename or "").name
        if not original_name:
            raise ValueError("Missing uploaded filename")

        suffix = Path(original_name).suffix.lower()
        if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
            raise UnsupportedSourceTypeError()

        destination_root = self._upload_root()
        ensure_owner_only_directory(destination_root)
        stored_name = f"{int(datetime.now().timestamp())}_{uuid4().hex[:8]}{suffix}"
        stored_path = destination_root / stored_name
        size_bytes = self._copy_upload_with_size_limit(
            file_obj,
            stored_path,
            max(1, int(max_bytes)),
        )
        try:
            owner_only(stored_path)
            try:
                preflight = inspect_source(stored_path, fallback_title=Path(original_name).stem)
            except Exception as exc:
                preflight = self._fallback_preflight(original_name, suffix, size_bytes, exc)
            return self._upload_response(
                stored_path, stored_name, original_name, size_bytes, preflight, (preflight or {}).get("title"),
            )
        except BaseException:
            # No handle was returned, so nothing else can reference the file.
            stored_path.unlink(missing_ok=True)
            raise

    def _upload_response(
        self, stored_path: Path, stored_name: str, original_name: str, size_bytes: int, preflight,
        title: str | None,
    ) -> dict:
        existing = self.find_existing_books(stored_path, title)
        if isinstance(preflight, dict):
            # Clients read duplicates from the preflight, as POST /api/sources/preflight sends them.
            preflight = {**preflight, "existing_books": existing}
        return {
            "source_handle": self._source_handle(stored_name),
            "original_name": original_name,
            "size_bytes": size_bytes,
            "preflight": preflight,
            "existing_books": existing,
        }

    def upload_text_source(
        self,
        *,
        text: str,
        title: str | None = None,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> dict:
        self._sweep_stale_uploads_when_due()

        normalized_title = (title or "").strip() or DEFAULT_PASTE_TITLE
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("Pasted text is empty")

        source_text = self._normalize_pasted_markdown(normalized_text, normalized_title)
        source_bytes = source_text.encode("utf-8")
        size_bytes = len(source_bytes)
        if size_bytes > max(1, int(max_bytes)):
            raise UploadTooLargeError(f"Pasted text exceeds {max_bytes} bytes")

        destination_root = self._upload_root()
        ensure_owner_only_directory(destination_root)
        original_name = self._safe_markdown_filename(normalized_title)
        stored_name = source_filename(
            original_name, prefix=f"{int(datetime.now().timestamp())}_{uuid4().hex[:8]}_",
        )
        stored_path = destination_root / stored_name
        try:
            stored_path.write_bytes(source_bytes)
            owner_only(stored_path)
            try:
                preflight = self.preflight_source(stored_path)
            except Exception as exc:
                preflight = {
                    **self._fallback_preflight(original_name, ".md", size_bytes, exc),
                    "title": normalized_title,
                }
            return self._upload_response(
                stored_path, stored_name, original_name, size_bytes, preflight, normalized_title,
            )
        except BaseException:
            stored_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def preflight_source(source_path: Path | str) -> dict:
        return inspect_source(source_path)

    def get_book_cover(self, book_id: int) -> CoverImage | None:
        with self.library_snapshot():
            book = self._repository.get_book(book_id)
            if book is None:
                raise FileNotFoundError("Book not found")
        # Rendering a PDF page or unzipping an EPUB must not hold the repository
        # lock; a source deleted meanwhile just yields no cover.
        return extract_cover(self._resolve_stored_path(book.source_path))

    def get_book_playback_file(self, book_id: int, chapter_number: int | None = None) -> Optional[Path]:
        detail = self._get_book_record(book_id)
        if detail is None:
            return None

        book, chapters = detail
        raw_path = getattr(book, "playback_path", None)
        if chapter_number is not None:
            chapter = next((item for item in chapters if item.chapter_number == chapter_number), None)
            raw_path = getattr(chapter, "mp3_path", None)
        playback_path = self._resolve_stored_path(raw_path)
        if playback_path is None:
            return None
        if not self._is_within_output_root(playback_path):
            return None
        if not playback_path.exists() or not playback_path.is_file():
            return None
        return playback_path

    def _source_path_exists(self, raw_path: Optional[str]) -> bool:
        if not raw_path:
            return False
        source_path = self._resolve_stored_path(raw_path)
        return bool(source_path and source_path.exists())

    def _audio_file_available(self, raw_path: Optional[str]) -> bool:
        path = self._resolve_stored_path(raw_path)
        return bool(path is not None and path.is_file() and self._is_within_output_root(path))

    def _published_recovery_chapters(self, raw_source_path: Optional[str]) -> int:
        """Count chapter files an interrupted conversion already published.

        Each atomically published chapter is a recovery checkpoint even before
        SQLite records it, so the library reports it as ready.
        """
        from modules.contracts.recovery_inventory import recovery_audio_paths

        source = self._resolve_stored_path(raw_source_path)
        if source is None:
            return 0
        return sum(
            1 for path in recovery_audio_paths(source).values()
            if path.is_file() and self._is_within_output_root(path)
        )

    def _ready_chapter_count(self, recorded: int, total: int, raw_source_path: Optional[str]) -> int:
        if total and recorded < total:
            return min(total, max(recorded, self._published_recovery_chapters(raw_source_path)))
        return recorded

    @staticmethod
    def _is_within_root(path: Path, root: Path) -> bool:
        resolved = _resolve(path)
        resolved_root = _resolve(root)
        folded_path = str(resolved).casefold()
        folded_root = str(resolved_root).casefold()
        # Containment implies a case-folded string prefix. Rejecting on the
        # string first avoids pathlib's per-part comparison, which dominates
        # when a deletion checks every library path against its directories.
        if folded_path != folded_root and not folded_path.startswith(folded_root.rstrip(os.sep) + os.sep):
            return False
        try:
            resolved.relative_to(resolved_root)
            return True
        except ValueError:
            if resolved.exists() and resolved_root.exists():
                for parent in (resolved, *resolved.parents):
                    try:
                        if os.path.samefile(parent, resolved_root):
                            return True
                    except OSError:
                        continue
            return False

    def _is_within_output_root(self, path: Path) -> bool:
        return self._is_within_root(path, self._output_dir)

    def _is_output_root(self, path: Path) -> bool:
        if _resolve(path) == _resolve(self._output_dir):
            return True
        try:
            return os.path.samefile(path, self._output_dir)
        except OSError:
            return False

    def _is_managed_source_path(self, path: Path) -> bool:
        return self._is_within_root(
            path,
            self._temp_dir / MANAGED_UPLOAD_ROOT_NAME,
        )

    def _app_owned_sources_root(self) -> Path:
        return resolve_app_owned_sources_root(
            self._workspace_root(),
            getattr(self._repository, "db_path", None),
        )

    def _is_app_owned_durable_source_path(self, path: Path) -> bool:
        return is_app_owned_durable_source(path, self._app_owned_sources_root())

    def _is_managed_deletable_source_path(self, path: Path) -> bool:
        # App-owned durable sources and real server uploads are the only
        # managed (deletable) source locations.
        return self._is_app_owned_durable_source_path(path) or self._is_managed_source_path(path)

    def _is_source_referenced_by_books(
        self,
        source_path: Path,
        *,
        exclude_book_id: int | None = None,
    ) -> bool:
        source_paths = getattr(self._repository, "list_source_paths", None)
        if callable(source_paths):
            target = source_path.resolve()
            return any(
                path.resolve() == target
                for raw in source_paths(exclude_book_id)
                if (path := self._resolve_stored_path(raw)) is not None
            )

        list_books = getattr(self._repository, "list_books", None)
        if list_books is None:
            return False

        target = source_path.resolve()
        # Page through the entire library rather than capping the scan: a shared
        # source referenced by a book beyond an arbitrary limit must not be
        # deleted out from under that book.
        page_size = 1_000
        offset = 0
        while True:
            books = list_books(BookFilters(limit=page_size, offset=offset))
            if not books:
                return False
            for book in books:
                if exclude_book_id is not None and book.id == exclude_book_id:
                    continue
                other_path = self._resolve_stored_path(getattr(book, "source_path", None))
                if other_path is not None and other_path.resolve() == target:
                    return True
            if len(books) < page_size:
                return False
            offset += page_size

    def _delete_staging_root(self) -> Path:
        return self._temp_dir / DELETE_STAGING_DIR_NAME

    def _other_book_artifact_paths(self, book_id: int) -> set[Path]:
        artifact_paths = getattr(self._repository, "list_artifact_paths", None)
        if callable(artifact_paths):
            return {
                _resolve(path)
                for raw in artifact_paths(book_id)
                if (path := self._resolve_stored_path(raw)) is not None
            }

        list_books = getattr(self._repository, "list_books", None)
        if list_books is None:
            return set()
        paths: set[Path] = set()
        offset = 0
        while True:
            books = list_books(BookFilters(limit=1000, offset=offset))
            for summary in books:
                if summary.id == book_id:
                    continue
                detail = self._repository.get_book_with_chapters(summary.id)
                if detail is None:
                    continue
                book, chapters = detail
                raw_paths = [book.source_path, book.playback_path, *(chapter.mp3_path for chapter in chapters)]
                for raw in raw_paths:
                    path = self._resolve_stored_path(raw)
                    if path is not None:
                        paths.add(path.resolve())
            if len(books) < 1000:
                return paths
            offset += 1000

    def _stage_artifact_for_deletion(self, path: Path, destination: Path) -> Path:
        if not path.exists():
            return path

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            for directory in (destination.parent, destination.parent.parent, path.parent):
                sync_directory(directory)
            os.replace(path, destination)
            sync_directory(path.parent)
            sync_directory(destination.parent)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise RuntimeError("Cannot stage audiobook files across filesystem boundaries.") from exc
            if getattr(exc, "winerror", None) == 5 and path.is_dir() and not path.is_junction():
                # Windows refuses to move a folder holding an open file with
                # ERROR_ACCESS_DENIED rather than a sharing violation.
                raise RuntimeError(
                    "Some files are in use or protected. Stop playback and try again."
                ) from exc
            if exc.errno in {errno.EACCES, errno.EPERM} and getattr(exc, "winerror", None) not in {32, 33}:
                raise RuntimeError("Cannot delete audiobook files: permission denied.") from exc
            if exc.errno not in {None, errno.EBUSY, errno.ETXTBSY, errno.EACCES, errno.EPERM}:
                raise RuntimeError(f"Cannot stage audiobook files: {exc.strerror or exc}.") from exc
            raise RuntimeError("Some files are in use. Stop playback and try again.") from exc
        return destination

    def _restore_staged_artifacts(self, staged: list[tuple[Path, Path]]) -> None:
        restore_staged_artifacts(staged)

    def _make_delete_durable(self, entry: Path) -> None:
        connection = getattr(self._repository, "_connection", None)
        if connection is not None:
            try:
                with connection() as conn:
                    # FULL checkpoints sync WAL and database even under NORMAL:
                    # https://www.sqlite.org/pragma.html#pragma_wal_checkpoint
                    result = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
                    if result[0] or result[1] < 0 or result[1] != result[2]:
                        raise RuntimeError("Cannot make book deletion durable: checkpoint incomplete")
            except sqlite3.Error as exc:
                raise RuntimeError("Cannot make book deletion durable") from exc
        mark_delete_transaction_committed(entry)

    def _commit_staged_artifacts(self, staged: list[tuple[Path, Path]]) -> None:
        try:
            remove_staged_artifacts(staged)
        except OSError as exc:
            raise RuntimeError("failed to delete staged audiobook artifacts") from exc

    def _recovery_output_dir(self, book) -> Optional[Path]:
        from modules.pipeline.recovery import recovery_audio_paths

        source = self._resolve_stored_path(getattr(book, "source_path", None))
        if source is None or not recovery_audio_paths(source):
            return None
        directory = source.parent.parent
        if self._is_within_output_root(directory) and not self._is_output_root(directory):
            return directory
        return None

    def _book_output_dir(self, book, chapters) -> Optional[Path]:
        recovery_directory = self._recovery_output_dir(book)
        if recovery_directory is not None:
            return recovery_directory
        candidate_paths = []
        playback_path = self._resolve_stored_path(getattr(book, "playback_path", None))
        if playback_path is not None:
            candidate_paths.append(playback_path)

        for chapter in chapters:
            chapter_path = self._resolve_stored_path(getattr(chapter, "mp3_path", None))
            if chapter_path is not None:
                candidate_paths.append(chapter_path)

        for candidate in candidate_paths:
            if not self._is_within_output_root(candidate):
                continue
            directory = candidate if candidate.is_dir() else candidate.parent
            if directory.name == "chapters":
                directory = directory.parent
            return directory

        safe_title = sanitize_book_title(getattr(book, "title", ""))
        if not safe_title:
            return None
        return self._output_dir / safe_title

    def _output_artifacts_for_deletion(self, book, chapters) -> tuple[set[Path], set[Path]]:
        output_dirs: set[Path] = set()
        output_files: set[Path] = set()

        def add_output_path(raw_path: Optional[str]) -> Optional[Path]:
            path = self._resolve_stored_path(raw_path)
            if path is None or not self._is_within_output_root(path):
                return None

            if path.is_dir():
                candidate_dir = path
            else:
                output_files.add(path)
                candidate_dir = path.parent

            if candidate_dir.name == "chapters":
                candidate_dir = candidate_dir.parent

            if self._is_within_output_root(candidate_dir) and not self._is_output_root(candidate_dir):
                output_dirs.add(candidate_dir)
            return path

        recovery_directory = self._recovery_output_dir(book)
        if recovery_directory is not None:
            output_dirs.add(recovery_directory)
        add_output_path(getattr(book, "playback_path", None))

        for chapter in chapters:
            chapter_path = add_output_path(getattr(chapter, "mp3_path", None))
            if chapter_path is None:
                continue

            artifact_dir = chapter_path if chapter_path.is_dir() else chapter_path.parent
            if not self._is_within_output_root(artifact_dir):
                continue

            chapter_number = int(getattr(chapter, "chapter_number", 0) or 0)
            if chapter_number <= 0:
                continue

            prefix = f"chapter_{chapter_number:02d}"
            for suffix in (
                "before_cleaning.md",
                "llm_input.md",
                "cleaned.md",
                "cleaning_trace.jsonl",
            ):
                output_files.add(artifact_dir / f"{prefix}_{suffix}")

        return output_dirs, output_files

    def _prune_empty_output_dirs(self, paths: set[Path]) -> None:
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            current = path.parent
            while self._is_within_output_root(current) and not self._is_output_root(current):
                try:
                    current.rmdir()
                except FileNotFoundError:
                    break
                except OSError:
                    break
                current = current.parent

    def book_cleanup_paths(self, book_id: int) -> list[tuple[Path, bool]]:
        """Potential deletion files and directory trees for admission fencing."""
        with self.library_snapshot():
            detail = self._get_book_record(book_id)
            if detail is None:
                return []
            book, chapters = detail
            directories, files = self._output_artifacts_for_deletion(book, chapters)
            source = self._resolve_stored_path(book.source_path)
            if source is not None and self._is_managed_deletable_source_path(source):
                files.add(source)
            return [(path, True) for path in directories] + [(path, False) for path in files]

    def _get_book_record(self, book_id: int):
        detail = self._repository.get_book_with_chapters(book_id)
        if detail is None:
            return None
        book, chapters = detail
        if not book.playback_path:
            found = self._ondisk_playback([chapter.mp3_path for chapter in chapters])
            if found is not None:
                book.playback_path, book.playback_status = found
        return book, chapters

    def _ondisk_playback(self, chapter_raw_paths: list[Optional[str]]) -> tuple[str, PlaybackStatus] | None:
        """Audio a book without a recorded playback file can still play from."""
        artifact = _find_ondisk_playback_artifact(
            title=None,
            playback_raw=None,
            chapter_raw_paths=chapter_raw_paths,
            workspace_root=self._workspace_root(),
            output_root=self._output_dir,
            temp_root=self._temp_dir,
            allow_title_fallback=False,
        )
        if artifact is not None:
            return str(artifact[0]), PlaybackStatus(artifact[1])
        for raw in chapter_raw_paths:
            path = self._resolve_stored_path(raw)
            if path is not None and self._is_within_output_root(path) and path.is_file():
                return str(path), PlaybackStatus.CHAPTER_AUDIO
        return None

    def _playable_audio_available(self, playback_path: Optional[str], chapter_raw_paths: list[Optional[str]]) -> bool:
        """The detail view's rule: a file ``/audio`` can serve exists."""
        if not playback_path:
            found = self._ondisk_playback(chapter_raw_paths)
            playback_path = found[0] if found is not None else None
        return self._audio_file_available(playback_path)

    def _chapter_audio_paths(self, book_ids: list[int]) -> dict[int, list[str]]:
        """Chapter audio paths of several books, in chapter order; call under the snapshot."""
        if not book_ids:
            return {}
        batch = getattr(self._repository, "list_chapter_audio_paths", None)
        if callable(batch):
            return batch(book_ids)
        get_detail = getattr(self._repository, "get_book_with_chapters", None)
        if not callable(get_detail):
            return {}
        paths: dict[int, list[str]] = {}
        for book_id in book_ids:
            detail = get_detail(book_id)
            if detail is not None:
                paths[book_id] = [chapter.mp3_path for chapter in detail[1] if chapter.mp3_path]
        return paths

    def library_snapshot(self, expected_epoch: str | None = None, *, mutation: bool = False):
        snapshot = getattr(self._repository, "library_snapshot", None)
        return snapshot(expected_epoch, mutation=mutation) if callable(snapshot) else nullcontext({})

    def get_library_books(
        self,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        conversion_mode=None,
        favorite: Optional[bool] = None,
    ) -> list[BookSummary]:
        resolved_conversion_mode = None
        if conversion_mode:
            try:
                resolved_conversion_mode = ConversionMode(str(conversion_mode))
            except ValueError:
                resolved_conversion_mode = None

        filters = BookFilters(
            search_query=search,
            limit=limit,
            offset=offset,
            conversion_mode=resolved_conversion_mode,
            favorite=favorite,
        )
        with self.library_snapshot():
            books = self._repository.list_books(filters)
            chapter_paths = self._chapter_audio_paths(
                [book.id for book in books if not getattr(book, "playback_path", None)]
            )
        # The rows are read; checking their files on disk does not need the
        # repository lock, so playback saves and conversion commits proceed.
        with _memoized_path_resolution():
            for book in books:
                book.source_available = self._source_path_exists(book.source_path)
                book.completed_chapters = self._ready_chapter_count(
                    book.completed_chapters or 0, book.total_chapters or 0, book.source_path,
                )
                # Restored or moved rows can outlive their audio; say so instead of
                # listing a book that cannot play.
                book.audio_available = self._playable_audio_available(
                    getattr(book, "playback_path", None), chapter_paths.get(book.id, []),
                )
        return books

    def get_library_count(self) -> int:
        return self._repository.count_books()

    def find_existing_books(self, source_path: Path | str, title: str | None = None) -> list[dict]:
        """Books already made from the same file contents or with the same title.

        Converting again (for example with other settings) stays allowed; this
        only gives the UI the facts to say so. Contents are compared by size
        first, so a file is hashed only when another source has its exact size.
        """
        import hashlib

        if not callable(getattr(self._repository, "list_books", None)):
            return []
        candidate = Path(source_path)
        try:
            candidate_size = candidate.stat().st_size
        except OSError:
            candidate_size = None
        candidate_digest: str | None = None
        wanted_title = (title or "").strip().casefold()

        def digest(path: Path) -> str | None:
            try:
                with path.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError:
                return None

        def library_rows():
            # Titles and sources only: the summary query aggregates every chapter.
            book_sources = getattr(self._repository, "list_book_sources", None)
            if callable(book_sources):
                yield from book_sources()
                return
            offset = 0
            while True:
                page = self._repository.list_books(BookFilters(limit=1000, offset=offset))
                yield from ((book.id, book.title, book.source_path) for book in page)
                if len(page) < 1000:
                    return
                offset += 1000

        matches: list[dict] = []
        for book_id, book_title, source_path in library_rows():
            match = None
            stored = self._resolve_stored_path(source_path)
            if stored is not None and candidate_size is not None:
                try:
                    same_size = stored.is_file() and stored.stat().st_size == candidate_size
                except OSError:
                    same_size = False
                if same_size:
                    if candidate_digest is None:
                        candidate_digest = digest(candidate) or ""
                    if candidate_digest and digest(stored) == candidate_digest:
                        match = "same_file"
            if match is None and wanted_title and (book_title or "").strip().casefold() == wanted_title:
                match = "same_title"
            if match is not None:
                matches.append({"id": book_id, "title": book_title, "match": match})
        return matches

    def books_with_missing_files(self) -> dict:
        """List rows whose audio or source file is gone; nothing is changed."""
        missing_audio: list[dict] = []
        missing_source: list[dict] = []
        rows: list[BookSummary] = []
        with self.library_snapshot():
            offset = 0
            while True:
                page = self._repository.list_books(BookFilters(limit=1000, offset=offset))
                rows.extend(page)
                if len(page) < 1000:
                    break
                offset += 1000
            chapter_paths = self._chapter_audio_paths([book.id for book in rows if not book.playback_path])
        with _memoized_path_resolution():
            for book in rows:
                entry = {"id": book.id, "title": book.title}
                if not self._playable_audio_available(book.playback_path, chapter_paths.get(book.id, [])):
                    missing_audio.append(entry)
                if not self._source_path_exists(book.source_path):
                    missing_source.append(entry)
        return {"books": len(rows), "missing_audio": missing_audio, "missing_source": missing_source}

    def get_book(self, book_id: int) -> Optional[dict]:
        with self.library_snapshot() as identity:
            detail = self._get_book_record(book_id)
            if detail is None:
                return None

            book, chapters = detail
            return {
                **identity,
                "id": book.id,
                "title": book.title,
                "author": book.author,
                "source_path": book.source_path,
                "source_type": book.source_type.value,
                "conversion_mode": book.conversion_mode.value,
                "total_chapters": book.total_chapters,
                "playback_path": book.playback_path,
                "playback_status": book.playback_status.value,
                "playback_position_sec": book.playback_position_sec,
                "tags": getattr(book, "tags", "") or "",
                "favorite": bool(getattr(book, "favorite", False)),
                "source_available": self._source_path_exists(book.source_path),
                "audio_available": self._audio_file_available(book.playback_path),
                # Same rule as the library list: a packaged book is complete.
                "completed_chapters": self._ready_chapter_count(
                    (book.total_chapters or 0)
                    if book.playback_status == PlaybackStatus.PACKAGED and book.playback_path
                    else sum(1 for chapter in chapters if chapter.mp3_path),
                    book.total_chapters or 0, book.source_path,
                ),
                "created_at": book.created_at,
                "chapters": [
                    {
                        "id": chapter.id,
                        "number": chapter.chapter_number,
                        "title": chapter.title,
                        "duration_ms": chapter.duration_ms,
                        "mp3_path": chapter.mp3_path,
                        "packaged_start_ms": chapter.packaged_start_ms,
                    }
                    for chapter in chapters
                ],
            }

    def update_book_metadata(
        self,
        book_id: int,
        *,
        title: Optional[str] = None,
        author: Optional[str] = None,
        tags: Optional[str] = None,
        favorite: Optional[bool] = None,
        expected_epoch: str | None = None,
    ) -> Optional[dict]:
        with self.library_snapshot(expected_epoch, mutation=True) as identity:
            updates = {}
            if title is not None:
                normalized_title = title.strip()
                if normalized_title:
                    updates["title"] = normalized_title
            if author is not None:
                updates["author"] = author.strip() or None
            if tags is not None:
                updates["tags"] = tags.strip()
            if favorite is not None:
                updates["favorite"] = bool(favorite)

            updated = self._repository.update_book(book_id, **updates)
            if updated is None:
                return None
            # The same detail GET /api/library/{id} returns, read under this snapshot.
            detail = self.get_book(book_id)
            return {**detail, **identity} if detail is not None else None

    def _read_text_if_exists(self, path: Path) -> str:
        try:
            parent = path.parent.resolve(strict=True)
            if path.is_symlink() or not self._is_within_output_root(parent) or not self._is_within_output_root(path):
                return ""
            validated = os.lstat(parent / path.name)
            if (
                not stat.S_ISREG(validated.st_mode)
                or getattr(validated, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or not path.is_file()
            ):
                return ""
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(parent / path.name, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
                opened = os.fstat(stream.fileno())
                current = os.lstat(parent / path.name)
                if (
                    not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                    or getattr(current, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or (opened.st_dev, opened.st_ino) != (validated.st_dev, validated.st_ino)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                ):
                    return ""
                return stream.read()
        except (OSError, RuntimeError):
            return ""

    def _read_trace_rows(self, path: Path) -> list[dict]:
        rows = []
        for line in self._read_text_if_exists(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows

    def get_chapter_cleaning_preview(self, book_id: int, chapter_number: int) -> Optional[dict]:
        detail = self._get_book_record(book_id)
        if detail is None:
            return None

        _book, chapters = detail
        selected = next(
            (chapter for chapter in chapters if chapter.chapter_number == chapter_number),
            None,
        )
        if selected is None:
            return None

        chapter_path = self._resolve_stored_path(getattr(selected, "mp3_path", None))
        artifact_dir = (
            chapter_path.parent
            if chapter_path is not None and self._is_within_output_root(chapter_path)
            else None
        )
        if artifact_dir is None:
            return {
                "book_id": book_id,
                "chapter_number": chapter_number,
                "title": selected.title,
                "available": False,
                "before_text": "",
                "llm_input_text": "",
                "cleaned_text": "",
                "trace_rows": [],
            }

        prefix = f"chapter_{chapter_number:02d}"
        before_text = self._read_text_if_exists(artifact_dir / f"{prefix}_before_cleaning.md")
        llm_input_text = self._read_text_if_exists(artifact_dir / f"{prefix}_llm_input.md")
        cleaned_text = self._read_text_if_exists(artifact_dir / f"{prefix}_cleaned.md")
        trace_rows = self._read_trace_rows(artifact_dir / f"{prefix}_cleaning_trace.jsonl")

        return {
            "book_id": book_id,
            "chapter_number": chapter_number,
            "title": selected.title,
            "available": bool(before_text or llm_input_text or cleaned_text or trace_rows),
            "before_text": before_text,
            "llm_input_text": llm_input_text,
            "cleaned_text": cleaned_text,
            "trace_rows": trace_rows,
        }

    def update_book_playback_position(self, book_id: int, position_sec: float, expected_epoch: str | None = None) -> Optional[float]:
        with self.library_snapshot(expected_epoch, mutation=True):
            updated_book = self._repository.update_book_playback_position(book_id, position_sec)
            if updated_book is None:
                return None
            return updated_book.playback_position_sec

    def _prefetch_artifact_resolutions(self, book_id: int) -> dict[Path, Path]:
        """Resolve the paths a deletion will compare before the repository lock.

        That is one filesystem round trip per stored path, which dominates a
        deletion of a long book or in a large library. The deletion re-reads the
        records under the lock and resolves only paths added meanwhile; the
        book's own record cannot change while its deletion is reserved.
        """
        resolutions: dict[Path, Path] = {}
        try:
            with _memoized_path_resolution(resolutions):
                artifact_paths = getattr(self._repository, "list_artifact_paths", None)
                if callable(artifact_paths):
                    self._other_book_artifact_paths(book_id)
                detail = self._get_book_record(book_id)
                if detail is not None:
                    output_dirs, output_files = self._output_artifacts_for_deletion(*detail)
                    for path in (*output_dirs, *output_files):
                        _resolve(path)
        except OSError:
            # Only a cache: the locked analysis resolves whatever is missing.
            pass
        return resolutions

    def remove_unowned_output_folder(self, folder: Path) -> None:
        """Remove an output folder no book owns, through a staged deletion.

        The caller holds the storage-cleanup reservation, so no conversion,
        restore or book deletion runs meanwhile. The folder must be a direct,
        real child of the output root that no library path points into.
        """
        if (folder.is_symlink() or folder.is_junction() or not folder.is_dir()
                or _resolve(folder).parent != _resolve(self._output_dir)):
            raise ValueError("Only a folder directly inside the output folder can be removed.")
        staging_root = self._delete_staging_root() / uuid4().hex
        original = _resolve(folder)
        staged = [(original, original.parent / LOCAL_DELETE_STAGING / staging_root.name / f"0-{original.name}")]
        with self.library_snapshot(mutation=True):
            if any(self._is_within_root(path, original) for path in self._other_book_artifact_paths(UNOWNED_FOLDER_BOOK_ID)):
                raise RuntimeError("A library book uses this folder; delete the book instead.")
            staging_root.mkdir(parents=True, exist_ok=True)
            try:
                write_delete_transaction(staging_root, UNOWNED_FOLDER_BOOK_ID, staged)
                self._stage_artifact_for_deletion(*staged[0])
                self._make_delete_durable(staging_root)
            except Exception:
                self._restore_staged_artifacts(staged)
                try:
                    finish_delete_transaction(staging_root, staged)
                except OSError:
                    pass
                raise
        try:
            self._commit_staged_artifacts(staged)
            finish_delete_transaction(staging_root, staged)
        except Exception:
            self._deletion_files_pending(UNOWNED_FOLDER_BOOK_ID, staging_root)
            raise

    def _plan_book_deletion(
        self, book_id: int, book, chapters, staging_root: Path,
    ) -> tuple[list[tuple[Path, Path]], set[Path]]:
        """Choose the artifacts a deletion stages, and where each one goes."""
        staged_output_files: set[Path] = set()
        output_dirs, output_files = self._output_artifacts_for_deletion(book, chapters)
        other_paths = self._other_book_artifact_paths(book_id)
        shared_dirs = {
            directory for directory in output_dirs
            if any(self._is_within_root(path, directory) for path in other_paths)
        }
        output_dirs -= shared_dirs
        own_paths = {
            _resolve(path) for raw in [book.playback_path, *(chapter.mp3_path for chapter in chapters)]
            if (path := self._resolve_stored_path(raw)) is not None
        }
        artifacts: list[Path] = []
        for output_dir in sorted(output_dirs, key=lambda path: len(path.parts)):
            if output_dir.exists() and not any(self._is_within_root(output_dir, parent) for parent in artifacts):
                artifacts.append(output_dir)

        for output_file in sorted(output_files):
            if any(self._is_within_root(output_file, output_dir) for output_dir in output_dirs):
                continue
            if _resolve(output_file) in other_paths:
                continue
            if _resolve(output_file) not in own_paths and any(self._is_within_root(output_file, directory) for directory in shared_dirs):
                continue
            if output_file.exists():
                artifacts.append(output_file)
                staged_output_files.add(output_file)

        source_path = self._resolve_stored_path(book.source_path)
        # other_paths already includes every remaining source as well
        # as audio references; scanning the library again adds no
        # protection while this deletion holds its library snapshot.
        if source_path is not None and self._is_managed_deletable_source_path(source_path):
            if source_path.exists() and _resolve(source_path) not in other_paths and not any(
                self._is_within_root(source_path, artifact) for artifact in artifacts
            ):
                artifacts.append(source_path)

        staged_artifacts = [
            (_resolve(path), _resolve(path).parent / LOCAL_DELETE_STAGING / staging_root.name / f"{index}-{path.name}")
            for index, path in enumerate(artifacts)
        ]
        return staged_artifacts, staged_output_files

    def delete_book(self, book_id: int, expected_epoch: str | None = None) -> bool | dict:
        """Delete a book row and its owned files through a staged transaction.

        Callers hold the book's JobCoordinator reservation (see
        ``AppController.delete_book_with_identity``): it excludes restore,
        storage cleanup, a repair of this book, and conversions of its files
        while the staged files are removed after the repository lock is released.
        """
        resolutions = self._prefetch_artifact_resolutions(book_id)
        with self.library_snapshot(expected_epoch, mutation=True):
            detail = self._get_book_record(book_id)
            if detail is None:
                return False

            book, chapters = detail
            staging_root = self._delete_staging_root() / uuid4().hex
            staging_root.mkdir(parents=True, exist_ok=True)
            staged_artifacts: list[tuple[Path, Path]] = []
            staged_output_files: set[Path] = set()
            db_deleted = False
            committed = False

            try:
                # Nothing is moved until the analysis ends, so each path
                # resolves once.
                with _memoized_path_resolution(resolutions):
                    staged_artifacts, staged_output_files = self._plan_book_deletion(
                        book_id, book, chapters, staging_root,
                    )
                # Before anything moves: no other library's records may sit in
                # or above what this deletion moves, stages or removes.
                self._verify_storage_ownership(removing=(
                    staging_root, *(path for pair in staged_artifacts for path in pair),
                ))
                write_delete_transaction(staging_root, book_id, staged_artifacts)
                for original, destination in staged_artifacts:
                    self._stage_artifact_for_deletion(original, destination)

                deleted = self._repository.delete_book(book_id)
                if not deleted:
                    self._restore_staged_artifacts(staged_artifacts)
                    return False

                db_deleted = True
                try:
                    # Unmarked transactions may contain the only copy after a failed rollback.
                    self._make_delete_durable(staging_root)
                except Exception:
                    return self._deletion_files_pending(book_id, staging_root)
                committed = True
            except Exception:
                if not db_deleted:
                    self._restore_staged_artifacts(staged_artifacts)
                raise
            finally:
                try:
                    if not committed and staging_root.exists():
                        finish_delete_transaction(staging_root, staged_artifacts)
                except OSError:
                    pass

        # The row is gone and the transaction is marked committed, so removing
        # the staged files no longer needs the repository lock: other library
        # reads and writes proceed meanwhile. Callers hold the book's
        # JobCoordinator reservation, which keeps restore and storage cleanup
        # (the other staging owners) out until this finishes.
        try:
            try:
                self._commit_staged_artifacts(staged_artifacts)
                finish_delete_transaction(staging_root, staged_artifacts)
            except Exception:
                return self._deletion_files_pending(book_id, staging_root)
            self._prune_empty_output_dirs(staged_output_files)
            return True
        finally:
            try:
                if staging_root.exists():
                    finish_delete_transaction(staging_root, staged_artifacts)
            except OSError:
                pass

    @staticmethod
    def _deletion_files_pending(book_id: int, staging_root: Path) -> dict:
        # The library removal succeeded; report files left for maintenance.
        logger.exception(
            "Failed to permanently remove staged artifacts after deleting book %s; "
            "leaving trash under %s for maintenance",
            book_id,
            staging_root,
        )
        return {"deleted": True, "files_pending": True, "path": staging_root.name}

    def open_book_folder(self, book_id: int) -> Optional[Path]:
        detail = self._get_book_record(book_id)
        if detail is None:
            return None

        book, chapters = detail
        output_dir = self._book_output_dir(book, chapters)
        if output_dir is not None and output_dir.exists():
            folder = output_dir
        else:
            source_path = self._resolve_stored_path(book.source_path)
            # A managed source lives in an internal folder shared by every book.
            if output_dir is not None and source_path is not None and self._is_managed_deletable_source_path(source_path):
                output_dir.mkdir(parents=True, exist_ok=True)
                folder = output_dir
            elif (source_path is not None and source_path.exists()
                    and not self._is_managed_deletable_source_path(source_path)):
                folder = source_path.parent
            elif output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                folder = output_dir
            else:
                return None

        self._folder_opener(folder)
        return folder

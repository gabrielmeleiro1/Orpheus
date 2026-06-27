"""Library CRUD/query service for AppController."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from modules.contracts.source_extensions import SUPPORTED_SOURCE_SUFFIXES
from modules.errors import sanitize_filename
from modules.ingestion.preflight import preflight_source as inspect_source
from modules.pipeline.book_workspace import sanitize_book_title
from modules.storage.maintenance import resolve_stored_path
from modules.storage.models import BookFilters, BookSummary, ConversionMode
from modules.storage.repository import IBookRepository

MANAGED_UPLOAD_ROOT_NAME = "web_uploads"
UPLOAD_HANDLE_PREFIX = "upload:"
SUPPORTED_UPLOAD_SUFFIXES = SUPPORTED_SOURCE_SUFFIXES
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024
DEFAULT_PASTE_TITLE = "Pasted Document"
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S+", re.MULTILINE)


class UploadTooLargeError(ValueError):
    """Raised when an uploaded source exceeds the configured size limit."""


def _default_folder_opener(folder: Path) -> None:
    if sys.platform == "darwin":
        command = ["open", str(folder)]
    elif os.name == "nt":
        command = ["explorer", str(folder)]
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
        self._output_dir = Path(output_dir).expanduser()
        self._temp_dir = Path(temp_dir).expanduser()
        self._storage_root = (
            Path(storage_root).expanduser()
            if storage_root is not None
            else self._output_dir.resolve().parent
        )
        self._folder_opener = folder_opener or _default_folder_opener

    def _workspace_root(self) -> Path:
        return self._storage_root.resolve()

    def _resolve_stored_path(self, raw_path: Optional[str]) -> Optional[Path]:
        return resolve_stored_path(
            raw_path,
            self._workspace_root(),
            output_root=self._output_dir,
            temp_root=self._temp_dir,
        )

    def _upload_root(self) -> Path:
        return self._temp_dir / MANAGED_UPLOAD_ROOT_NAME

    @staticmethod
    def _source_handle(stored_name: str) -> str:
        return f"{UPLOAD_HANDLE_PREFIX}{stored_name}"

    @staticmethod
    def _safe_markdown_filename(title: str) -> str:
        safe_title = sanitize_filename(title).strip()
        safe_stem = Path(safe_title).stem or "pasted_document"
        return f"{safe_stem}.md"

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
        original_name = Path(filename or "").name
        if not original_name:
            raise ValueError("Missing uploaded filename")

        suffix = Path(original_name).suffix.lower()
        if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
            raise ValueError(
                "Unsupported source type. Upload .pdf, .epub, .txt, or .md"
            )

        destination_root = self._upload_root()
        destination_root.mkdir(parents=True, exist_ok=True)
        stored_name = f"{int(datetime.now().timestamp())}_{uuid4().hex[:8]}{suffix}"
        stored_path = destination_root / stored_name
        size_bytes = self._copy_upload_with_size_limit(
            file_obj,
            stored_path,
            max(1, int(max_bytes)),
        )

        try:
            preflight = self.preflight_source(stored_path)
        except Exception as exc:
            preflight = self._fallback_preflight(original_name, suffix, size_bytes, exc)

        return {
            "source_handle": self._source_handle(stored_name),
            "original_name": original_name,
            "size_bytes": size_bytes,
            "preflight": preflight,
        }

    def upload_text_source(
        self,
        *,
        text: str,
        title: str | None = None,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> dict:
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
        destination_root.mkdir(parents=True, exist_ok=True)
        original_name = self._safe_markdown_filename(normalized_title)
        stored_name = f"{int(datetime.now().timestamp())}_{uuid4().hex[:8]}_{original_name}"
        stored_path = destination_root / stored_name
        stored_path.write_bytes(source_bytes)
        preflight = self.preflight_source(stored_path)

        return {
            "source_handle": self._source_handle(stored_name),
            "original_name": original_name,
            "size_bytes": size_bytes,
            "preflight": preflight,
        }

    @staticmethod
    def preflight_source(source_path: Path | str) -> dict:
        return inspect_source(source_path)

    def get_book_playback_file(self, book_id: int) -> Optional[Path]:
        detail = self._get_book_record(book_id)
        if detail is None:
            return None

        book, _chapters = detail
        playback_path = self._resolve_stored_path(getattr(book, "playback_path", None))
        if playback_path is None:
            return None
        if not self._is_within_output_root(playback_path):
            return None
        if not playback_path.exists() or not playback_path.is_file():
            return None
        return playback_path

    def _source_path_exists(self, raw_path: Optional[str]) -> bool:
        if not raw_path:
            return True
        source_path = self._resolve_stored_path(raw_path)
        return bool(source_path and source_path.exists())

    @staticmethod
    def _is_within_root(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _is_within_output_root(self, path: Path) -> bool:
        return self._is_within_root(path, self._output_dir)

    def _is_output_root(self, path: Path) -> bool:
        return path.resolve() == self._output_dir.resolve()

    def _is_managed_source_path(self, path: Path) -> bool:
        return self._is_within_root(
            path,
            self._temp_dir / MANAGED_UPLOAD_ROOT_NAME,
        )

    def _is_managed_upload_reference(self, path: Path) -> bool:
        if self._is_managed_source_path(path):
            return True
        return path.parent.name == MANAGED_UPLOAD_ROOT_NAME and path.parent.parent.name == "temp"

    def _book_output_dir(self, book, chapters) -> Optional[Path]:
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

    def _remove_output_artifacts(self, book, chapters) -> None:
        output_dirs, output_files = self._output_artifacts_for_deletion(book, chapters)

        for output_dir in sorted(output_dirs, key=lambda path: len(path.parts), reverse=True):
            if self._is_output_root(output_dir):
                continue
            try:
                shutil.rmtree(output_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError(f"failed to delete audiobook output directory: {output_dir}") from exc

        for output_file in sorted(output_files):
            if any(self._is_within_root(output_file, output_dir) for output_dir in output_dirs):
                continue
            self._unlink_path(output_file)

        self._prune_empty_output_dirs(output_files)

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

        # Only guess a title-derived directory when no real output directory
        # could be resolved from stored paths. Otherwise a book whose actual
        # directory was de-duplicated (e.g. "Title (2)") would also delete a
        # different book that owns the plain "Title" directory.
        if not output_dirs:
            safe_title = sanitize_book_title(getattr(book, "title", ""))
            if safe_title:
                fallback_dir = self._output_dir / safe_title
                if self._is_within_output_root(fallback_dir) and not self._is_output_root(fallback_dir):
                    output_dirs.add(fallback_dir)

        return output_dirs, output_files

    def _unlink_path(self, path: Path) -> None:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(f"failed to delete audiobook artifact: {path}") from exc

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

    def _get_book_record(self, book_id: int):
        return self._repository.get_book_with_chapters(book_id)

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
        books = self._repository.list_books(filters)
        for book in books:
            book.source_available = self._source_path_exists(book.source_path)
        return books

    def get_book(self, book_id: int) -> Optional[dict]:
        detail = self._get_book_record(book_id)
        if detail is None:
            return None

        book, chapters = detail
        return {
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
            "created_at": book.created_at,
            "chapters": [
                {
                    "id": chapter.id,
                    "number": chapter.chapter_number,
                    "title": chapter.title,
                    "duration_ms": chapter.duration_ms,
                    "mp3_path": chapter.mp3_path,
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
    ) -> Optional[dict]:
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
        return {
            "id": updated.id,
            "title": updated.title,
            "author": updated.author,
            "source_path": updated.source_path,
            "source_type": updated.source_type.value,
            "conversion_mode": updated.conversion_mode.value,
            "total_chapters": updated.total_chapters,
            "playback_path": updated.playback_path,
            "playback_status": updated.playback_status.value,
            "playback_position_sec": updated.playback_position_sec,
            "tags": getattr(updated, "tags", "") or "",
            "favorite": bool(getattr(updated, "favorite", False)),
            "source_available": self._source_path_exists(updated.source_path),
            "created_at": updated.created_at,
            "chapters": [],
        }

    @staticmethod
    def _read_text_if_exists(path: Path) -> str:
        if not path.exists() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _read_trace_rows(path: Path) -> list[dict]:
        if not path.exists() or not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
        artifact_dir = chapter_path.parent if chapter_path is not None else None
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

    def update_book_playback_position(self, book_id: int, position_sec: float) -> Optional[float]:
        updated_book = self._repository.update_book_playback_position(book_id, position_sec)
        if updated_book is None:
            return None
        return updated_book.playback_position_sec

    def delete_book(self, book_id: int) -> bool:
        detail = self._get_book_record(book_id)
        if detail is None:
            return False

        book, chapters = detail
        self._remove_output_artifacts(book, chapters)

        source_path = self._resolve_stored_path(book.source_path)
        if source_path is not None and (
            self._is_managed_source_path(source_path) or self._is_managed_upload_reference(source_path)
        ):
            self._unlink_path(source_path)

        return self._repository.delete_book(book_id)

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
            if output_dir is not None and source_path is not None and self._is_managed_upload_reference(source_path):
                output_dir.mkdir(parents=True, exist_ok=True)
                folder = output_dir
            elif source_path is not None and source_path.exists():
                folder = source_path.parent
            elif output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                folder = output_dir
            else:
                return None

        self._folder_opener(folder)
        return folder

    def get_library_stats(self) -> dict:
        return self._repository.get_library_stats()

"""Lifecycle tests for temp web uploads and safe TTL cleanup."""

from __future__ import annotations

import os
import time

from modules.app.services.library_service import LibraryService, WEB_UPLOAD_TTL_SECONDS
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def _library_service(tmp_path) -> LibraryService:
    return LibraryService(
        repository=object(),
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
    )


def test_sweep_removes_only_expired_uploads(tmp_path) -> None:
    service = _library_service(tmp_path)
    root = tmp_path / "temp" / "web_uploads"
    root.mkdir(parents=True)

    stale = root / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    fresh = root / "fresh.txt"
    fresh.write_text("new", encoding="utf-8")

    expired_mtime = time.time() - WEB_UPLOAD_TTL_SECONDS - 60
    os.utime(stale, (expired_mtime, expired_mtime))

    removed = service.sweep_stale_uploads()

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_leaves_fresh_upload_and_subdirectories(tmp_path) -> None:
    service = _library_service(tmp_path)
    root = tmp_path / "temp" / "web_uploads"
    root.mkdir(parents=True)

    fresh = root / "fresh.epub"
    fresh.write_text("data", encoding="utf-8")

    nested = root / "nested"
    nested.mkdir()
    expired_mtime = time.time() - WEB_UPLOAD_TTL_SECONDS - 60
    os.utime(nested, (expired_mtime, expired_mtime))

    removed = service.sweep_stale_uploads()

    assert removed == 0
    assert fresh.exists()
    assert nested.exists()


def test_sweep_preserves_expired_upload_referenced_by_library(tmp_path) -> None:
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    upload_root = temp_dir / "web_uploads"
    upload_root.mkdir(parents=True)
    source = upload_root / "queued.epub"
    source.write_text("queued", encoding="utf-8")
    expired_mtime = time.time() - WEB_UPLOAD_TTL_SECONDS - 60
    os.utime(source, (expired_mtime, expired_mtime))

    repository = SQLiteRepository(tmp_path / "data" / "app.db", output_dir=output_dir, temp_dir=temp_dir)
    try:
        repository.create_book(
            BookCreate(
                title="Queued",
                author="Author",
                source_path=str(source),
                source_type=SourceType.EPUB,
                total_chapters=1,
            )
        )
        service = LibraryService(repository, output_dir=output_dir, temp_dir=temp_dir)
        assert service.sweep_stale_uploads() == 0
        assert source.exists()
    finally:
        repository.close()


def test_sweep_handles_missing_upload_root(tmp_path) -> None:
    service = _library_service(tmp_path)
    assert service.sweep_stale_uploads() == 0

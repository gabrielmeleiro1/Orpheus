"""Lifecycle tests for temp web uploads: TTL sweep and durable-copy cleanup."""

from __future__ import annotations

import os
import time

from modules.app.services.conversion_service import ConversionService
from modules.app.services.library_service import LibraryService, WEB_UPLOAD_TTL_SECONDS


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


def test_sweep_handles_missing_upload_root(tmp_path) -> None:
    service = _library_service(tmp_path)
    assert service.sweep_stale_uploads() == 0


def test_delete_consumed_upload_only_removes_web_upload_paths(tmp_path) -> None:
    upload_root = (tmp_path / "temp" / "web_uploads").resolve()
    upload_root.mkdir(parents=True)

    inside = upload_root / "book.epub"
    inside.write_text("uploaded", encoding="utf-8")

    outside = tmp_path / "user_docs" / "book.epub"
    outside.parent.mkdir(parents=True)
    outside.write_text("user owned", encoding="utf-8")

    ConversionService._delete_consumed_upload(inside, upload_root)  # noqa: SLF001
    ConversionService._delete_consumed_upload(outside, upload_root)  # noqa: SLF001

    assert not inside.exists()
    assert outside.exists()

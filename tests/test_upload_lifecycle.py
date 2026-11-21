"""Lifecycle tests for temp web uploads and safe TTL cleanup."""

from __future__ import annotations

import os
import time

from modules.app.services.library_service import LibraryService, WEB_UPLOAD_TTL_SECONDS
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository
from modules.web.source_handles import resolve_source_reference


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


def test_sweep_keeps_quarantined_library_uploads_until_a_backup_is_restored(tmp_path) -> None:
    db = tmp_path / "data" / "app.db"
    output_dir, temp_dir = tmp_path / "output", tmp_path / "temp"
    repository = SQLiteRepository(db, output_dir=output_dir, temp_dir=temp_dir)
    upload = temp_dir / "web_uploads" / "legacy.epub"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"the damaged library's only source")
    repository.create_book(BookCreate(title="Legacy", source_path=str(upload), source_type=SourceType.EPUB))
    repository.close()
    expired_mtime = time.time() - WEB_UPLOAD_TTL_SECONDS - 60
    os.utime(upload, (expired_mtime, expired_mtime))
    db.write_bytes(b"SQLite format 3\x00" + b"\xde\xad" * 500)

    repository = SQLiteRepository(db, output_dir=output_dir, temp_dir=temp_dir)
    try:
        assert repository.quarantine is not None
        service = LibraryService(repository, output_dir=output_dir, temp_dir=temp_dir)
        assert service.sweep_stale_uploads() == 0
    finally:
        repository.close()
    assert upload.read_bytes() == b"the damaged library's only source"


def test_sweep_handles_missing_upload_root(tmp_path) -> None:
    service = _library_service(tmp_path)
    assert service.sweep_stale_uploads() == 0


def test_upload_handles_reject_links_and_directories(tmp_path) -> None:
    root = tmp_path / "temp" / "web_uploads"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = root / "linked.txt"
    link.symlink_to(outside)
    directory = root / "directory.txt"
    directory.mkdir()

    for name in (link.name, directory.name):
        try:
            resolve_source_reference(f"upload:{name}", tmp_path / "temp")
        except ValueError as exc:
            assert "invalid" in str(exc)
        else:
            raise AssertionError("non-file upload handle was accepted")


def test_upload_sweep_does_not_follow_symlink_targets(tmp_path) -> None:
    service = _library_service(tmp_path)
    root = tmp_path / "temp" / "web_uploads"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = root / "old-link.txt"
    link.symlink_to(outside)
    expired_mtime = time.time() - WEB_UPLOAD_TTL_SECONDS - 60
    os.utime(outside, (expired_mtime, expired_mtime))
    if os.utime in os.supports_follow_symlinks:
        os.utime(link, (expired_mtime, expired_mtime), follow_symlinks=False)
    target_stat = outside.stat()

    assert service.sweep_stale_uploads() == 0
    assert link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "private"
    assert outside.stat().st_mtime_ns == target_stat.st_mtime_ns
    assert outside.stat().st_mode == target_stat.st_mode

"""A library must never collect managed files belonging to another database."""

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.app.services.library_service import LibraryService
from modules.storage.maintenance import backup_database, prune_stale_records_and_files
from modules.storage.managed_roots import STORAGE_OWNER_NAME, StorageOwnershipError, claim_managed_roots
from modules.storage.sqlite_repo import SQLiteRepository
from modules.storage.models import BookCreate, SourceType


@pytest.mark.parametrize("live", [False, True])
def test_different_database_cannot_collect_sources_after_owner_stops_or_while_running(tmp_path, live):
    db = tmp_path / "data" / "b.db"
    script = """
import sys
from pathlib import Path
from modules.storage.sqlite_repo import SQLiteRepository
repo = SQLiteRepository(Path(sys.argv[1]), output_dir=Path(sys.argv[2]), temp_dir=Path(sys.argv[3]))
print('ready', flush=True)
sys.stdin.readline()
repo.close()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(db), str(tmp_path / "out-b"), str(tmp_path / "tmp-b")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        source = db.parent / "sources" / "book.epub"
        source.write_bytes(b"another library's only source")
        old = time.time() - 2 * 86400
        os.utime(source, (old, old))
        if not live:
            child.communicate("\n", timeout=10)
            assert child.returncode == 0
        with pytest.raises(StorageOwnershipError):
            SQLiteRepository(db.with_name("a.db"), output_dir=tmp_path / "out-a", temp_dir=tmp_path / "tmp-a")
        with pytest.raises(StorageOwnershipError):
            prune_stale_records_and_files(
                db.with_name("a.db"), output_dir=tmp_path / "out-a", temp_dir=tmp_path / "tmp-a",
                aggressive=True,
            )
        assert source.read_bytes() == b"another library's only source"
    finally:
        if child.poll() is None:
            child.communicate("\n", timeout=10)


def test_cleanup_keeps_quarantined_library_files_until_a_backup_is_restored(tmp_path):
    db = tmp_path / "data" / "audiobooks.db"
    output, temp = tmp_path / "output", tmp_path / "temp"
    repo = SQLiteRepository(db, output_dir=output, temp_dir=temp)
    source = db.parent / "sources" / "novel.epub"
    source.write_bytes(b"the damaged library's only source")
    repo.create_book(BookCreate(title="Novel", source_path=str(source), source_type=SourceType.EPUB))
    repo.close()
    folder = output / "Novel"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("partial conversion")
    upload = temp / "web_uploads" / "legacy.epub"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"legacy upload source")
    old = time.time() - 2 * 86400
    for path in (source, folder / "notes.txt", folder, upload):
        os.utime(path, (old, old))
    db.write_bytes(b"SQLite format 3\x00" + b"\xde\xad" * 500)

    repo = SQLiteRepository(db, output_dir=output, temp_dir=temp)
    try:
        assert repo.quarantine is not None
        report = prune_stale_records_and_files(db, output_dir=output, temp_dir=temp, aggressive=True)
    finally:
        repo.close()

    assert source.read_bytes() == b"the damaged library's only source"
    assert (folder / "notes.txt").exists()
    assert upload.read_bytes() == b"legacy upload source"
    summary = report["summary"]
    assert summary["removable"]["orphan_sources"] == 0
    assert summary["removable"]["output_folders"] == 0
    assert summary["removable"]["temp_uploads"] == 0
    [kept] = [group for group in summary["kept"] if group["category"] == "quarantined_library_files"]
    assert kept["reason"] == "library_quarantined"
    assert sorted(item["name"] for item in kept["items"]) == ["Novel", "legacy.epub", "novel.epub"]


@pytest.mark.parametrize("shared", ["output", "temp"])
@pytest.mark.parametrize("live", [False, True])
def test_separate_database_directories_cannot_reuse_managed_root(tmp_path, shared, live):
    common = tmp_path / "shared"
    first = SQLiteRepository(
        tmp_path / "a" / "library.db",
        output_dir=common if shared == "output" else tmp_path / "out-a",
        temp_dir=common if shared == "temp" else tmp_path / "tmp-a",
    )
    retained = common / "owned.txt"
    retained.write_bytes(b"retain")
    try:
        if not live:
            first.close()
        with pytest.raises(StorageOwnershipError):
            SQLiteRepository(
                tmp_path / "b" / "library.db",
                output_dir=common if shared == "output" else tmp_path / "out-b",
                temp_dir=common if shared == "temp" else tmp_path / "tmp-b",
            )
        assert retained.read_bytes() == b"retain"
    finally:
        first.close()


@pytest.mark.parametrize("parent_first", [False, True])
def test_nested_roots_cannot_have_different_database_owners(tmp_path, parent_first):
    parent, child = tmp_path / "managed", tmp_path / "managed" / "nested"
    first, second = (parent, child) if parent_first else (child, parent)
    claim_managed_roots(tmp_path / "a.db", first)
    with pytest.raises(StorageOwnershipError):
        claim_managed_roots(tmp_path / "b.db", second)


def test_legacy_sources_shared_by_existing_databases_are_not_claimed_or_deleted(tmp_path):
    source = tmp_path / "sources" / "retain.epub"
    source.parent.mkdir()
    source.write_bytes(b"existing unclaimed source")
    for name in ("a.db", "b.db"):
        with sqlite3.connect(tmp_path / name) as conn:
            conn.execute("CREATE TABLE legacy (id INTEGER)")
    with pytest.raises(StorageOwnershipError, match="multiple library databases"):
        SQLiteRepository(tmp_path / "a.db")
    assert source.read_bytes() == b"existing unclaimed source"
    assert not (source.parent / STORAGE_OWNER_NAME).exists()


def test_cleanup_never_expires_ownership_and_backups_do_not_claim_managed_roots(tmp_path):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        marker = repo.db_path.parent / "sources" / STORAGE_OWNER_NAME
        before = marker.read_bytes()
        backup = backup_database(repo.db_path)
        prune_stale_records_and_files(repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0)
        assert marker.read_bytes() == before
        assert not (backup.parent / "sources").exists()
    finally:
        repo.close()


def test_controller_uses_canonical_database_for_backup_list_restore_and_diagnostics(tmp_path):
    real = tmp_path / "real" / "library.db"
    alias = tmp_path / "alias" / "library.db"
    real.parent.mkdir()
    alias.parent.mkdir()
    alias.symlink_to(real)
    controller = AppController(AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        db_path=alias, worker_pool_reuse_enabled=False,
    ))
    try:
        assert controller.config.db_path == real.resolve()
        backup = controller.backup_database()
        assert backup.parent == real.resolve().parent / "backups"
        assert backup.name in {item["id"] for item in controller.list_database_backups()}
        alias.unlink()
        alias.symlink_to(tmp_path / "other.db")
        controller.restore_database_backup(backup.name)
        assert controller.get_storage_diagnostics()["database_path"] == str(real.resolve())
        assert not (tmp_path / "other.db").exists()
    finally:
        controller.repository.close()


def test_retargeting_output_alias_cannot_delete_another_librarys_audio(tmp_path):
    output_a, output_b, alias = tmp_path / "out-a", tmp_path / "out-b", tmp_path / "alias"
    output_a.mkdir()
    alias.symlink_to(output_a, target_is_directory=True)
    first = SQLiteRepository(tmp_path / "a" / "library.db", output_dir=alias, temp_dir=tmp_path / "tmp-a")
    second = SQLiteRepository(tmp_path / "b" / "library.db", output_dir=output_b, temp_dir=tmp_path / "tmp-b")
    try:
        service = LibraryService(first, output_dir=alias, temp_dir=tmp_path / "tmp-a")
        for repo in (first, second):
            repo.create_book(BookCreate(
                title="Book", source_path="external.txt", source_type=SourceType.TXT,
                playback_path="output/Book/audio.m4b",
            ))
        victim = output_b / "Book" / "audio.m4b"
        victim.parent.mkdir()
        victim.write_bytes(b"second library audio")
        alias.unlink()
        alias.symlink_to(output_b, target_is_directory=True)
        assert service.delete_book(1) is True
        assert victim.read_bytes() == b"second library audio"
        assert second.get_book(1) is not None
    finally:
        first.close()
        second.close()


def test_controller_freezes_configured_output_and_temp_aliases(tmp_path):
    target = tmp_path / "managed"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    controller = AppController(AppConfig(
        data_dir=tmp_path / "data", db_path=tmp_path / "data" / "library.db",
        output_dir=alias / "output", temp_dir=alias / "temp", worker_pool_reuse_enabled=False,
    ))
    try:
        alias.unlink()
        alias.symlink_to(tmp_path / "replacement", target_is_directory=True)
        assert controller.config.output_dir == target / "output"
        assert controller.config.temp_dir == target / "temp"
    finally:
        controller.repository.close()

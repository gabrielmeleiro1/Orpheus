"""Rollback evidence must survive restarts and backup retention."""

from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil

import pytest

from modules.storage import maintenance
from modules.storage.db_ownership import acquire_database
from modules.storage.maintenance_paths import _connect
from modules.storage.models import BookCreate, SourceType
from modules.storage.restore_recovery import (
    record_restore_rollback, recover_restore_rollback, rollback_intent_path,
)
from modules.storage.sqlite_repo import SQLiteRepository
from tests.test_database_ownership import paused_process


@pytest.fixture
def pending_rollback(tmp_path):
    db = tmp_path / "library.db"
    repo = SQLiteRepository(db)
    # Restore journals use the owned identity, including Windows case
    # normalization, rather than the caller's original path spelling.
    db = repo.db_path
    repo.create_book(BookCreate(title="Current", source_path="source.txt", source_type=SourceType.TXT))
    epoch = repo.get_library_identity()["library_epoch"]
    saved = maintenance.backup_database(db)
    repo.close()
    safety = saved.with_name("library_safety_recovery.db")
    shutil.copyfile(saved, safety)
    record_restore_rollback(db, safety)
    db.write_bytes(b"failed restore publication")
    return db, safety, epoch


def test_rollback_replays_before_sqlite_opens_or_quarantines_live_file(pending_rollback):
    db, safety, epoch = pending_rollback
    repo = SQLiteRepository(db)
    try:
        assert repo.get_book(1).title == "Current"
        assert repo.get_library_identity()["library_epoch"] == epoch
        assert repo.quarantine is None
        assert not rollback_intent_path(db).exists()
        assert safety.exists()
        assert not list(db.parent.glob("*.corrupt-*"))
    finally:
        repo.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_ordinary_maintenance_cannot_open_a_pending_rollback(pending_rollback, read_only):
    db, _, _ = pending_rollback
    with pytest.raises(RuntimeError, match="rollback recovery is pending"):
        _connect(db, read_only=read_only)
    assert db.read_bytes() == b"failed restore publication"


def test_failed_replay_keeps_intent_and_retries_from_intact_safety_copy(pending_rollback):
    db, safety, _ = pending_rollback
    before = safety.read_bytes()

    def partial_copy(source, target):
        target.write_bytes(source.read_bytes()[:100])
        raise OSError("interrupted safety copy")

    with acquire_database(db) as owner, owner.lock, pytest.raises(OSError, match="interrupted"):
        recover_restore_rollback(db, copy=partial_copy)
    assert rollback_intent_path(db).exists()
    assert safety.read_bytes() == before
    assert db.read_bytes() == b"failed restore publication"
    repo = SQLiteRepository(db)
    try:
        assert repo.get_book(1).title == "Current"
    finally:
        repo.close()


def test_invalid_rollback_path_fails_closed(pending_rollback, tmp_path):
    db, safety, _ = pending_rollback
    outside = tmp_path / "outside.db"
    outside.write_bytes(safety.read_bytes())
    marker = rollback_intent_path(db)
    payload = json.loads(marker.read_text())
    payload["safety"] = str(outside)
    marker.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Invalid restore rollback intent"):
        SQLiteRepository(db)
    assert db.read_bytes() == b"failed restore publication"
    assert marker.exists()
    assert outside.read_bytes() == safety.read_bytes()


def test_pending_rollback_never_follows_replaced_database_name(pending_rollback, tmp_path):
    db, _, _ = pending_rollback
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"unrelated bytes")
    db.unlink()
    db.symlink_to(outside)
    with pytest.raises(ValueError, match="must not be a link"):
        SQLiteRepository(db)
    with pytest.raises(ValueError, match="must not be a link"):
        _connect(db)
    assert outside.read_bytes() == b"unrelated bytes"
    assert rollback_intent_path(db).exists()
    assert not Path(str(outside) + ".lock").exists()


def test_backup_retention_preserves_a_pending_rollback_safety_copy(pending_rollback):
    db, safety, _ = pending_rollback
    later = safety.with_name("library_newest.db")
    later.write_bytes(safety.read_bytes())
    maintenance._enforce_retention(safety.parent, "library_*.db", keep=1, databases=True)
    assert safety.exists()
    assert later.exists()
    assert rollback_intent_path(db).exists()


def test_retention_bounds_backup_locks_and_skips_open_backups(tmp_path, monkeypatch):
    class Clock:
        ticks = 0

        @classmethod
        def now(cls):
            cls.ticks += 1
            return datetime(2026, 10, 2) + timedelta(seconds=cls.ticks)

    monkeypatch.setattr(maintenance, "datetime", Clock)
    monkeypatch.setattr(maintenance, "DEFAULT_RETENTION_KEEP", 2)
    repo = SQLiteRepository(tmp_path / "library.db")
    try:
        first = maintenance.backup_database(repo.db_path)
        orphan = first.with_name("library_orphan.db.lock")
        orphan.touch()
        with closing(_connect(first)):
            for _ in range(5):
                maintenance.backup_database(repo.db_path)
            assert first.exists()
            assert not orphan.exists()
        maintenance.backup_database(repo.db_path)
        assert not first.exists()
        assert len(list(first.parent.glob("*.db"))) == 2
        assert len(list(first.parent.glob("*.db.lock"))) == 2
    finally:
        repo.close()


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="POSIX fork handle tracking")
def test_directory_gate_handle_closes_when_lock_wait_is_interrupted(tmp_path, monkeypatch):
    import fcntl
    from modules.storage import db_ownership

    flock = fcntl.flock
    observed = []

    def inspect(handle, flags):
        if Path(handle.name).name == db_ownership.DIRECTORY_GATE_NAME:
            observed.append(handle)
            raise OSError("interrupted before acquiring gate")
        return flock(handle, flags)

    monkeypatch.setattr(fcntl, "flock", inspect)
    with pytest.raises(OSError, match="interrupted"):
        acquire_database(tmp_path / "library.db")
    assert observed and observed[0].closed


def test_retention_preserves_backup_held_by_another_process(tmp_path):
    repo = SQLiteRepository(tmp_path / "library.db")
    try:
        older = maintenance.backup_database(repo.db_path)
        newer = maintenance.backup_database(repo.db_path)
        os.utime(older, (1, 1))
        script = """
import sys
from modules.storage.maintenance_paths import _connect
connection = _connect(sys.argv[1])
print('ready', flush=True)
sys.stdin.read(1)
"""
        with paused_process(script, older):
            maintenance._enforce_retention(older.parent, "library_*.db", keep=1, databases=True)
            assert older.exists()
        maintenance._enforce_retention(older.parent, "library_*.db", keep=1, databases=True)
        assert not older.exists()
        assert not Path(str(older) + ".lock").exists()
        assert newer.exists()
    finally:
        repo.close()

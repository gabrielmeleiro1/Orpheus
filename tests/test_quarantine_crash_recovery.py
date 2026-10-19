"""Quarantine recovery after in-process interruption."""

import json
import sqlite3
import threading
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from modules.storage.sqlite_repo import SQLiteRepository


class InterruptedQuarantine(BaseException):
    pass


@pytest.mark.parametrize("step", ["intent", "database", "wal", "shm", "status", "complete"])
def test_startup_completes_interrupted_quarantine_before_opening_sqlite(tmp_path, monkeypatch, step):
    db = tmp_path / "library.db"
    originals = {db: b"corrupt database", Path(str(db) + "-wal"): b"saved wal", Path(str(db) + "-shm"): b"saved shm"}
    for path, data in originals.items():
        path.write_bytes(data)
    repo = SQLiteRepository.__new__(SQLiteRepository)
    repo._lock = threading.RLock()
    repo.db_path = db
    repo._temp_dir = tmp_path / "temp"
    repo._conn = Mock()
    repo._conn.execute.side_effect = sqlite3.DatabaseError("unreadable")
    repo.library_generation = 0
    replace = Path.replace
    intent = Path(str(db) + ".quarantine-intent.json")
    status = Path(str(db) + ".quarantine.json")

    def interrupt(path, target):
        result = replace(path, target)
        target = Path(target)
        phase = json.loads(intent.read_text())["phase"] if intent.exists() else None
        reached = (
            (step == "intent" and target == intent and phase == "moving")
            or (step == "database" and path == db)
            or (step == "wal" and path == Path(str(db) + "-wal"))
            or (step == "shm" and path == Path(str(db) + "-shm"))
            or (step == "status" and target == status)
            or (step == "complete" and target == intent and phase == "complete")
        )
        if reached:
            raise InterruptedQuarantine()
        return result

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(InterruptedQuarantine):
        repo._quarantine_corrupt_database(sqlite3.DatabaseError("unreadable"))
    assert intent.exists()
    monkeypatch.setattr(Path, "replace", replace)
    reopened = SQLiteRepository(db)
    quarantine = Path(reopened.quarantine["path"])
    assert quarantine.read_bytes() == originals[db]
    assert Path(str(quarantine) + "-wal").read_bytes() == originals[Path(str(db) + "-wal")]
    assert Path(str(quarantine) + "-shm").read_bytes() == originals[Path(str(db) + "-shm")]
    assert reopened.count_books() == 0
    assert not intent.exists()
    reopened.close()


def test_quarantine_does_not_move_files_until_intent_is_synced(tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    db.write_bytes(b"corrupt")
    pending = db.with_name(db.name + ".quarantine-intent.json.tmp")
    open_path, fsync = Path.open, os.fsync
    intent_stream = None
    synced = []

    def open_intent(path, *args, **kwargs):
        nonlocal intent_stream
        stream = open_path(path, *args, **kwargs)
        if path == pending:
            intent_stream = stream
        return stream

    def sync(descriptor):
        if intent_stream is not None and not intent_stream.closed and descriptor == intent_stream.fileno():
            synced.append(pending)
            raise OSError("sync failed")
        return fsync(descriptor)

    monkeypatch.setattr(Path, "open", open_intent)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="sync failed"):
        SQLiteRepository(db)
    assert synced == [pending]
    assert db.is_file()
    assert db.read_bytes() == b"corrupt"
    assert not list(tmp_path.glob("library.db.corrupt-*"))


@pytest.mark.parametrize("step", ["rollback_intent", "wal", "database"])
def test_startup_resumes_interrupted_quarantine_rollback(tmp_path, monkeypatch, step):
    db = tmp_path / "library.db"
    originals = {db: b"corrupt database", Path(str(db) + "-wal"): b"saved wal", Path(str(db) + "-shm"): b"saved shm"}
    for path, data in originals.items():
        path.write_bytes(data)
    repo = SQLiteRepository.__new__(SQLiteRepository)
    repo._lock = threading.RLock()
    repo.db_path = db
    repo._temp_dir = tmp_path / "temp"
    repo._conn = Mock()
    repo._conn.execute.side_effect = sqlite3.DatabaseError("unreadable")
    repo.library_generation = 0
    replace = Path.replace
    intent = Path(str(db) + ".quarantine-intent.json")

    def interrupt(path, target):
        if path == Path(str(db) + "-shm"):
            raise OSError("locked")
        result = replace(path, target)
        target = Path(target)
        phase = json.loads(intent.read_text())["phase"] if intent.exists() else None
        if phase == "rollback" and (
            (step == "rollback_intent" and target == intent)
            or (step == "wal" and target == Path(str(db) + "-wal"))
            or (step == "database" and target == db)
        ):
            raise InterruptedQuarantine()
        return result

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(InterruptedQuarantine):
        repo._quarantine_corrupt_database(sqlite3.DatabaseError("unreadable"))
    monkeypatch.setattr(Path, "replace", replace)
    # Verify rollback before opening SQLite, which may discard invalid journals.
    resumed = SQLiteRepository.__new__(SQLiteRepository)
    resumed.db_path = db
    resumed._resume_quarantine()
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert not intent.exists()
    assert not list(tmp_path.glob("library.db.corrupt-*"))


def test_quarantine_recovery_preserves_conflicting_files_without_opening_database(tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    target = tmp_path / "library.db.corrupt-saved"
    db.write_bytes(b"current")
    target.write_bytes(b"original")
    intent = {
        "version": 1, "phase": "moving", "status": {"path": str(target), "created_at": "now"},
        "files": [{"source": str(db) + suffix, "destination": str(target) + suffix, "required": suffix == ""}
                  for suffix in ("", "-wal", "-shm")],
    }
    Path(str(db) + ".quarantine-intent.json").write_text(json.dumps(intent))
    connect = Mock(side_effect=AssertionError("database must remain closed"))
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(OSError, match="Both database quarantine paths exist"):
        SQLiteRepository(db)
    connect.assert_not_called()
    assert db.read_bytes() == b"current"
    assert target.read_bytes() == b"original"

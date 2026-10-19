"""Replay a durable restore rollback before opening the live SQLite database."""

from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from modules.contracts.durable_files import atomic_write_json, sync_directory, sync_file
from modules.storage.permissions import ensure_owner_only_directory
from modules.storage.sqlite_uri import readonly_sqlite_uri


def rollback_intent_path(database: Path) -> Path:
    return database.with_name(database.name + ".restore-rollback.json")


def _read_intent(database: Path) -> Path | None:
    marker = rollback_intent_path(database)
    try:
        if marker.is_symlink():
            raise ValueError("Restore rollback intent must not be a link")
        record = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    safety = Path(record["safety"])
    backup_root = database.parent / "backups"
    if (
        record.get("version") != 1 or record.get("database") != str(database)
        or safety.parent != backup_root or safety.suffix != database.suffix
        or not safety.name.startswith(database.stem + "_safety_")
        or backup_root.is_symlink() or backup_root.is_junction()
        or safety.is_symlink() or not safety.is_file()
        or database.is_symlink()
    ):
        raise ValueError("Invalid restore rollback intent; safety files have been preserved")
    return safety


def record_restore_rollback(database: Path, safety: Path) -> None:
    # The journal must never outlive the bytes it promises to recover.
    sync_file(safety)
    sync_directory(safety.parent)
    atomic_write_json(
        rollback_intent_path(database),
        {"version": 1, "database": str(database), "safety": str(safety)},
        sync_parent=sync_directory,
    )


def pending_restore_safety_copies(backups: Path) -> set[Path]:
    """Identify backups that retention must keep until rollback completes."""
    protected = set()
    suffix = ".restore-rollback.json"
    for marker in backups.parent.glob("*" + suffix):
        database = marker.with_name(marker.name[:-len(suffix)])
        safety = _read_intent(database)
        if safety is not None:
            protected.add(safety)
    return protected


def recover_restore_rollback(database: Path, *, copy=shutil.copyfile, reopen=None):
    """Re-publish the safety copy, keeping intent until reopen succeeds.

    The caller must hold process and thread ownership. Maintenance connections
    refuse pending rollback; repository initialization replays it before either
    SQLite initialization or corruption quarantine can inspect the live file.
    """
    # The caller acquired ownership of this exact canonical name. Resolving it
    # again could follow a replacement link and bypass the adjacent journal.
    database = Path(os.path.normcase(os.path.abspath(database)))
    safety = _read_intent(database)
    if safety is None:
        return None
    with closing(sqlite3.connect(readonly_sqlite_uri(safety), uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Restore safety copy failed SQLite integrity check")
    staging_dir = database.parent / f".{database.name}.restore-staging"
    if staging_dir.is_symlink() or staging_dir.is_junction():
        raise ValueError("Restore staging directory must not be a link")
    ensure_owner_only_directory(staging_dir)
    descriptor, name = tempfile.mkstemp(
        prefix=f".restore-{database.name}-", suffix=database.suffix, dir=staging_dir,
    )
    os.close(descriptor)
    staging = Path(name)
    connection = None
    try:
        copy(safety, staging)
        sync_file(staging)
        for suffix in ("-wal", "-shm", "-journal"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        staging.replace(database)
        sync_directory(database.parent)
        if reopen is not None:
            connection = reopen(database)
        else:
            # Validate publication before retiring the journal. The repository
            # then opens/configures its own persistent connection normally.
            connection = sqlite3.connect(database)
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Rolled-back library failed SQLite integrity check")
            connection.close()
            connection = None
        rollback_intent_path(database).unlink()
        sync_directory(database.parent)
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    finally:
        staging.unlink(missing_ok=True)

"""
Database/storage maintenance helpers for auditing and aggressive pruning.

This module owns backup/retention, the orphan-deletion primitives and the
prune driver, and is the single import surface for maintenance: the path,
audit and reconciliation halves live in ``maintenance_paths``,
``maintenance_audit`` and ``maintenance_reconcile`` and are re-exported here.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile, rmtree
import json
import logging
import math
import os
import re
import sqlite3
import stat
import tempfile
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

import psutil

from modules.contracts.book_identity import candidate_book_title_with_suffix, sanitize_book_title
from modules.contracts import durable_files
from modules.contracts.durable_files import atomic_write_json, sync_directory, sync_file
from modules.storage.deletion import (
    DELETE_MANIFEST,
    apply_delete_staging_decision,
    delete_staging_policy,
    mark_delete_transaction_committed,
    preserve_delete_transactions,
    read_delete_transaction,
    remove_unmanifested_staging,
    _retry_read_only_removal,
)
from modules.storage.maintenance_audit import (
    _newest_mtime,
    _path_comparison_key,
    _recency_filter,
    audit_storage_state,
    audit_storage_state_with_unowned,
)
from modules.storage.maintenance_paths import (
    APP_OWNED_DB_PARENT_NAMES,
    BUSY_TIMEOUT_MS,
    DEFAULT_CLEANUP_GRACE_SECONDS,
    RESERVED_OUTPUT_DIR_NAMES,
    _connect,
    canonical_sources_dir,
    is_app_owned_durable_source,
    path_is_within_root,
    resolve_app_owned_sources_root,
    resolve_stored_path,
    unique_destination_path,
    workspace_root_for_db,
)
from modules.storage.maintenance_reconcile import (
    AUDIO_ARTIFACT_SUFFIXES,
    DURABLE_PLAYBACK_SUFFIXES,
    _candidate_book_output_dirs,
    _dir_contains_finished_audio,
    _dir_contains_recovery_inventory,
    _dir_has_direct_audio,
    _durable_audio_file_in_dir,
    _find_ondisk_playback_artifact,
    SourceRepair,
    _apply_source_repair,
    _move_referenced_upload_sources,
    _plan_relocated_upload_repairs,
    _repair_relocated_upload_sources,
    _referenced_output_dir_keys,
    _referenced_output_dir_keys_of,
    _source_repair_still_valid,
    _unclaimed_finished_audio_dirs,
)
from modules.storage.permissions import ensure_owner_only_directory, owner_only
from modules.storage.migrations import LATEST_SCHEMA_VERSION, prepare_restore_identity, run_migrations
from modules.storage.managed_roots import claim_managed_roots, claim_sources_root
from modules.storage.restore_recovery import (
    pending_restore_safety_copies, record_restore_rollback, recover_restore_rollback,
)
from modules.storage.db_ownership import acquire_database, canonical_database_path, retire_database_file
from modules.storage.sqlite_uri import readonly_sqlite_uri

if TYPE_CHECKING:
    from modules.storage.repository import IBookRepository
    from modules.storage.sqlite_repo import SQLiteRepository

__all__ = [
    "APP_OWNED_DB_PARENT_NAMES",
    "AUDIO_ARTIFACT_SUFFIXES",
    "BUSY_TIMEOUT_MS",
    "DEFAULT_CLEANUP_GRACE_SECONDS",
    "DEFAULT_RETENTION_KEEP",
    "DURABLE_PLAYBACK_SUFFIXES",
    "RESERVED_OUTPUT_DIR_NAMES",
    "audit_storage_state",
    "audit_storage_state_with_unowned",
    "audit_with_unowned_folders",
    "backup_database",
    "list_database_backups",
    "restore_database_backup",
    "recover_restore_artifacts",
    "canonical_sources_dir",
    "is_app_owned_durable_source",
    "library_quarantined",
    "path_is_within_root",
    "prune_stale_records_and_files",
    "resolve_app_owned_sources_root",
    "resolve_stored_path",
    "storage_cleanup_summary",
    "unique_destination_path",
    "unowned_output_folders",
    "workspace_root_for_db",
    # Internal helpers kept importable from this module for callers that
    # reached for them before the split.
    "_candidate_book_output_dirs",
    "_connect",
    "_dir_contains_finished_audio",
    "_dir_has_direct_audio",
    "_durable_audio_file_in_dir",
    "_find_ondisk_playback_artifact",
    "_move_referenced_upload_sources",
    "_newest_mtime",
    "_recency_filter",
    "_referenced_output_dir_keys",
    "_unclaimed_finished_audio_dirs",
]


# Retention cap for our own generated artifacts (timestamped DB backups and
# prune report JSON). Without a cap these directories grow without bound over a
# library's lifetime. Backups double as prune's safety net, so keep a generous
# window rather than a tight one.
DEFAULT_RETENTION_KEEP = 30
logger = logging.getLogger(__name__)
try:
    _RESTORE_PROCESS_STARTED_AT = psutil.Process().create_time()
except psutil.Error:
    # Without a reliable age boundary, retain ambiguous staging files.
    _RESTORE_PROCESS_STARTED_AT = None


def recover_restore_artifacts(repository: IBookRepository) -> None:
    """Retire old private stages and status for an already published restore.

    Called after repository initialization, under database ownership. No live
    database is replaced here; a matching epoch only retires quarantine status.
    """
    from modules.storage.sqlite_repo import SQLiteRepository

    if not isinstance(repository, SQLiteRepository):
        return
    with repository._lock:
        if not repository._ownership.active:
            return
        db_path = repository.db_path
        staging_dir = db_path.parent / f".{db_path.name}.restore-staging"
        # Only the private directory is ours; legacy loose stages stay untouched.
        pattern = re.compile(re.escape(f".restore-{db_path.name}-") + r"[a-z0-9_]{8}"
                             + re.escape(db_path.suffix) + r"(?:-wal|-shm|-journal)?")
        removed = False
        if (_RESTORE_PROCESS_STARTED_AT is not None and staging_dir.is_dir()
                and not staging_dir.is_symlink() and not staging_dir.is_junction()):
            for path in staging_dir.iterdir():
                if not pattern.fullmatch(path.name):
                    continue
                try:
                    info = path.lstat()
                    if (
                        stat.S_ISREG(info.st_mode)
                        and max(info.st_mtime, info.st_ctime) < _RESTORE_PROCESS_STARTED_AT
                    ):
                        path.unlink()
                        removed = True
                except OSError:
                    logger.warning("Could not remove database restore staging file", exc_info=True)
        if removed:
            sync_directory(staging_dir)
        _remove_interrupted_copies(db_path)
        status = repository.quarantine
        if (
            isinstance(status, dict)
            and status.get("restored_library_epoch") == repository.get_library_identity()["library_epoch"]
            and [row[0] for row in repository._conn.execute("PRAGMA integrity_check")] == ["ok"]
        ):
            _remove_quarantine_status(db_path)


def _remove_interrupted_copies(db_path: Path) -> None:
    """Remove backup and safety copies an earlier process never finished."""
    directory = db_path.parent / "backups"
    if (_RESTORE_PROCESS_STARTED_AT is None or not directory.is_dir()
            or directory.is_symlink() or directory.is_junction()):
        return
    pattern = re.compile(re.escape(f".{db_path.stem}_") + r".+" + re.escape(db_path.suffix + _COPY_TEMPORARY_SUFFIX)
                         + r"(?:-wal|-shm|-journal)?")
    removed = False
    for path in directory.iterdir():
        if not pattern.fullmatch(path.name):
            continue
        try:
            info = path.lstat()
            # Newer files can belong to a copy this process is writing now.
            if stat.S_ISREG(info.st_mode) and max(info.st_mtime, info.st_ctime) < _RESTORE_PROCESS_STARTED_AT:
                path.unlink()
                removed = True
        except OSError:
            logger.warning("Could not remove incomplete database copy %s", path, exc_info=True)
    if removed:
        sync_directory(directory)


def _remove_quarantine_status(db_path: Path) -> None:
    try:
        db_path.with_name(db_path.name + ".quarantine.json").unlink()
        sync_directory(db_path.parent)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove database quarantine status after restore", exc_info=True)


def _enforce_retention(
    directory: Path, glob_pattern: str, keep: int | None = None, *, databases: bool = False,
) -> list[str]:
    """Keep only the ``keep`` newest regular files matching ``glob_pattern``.

    ``keep`` defaults to :data:`DEFAULT_RETENTION_KEEP`, read at call time so the
    cap stays configurable. Only plain files matching the pattern are ever
    removed; symlinks, subdirectories and unrelated files are never touched.
    Best-effort — any deletion error is swallowed so retention can never break the
    caller.
    """
    if keep is None:
        keep = DEFAULT_RETENTION_KEEP
    if keep <= 0 or not directory.is_dir():
        return []
    try:
        protected = pending_restore_safety_copies(directory) if databases else set()
        if databases:
            for lockfile in directory.glob(glob_pattern + ".lock"):
                database = lockfile.with_suffix("")
                if not lockfile.is_symlink() and not database.exists():
                    try:
                        retire_database_file(database)
                    except OSError:
                        pass
        matches = [p for p in directory.glob(glob_pattern) if p.is_file() and not p.is_symlink()]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if len(matches) <= keep:
        return []
    timestamped_matches: list[tuple[float, Path]] = []
    for match in matches:
        try:
            timestamped_matches.append((match.stat().st_mtime, match))
        except OSError:
            # A concurrent delete, permission change, or filesystem race should
            # preserve the ambiguous entry and must never fail backup/prune.
            continue
    timestamped_matches.sort(key=lambda item: item[0], reverse=True)
    removed: list[str] = []
    for _, stale in timestamped_matches[keep:]:
        if stale in protected:
            continue
        try:
            if databases:
                if not retire_database_file(stale):
                    continue
            else:
                stale.unlink()
            removed.append(str(stale))
        except OSError:
            continue
    return removed


def backup_database(
    db_path: Path | str,
    backup_dir: Path | str | None = None,
) -> Path:
    """
    Create timestamped SQLite backup and return backup file path.
    """
    source = canonical_database_path(db_path)
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    target_dir = Path(backup_dir) if backup_dir else source.parent / "backups"
    ensure_owner_only_directory(target_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_conn = _connect(source)
    try:
        target = _write_database_copy(
            source_conn, lambda: unique_destination_path(target_dir / f"{source.stem}_{stamp}{source.suffix}"),
            lockfile=True,
        )
    finally:
        source_conn.close()
    # Cap the backups directory so it does not grow without bound; the just-made
    # backup is newest and always retained.
    _enforce_retention(target_dir, f"{source.stem}_*{source.suffix}", databases=True)
    return target


# Copies are written under a hidden name that listing, restore and retention
# ignore, then renamed into place complete.
_COPY_TEMPORARY_SUFFIX = ".tmp"


def _write_database_copy(source_conn: sqlite3.Connection, destination, *, lockfile: bool = False) -> Path:
    """Copy ``source_conn`` to the path ``destination()`` names, all or nothing.

    ``lockfile`` leaves the copy's ``.lock`` file, which retention takes to
    prove nothing still has a backup open before retiring it.
    """
    first = destination()
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{first.stem}.", suffix=first.suffix + _COPY_TEMPORARY_SUFFIX, dir=first.parent,
    )
    os.close(descriptor)
    temporary = Path(raw)
    try:
        target_conn = sqlite3.connect(temporary)
        try:
            source_conn.backup(target_conn)
            # A backup is a single self-contained file: leave rollback-journal mode
            # so closing it does not keep WAL sidecars beside it.
            target_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_conn.close()
        _remove_backup_sidecars(temporary)
        durable_files.sync_file(temporary)
        # The name is chosen again in case another copy took it meanwhile.
        target = destination()
        os.replace(temporary, target)
    except BaseException:
        _remove_database_files(temporary)
        raise
    sync_directory(target.parent)
    # mkstemp already created it owner-only; this also covers older umasks.
    owner_only(target)
    if lockfile:
        acquire_database(target).close()
    return target


def _remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove incomplete database copy %s", path, exc_info=True)


def _remove_backup_sidecars(backup: Path) -> None:
    """Remove WAL files a finished backup left behind.

    Retention takes the backup's ``.lock`` file to prove nothing still has the
    backup open before retiring it. Restore reads the backup without it and
    relies on the controller's storage-maintenance reservation, which keeps
    backups, cleanup and restore from overlapping.
    """
    for suffix in ("-wal", "-shm"):
        sidecar = backup.with_name(backup.name + suffix)
        try:
            if sidecar.is_file() and not sidecar.is_symlink():
                sidecar.unlink()
        except OSError:
            logger.debug("Could not remove backup sidecar %s", sidecar, exc_info=True)


def _backup_label(path: Path, created: datetime, book_count: int | None) -> str:
    kind = "Safety copy" if "_safety_" in path.stem else "Backup"
    when = created.astimezone().strftime("%Y-%m-%d %H:%M")
    if book_count is None:
        return f"{kind} from {when}"
    return f"{kind} from {when} · {book_count} book{'s' if book_count != 1 else ''}"


def _backup_path(db_path: Path, backup_id: str) -> Path:
    directory = db_path.parent / "backups"
    if (
        not backup_id or backup_id in {".", ".."}
        or "/" in backup_id or "\\" in backup_id
        or directory.is_symlink() or directory.is_junction()
    ):
        raise FileNotFoundError("Database backup not found")
    path = directory / backup_id
    if (
        path.suffix != db_path.suffix or path.is_symlink() or path.is_junction()
        or not path.is_file() or path.resolve().parent != directory.resolve()
    ):
        raise FileNotFoundError("Database backup not found")
    return path


# Backups are written once and renamed into place, so a file with the same
# identity, size and modification time still has the same book count.
_backup_book_counts: dict[tuple[str, int, int, int, int], int | None] = {}
_backup_book_counts_lock = threading.Lock()


def _backup_book_count(path: Path, info: os.stat_result) -> int | None:
    key = (str(path.resolve()), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with _backup_book_counts_lock:
        if key in _backup_book_counts:
            return _backup_book_counts[key]
    book_count = None
    try:
        with closing(sqlite3.connect(readonly_sqlite_uri(path.resolve()), uri=True)) as conn:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            book_count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    except sqlite3.OperationalError:
        # Busy or unreadable now; try again on the next listing.
        return None
    except sqlite3.DatabaseError:
        pass
    with _backup_book_counts_lock:
        if len(_backup_book_counts) >= 4 * DEFAULT_RETENTION_KEEP:
            _backup_book_counts.clear()
        _backup_book_counts[key] = book_count
    return book_count


def list_database_backups(db_path: Path | str) -> list[dict[str, Any]]:
    source = canonical_database_path(db_path)
    directory = source.parent / "backups"
    if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
        return []
    items = []
    for candidate in directory.iterdir():
        try:
            path = _backup_path(source, candidate.name)
            stat = path.stat()
        except (FileNotFoundError, OSError):
            continue
        book_count = _backup_book_count(path, stat)
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        items.append({
            "id": path.name, "filename": path.name,
            "created_at": created.isoformat(),
            "size": stat.st_size, "book_count": book_count,
            # Display text, so the UI never needs to show the raw filename.
            "kind": "safety_copy" if "_safety_" in path.stem else "backup",
            "label": _backup_label(path, created, book_count),
        })
    return sorted(items, key=lambda item: item["created_at"], reverse=True)


def _restore_path_remappings(
    connection: sqlite3.Connection, database: Path, roots: tuple[Path, ...],
) -> list[tuple[Path, Path]]:
    from modules.storage.managed_roots import stored_path_remappings
    from modules.storage.migrations import MANAGED_ROOT_KEYS

    rows = connection.execute("SELECT value FROM library_metadata WHERE key='managed_roots'").fetchall()
    if not rows:
        return stored_path_remappings(database, *roots)
    try:
        if len(rows) != 1:
            raise ValueError("Ambiguous managed roots")
        layout = json.loads(rows[0][0])
        if (not isinstance(layout, dict) or set(layout) != set(MANAGED_ROOT_KEYS)
                or any(not isinstance(raw, str) or not Path(raw).is_absolute() for raw in layout.values())):
            raise ValueError("Invalid managed roots")
    except (ValueError, TypeError) as error:
        raise ValueError("Backup has an invalid managed storage layout") from error
    # Historical roots can now contain unrelated external files.
    return [(Path(layout[key]), root) for key, root in zip(MANAGED_ROOT_KEYS, roots, strict=True)]


def _validate_restore_schema(connection: sqlite3.Connection) -> None:
    """Verify constraints before trusting a backup's foreign_key_check result."""
    for table in ("books", "chapters"):
        primary = [row for row in connection.execute(f"PRAGMA table_info({table})") if row["pk"]]
        if (
            len(primary) != 1 or primary[0]["name"].lower() != "id"
            or primary[0]["type"].upper() != "INTEGER"
            # A separate PK index denotes a non-rowid key (including INTEGER
            # PRIMARY KEY DESC); inserts rely on SQLite assigning the id.
            or any(row["origin"] == "pk" for row in connection.execute(f"PRAGMA index_list({table})"))
        ):
            raise ValueError("Backup has an incompatible library schema: invalid record primary key")

    foreign_keys = list(connection.execute("PRAGMA foreign_key_list(chapters)"))
    if not any(
        row["table"].lower() == "books" and row["from"].lower() == "book_id"
        and (row["to"] is None or row["to"].lower() == "id")
        and row["on_delete"].upper() == "CASCADE"
        and sum(other["id"] == row["id"] for other in foreign_keys) == 1
        for row in foreign_keys
    ):
        raise ValueError("Backup has an incompatible library schema: missing cascading book relationship")

    for index in connection.execute("PRAGMA index_list(chapters)"):
        if not index["unique"] or index["partial"]:
            continue
        columns = [
            row["name"].lower() if row["name"] is not None else None
            for row in connection.execute("SELECT name FROM pragma_index_info(?)", (index["name"],))
        ]
        if len(columns) == 2 and set(columns) == {"book_id", "chapter_number"}:
            return
    raise ValueError("Backup has an incompatible library schema: missing unique chapter numbers")


def _validate_restore_records(connection: sqlite3.Connection, repository: SQLiteRepository) -> None:
    """Reject unusable values that SQLite affinity and dataclasses accept."""
    def text(value, *, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, str):
            raise ValueError("Expected a text value")

    def number(value, *, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Expected a finite numeric value")

    try:
        for row in connection.execute("SELECT * FROM books"):
            book = repository._row_to_book(row, strict=True)
            for field in ("title", "source_path"):
                text(getattr(book, field))
            for field in ("author", "playback_path", "conversion_runtime_options", "tags"):
                text(getattr(book, field), nullable=True)
            number(book.playback_position_sec)
            # Historical schemas permit missing chapter totals and timings.
            number(book.total_chapters, nullable=True)
        for row in connection.execute("SELECT * FROM chapters"):
            text(row["title"], nullable=True)
            text(row["mp3_path"], nullable=True)
            number(row["chapter_number"])
            number(row["duration_ms"], nullable=True)
            number(row["packaged_start_ms"], nullable=True)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("Backup contains unreadable library records") from error


def restore_database_backup(repository: SQLiteRepository, backup_id: str) -> Path:
    """Validate a snapshot, preserve the live database, and reopen it in place.

    The caller must reserve storage maintenance before entering this operation.
    The repository lock drains reads and writes from every thread sharing its
    single connection before checkpointing and closing it.
    """
    from uuid import uuid4

    from modules.storage.sqlite_repo import _normalize_search

    db_path = repository.db_path
    recover_restore_artifacts(repository)
    backup = _backup_path(db_path, backup_id)
    staging_dir = db_path.parent / f".{db_path.name}.restore-staging"
    if staging_dir.is_symlink() or staging_dir.is_junction():
        raise ValueError("Restore staging directory must not be a link")
    ensure_owner_only_directory(staging_dir)
    descriptor, temporary = tempfile.mkstemp(prefix=f".restore-{db_path.name}-", suffix=db_path.suffix, dir=staging_dir)
    os.close(descriptor)
    staging = Path(temporary)
    restored_epoch = uuid4().hex

    def open_database(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.create_function("normalize_search", 1, _normalize_search, deterministic=True)
        try:
            run_migrations(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        return conn

    try:
        try:
            source_conn = sqlite3.connect(readonly_sqlite_uri(backup.resolve()), uri=True)
            try:
                # Orpheus defines no triggers. Reject executable snapshot schema
                # before identity repair or migrations can activate its writes.
                if source_conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
                    raise ValueError("Backup contains unsupported database triggers")
                if source_conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup failed SQLite integrity check")
                tables = {row[0] for row in source_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"books", "chapters"}.issubset(tables):
                    raise ValueError("Backup is not an Orpheus library database")
                if "schema_migrations" in tables:
                    version = source_conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
                    if version > LATEST_SCHEMA_VERSION:
                        raise ValueError("Backup was created by a newer version of Orpheus")
                target_conn = sqlite3.connect(staging)
                try:
                    source_conn.backup(target_conn)
                finally:
                    target_conn.close()
            finally:
                source_conn.close()
            with closing(sqlite3.connect(staging)) as identity_conn, identity_conn:
                # Another writer can change the source between validation and
                # backup. Validate the private copy before its first mutation.
                if identity_conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
                    raise ValueError("Backup contains unsupported database triggers")
                prepare_restore_identity(
                    identity_conn, restored_epoch, repository.get_library_identity().get("library_id"),
                )
            migrated = open_database(staging)
            try:
                migrated.execute("SELECT id, title, author, source_path, source_type, total_chapters, playback_path, playback_status, playback_position_sec, created_at, updated_at, conversion_mode, conversion_runtime_options, tags, favorite FROM books LIMIT 0")
                migrated.execute("SELECT id, book_id, chapter_number, title, duration_ms, mp3_path, packaged_start_ms FROM chapters LIMIT 0")
                _validate_restore_schema(migrated)
                # integrity_check does not inspect foreign keys. Validate the
                # migrated copy before closing or replacing the usable library.
                if migrated.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Backup contains invalid library relationships")
                # SQLite's structural checks do not validate values decoded by
                # library reads (for example ISO timestamps or numeric progress).
                _validate_restore_records(migrated, repository)
                migrated.execute("PRAGMA synchronous=FULL")
                prepare_restore_identity(migrated, restored_epoch, repository.get_library_identity().get("library_id"))
                from modules.storage.migrations import remap_stored_paths, store_managed_roots

                roots = (canonical_sources_dir(db_path), repository._output_dir, repository._temp_dir)
                remap_stored_paths(migrated, _restore_path_remappings(migrated, db_path, roots))
                store_managed_roots(migrated, roots)
                migrated.commit()
                migrated.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                migrated.execute("PRAGMA journal_mode=DELETE")
            finally:
                migrated.close()
        except sqlite3.DatabaseError as error:
            raise ValueError("Backup is corrupt or has an incompatible library schema") from error

        with repository._lock:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safety = _write_database_copy(
                repository._conn,
                lambda: unique_destination_path(backup.parent / f"{db_path.stem}_safety_{stamp}{db_path.suffix}"),
            )
            conflict_message = (
                "Cannot restore while database files are in use. Close other library connections "
                f"or applications and retry. The current library safety copy is at {safety}."
            )
            try:
                checkpoint = repository._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] != 0:
                    raise RuntimeError(conflict_message)
                mode = repository._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if mode.lower() != "delete":
                    raise RuntimeError(conflict_message)
            except sqlite3.OperationalError as error:
                raise RuntimeError(conflict_message) from error
            preserve_delete_transactions(repository._temp_dir / "delete_staging")
            status = repository.quarantine
            if isinstance(status, dict):
                # Record the target epoch before publication. A restart can
                # distinguish a completed restore from an unpublished/rolled
                # back restore without changing the rollback decision.
                atomic_write_json(
                    db_path.with_name(db_path.name + ".quarantine.json"),
                    {**status, "restored_library_epoch": restored_epoch},
                    permissions=0o600, sync_parent=sync_directory,
                )
            repository._conn.close()
            replaced = False
            try:
                # The restored file is in rollback-journal mode, so SQLite would
                # replay a journal left by the replaced database into it.
                for suffix in ("-wal", "-shm", "-journal"):
                    db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
                sync_file(staging)
                staging.replace(db_path)
                replaced = True
                sync_directory(db_path.parent)
                repository._conn = open_database(db_path)
            except Exception as error:
                if replaced:
                    repository._conn.close()
                    record_restore_rollback(db_path, safety)
                    repository._conn = recover_restore_rollback(db_path, copy=copyfile, reopen=open_database)
                else:
                    repository._conn = open_database(db_path)
                if isinstance(error, OSError):
                    raise RuntimeError(conflict_message) from error
                raise
            repository.mark_database_replaced()
            try:
                repository._claim_storage(repository.get_library_identity()["library_id"])
            except Exception:
                # Reopening must refresh these markers before another move can preserve ownership.
                logger.warning("Could not refresh storage ownership markers after database restore", exc_info=True)
            try:
                _repair_relocated_upload_sources(
                    repository._conn, db_path, workspace_root_for_db(db_path),
                    repository._output_dir, repository._temp_dir, canonical_sources_dir(db_path),
                )
                repository._conn.commit()
            except Exception:
                repository._conn.rollback()
                logger.warning("Could not repair source paths after database restore", exc_info=True)
            _remove_quarantine_status(db_path)
            return safety
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                staging.with_name(staging.name + suffix).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove database restore staging file", exc_info=True)


def unowned_output_folders(
    db_path: Path | str, output_dir: Path | str, temp_dir: Path | str, *, audit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Describe output folders that no library row owns; nothing is removed.

    Without a grace period, so a folder left by a restore or a lost row is
    reported at once; ``has_audio`` marks finished audio a user may want back.
    """
    if audit is None:
        audit = audit_storage_state(db_path, output_dir=output_dir, temp_dir=temp_dir, grace_seconds=0)
    folders = []
    for raw in audit["orphan_output_dirs"]:
        path = Path(raw)
        folders.append({
            "name": path.name,
            "path": str(path),
            "has_audio": _dir_contains_finished_audio(path),
            "has_recovery_inventory": _dir_contains_recovery_inventory(path),
        })
    return folders


def audit_with_unowned_folders(
    db_path: Path | str, output_dir: Path | str, temp_dir: Path | str,
    *, grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The graced audit and :func:`unowned_output_folders` from one audit pass."""
    audit, unowned_dirs = audit_storage_state_with_unowned(
        db_path, output_dir=output_dir, temp_dir=temp_dir, grace_seconds=grace_seconds,
    )
    return audit, unowned_output_folders(
        db_path, output_dir, temp_dir, audit={"orphan_output_dirs": unowned_dirs},
    )


def _book_titles(db_path: Path | str, book_ids: list[int]) -> dict[int, str]:
    if not book_ids:
        return {}
    conn = _connect(canonical_database_path(db_path), read_only=True)
    try:
        placeholders = ",".join("?" * len(book_ids))
        return {int(row[0]): row[1] for row in conn.execute(
            f"SELECT id, title FROM books WHERE id IN ({placeholders})", book_ids,
        )}
    finally:
        conn.close()


def _folder_may_hold_book(folder_name: str, title: str | None) -> bool:
    """Whether an output folder carries the name conversion gives this title.

    Conversion names a book's folder after its sanitized title and appends
    `` (N)`` when that name is taken.
    """
    if not title or not title.strip():
        return False
    safe_title = sanitize_book_title(title)
    numbered = re.search(r" \(\d+\)$", folder_name)
    expected = candidate_book_title_with_suffix(safe_title, numbered.group()) if numbered else safe_title
    return os.path.normcase(folder_name) == os.path.normcase(expected)


# After a corrupt database is quarantined, the empty replacement references
# none of the damaged library's files, so until a backup is restored these
# unreferenced entries may be the only copy of that library's sources. Every
# automatic sweep of them must check ``library_quarantined`` first.
_QUARANTINE_KEPT = ("orphan_durable_sources", "orphan_output_dirs", "stale_temp_uploads")


def library_quarantined(db_path: Path | str) -> bool:
    db_file = canonical_database_path(db_path)
    return db_file.with_name(db_file.name + ".quarantine.json").exists()


def storage_cleanup_summary(
    db_path: Path | str,
    output_dir: Path | str,
    temp_dir: Path | str,
    audit: dict[str, Any],
    *,
    actions: dict[str, Any] | None = None,
    changed: bool | None = None,
    grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
    unowned: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per category, what cleanup removes now and what it leaves, with the reason.

    ``audit`` is the graced audit cleanup acts on, so ``removable`` and
    ``removable_books`` are exactly what an explicit cleanup would remove.
    Without ``actions`` (diagnostics) this is a preview; after a cleanup it
    also reports what was removed, naming the books in ``removed_books``.
    ``unowned`` is :func:`unowned_output_folders` when the caller already has it.
    """
    kept: list[dict[str, Any]] = []
    # A book with no audio and nothing to rebuild it from loses its library
    # entry in a cleanup, so it is reported as removable and never as kept.
    removable_books = _books_cleanup_would_remove(db_path, output_dir, temp_dir, audit)
    removable_book_ids = {book["id"] for book in removable_books}
    missing_sources = [
        row for row in audit.get("stale_book_sources", []) if int(row["id"]) not in removable_book_ids
    ]
    if missing_sources:
        kept.append({
            "category": "missing_source", "reason": "needs_user_action",
            "items": [{"id": int(row["id"]), "title": row["title"]} for row in missing_sources],
        })
    chapters_by_book: dict[int, int] = {}
    for row in audit.get("stale_chapter_rows", []):
        if int(row["book_id"]) in removable_book_ids:
            continue
        chapters_by_book[int(row["book_id"])] = chapters_by_book.get(int(row["book_id"]), 0) + 1
    titles = _book_titles(db_path, sorted({*chapters_by_book, *((actions or {}).get("preserved_book_rows") or [])}))
    if chapters_by_book:
        kept.append({
            "category": "missing_chapter_audio", "reason": "book_kept",
            "items": [{"id": book_id, "title": titles.get(book_id), "chapters": count}
                      for book_id, count in sorted(chapters_by_book.items())],
        })
    # A row kept only because unclaimed audio exists is explained by the folders
    # still on disk now: one named for the book may be its audio, any other only
    # blocks the removal, and once they are gone the explanation goes with them.
    held_for_audio = set((actions or {}).get("book_rows_kept_for_unclaimed_audio") or [])
    unclaimed_names = [
        Path(raw).name for raw in (actions or {}).get("unclaimed_audio_dirs") or [] if Path(raw).is_dir()
    ]
    books_by_reason: dict[str, list[dict[str, Any]]] = {}
    for book_id in (actions or {}).get("preserved_book_rows") or []:
        if book_id not in held_for_audio:
            reason = "interrupted_conversion"
        elif not unclaimed_names:
            continue
        elif any(_folder_may_hold_book(name, titles.get(book_id)) for name in unclaimed_names):
            reason = "unowned_audio_present"
        else:
            reason = "other_audio_present"
        books_by_reason.setdefault(reason, []).append({"id": book_id, "title": titles.get(book_id)})
    for reason, items in books_by_reason.items():
        kept.append({"category": "book_without_audio", "reason": reason, "items": items})
    graced_keys = {_path_comparison_key(raw) for raw in audit.get("orphan_output_dirs", [])}
    folders_by_reason: dict[str, list[dict[str, Any]]] = {}
    if unowned is None:
        unowned = unowned_output_folders(db_path, output_dir, temp_dir)
    for folder in unowned:
        if folder["has_audio"]:
            reason = "contains_finished_audio"
        elif folder["has_recovery_inventory"]:
            reason = "interrupted_conversion"
        elif _path_comparison_key(folder["path"]) not in graced_keys:
            reason = "recently_modified"
        else:
            continue  # cleanup removes it; counted under ``removable``
        folders_by_reason.setdefault(reason, []).append({"name": folder["name"], "path": folder["path"]})
    for reason, items in folders_by_reason.items():
        kept.append({"category": "unowned_output_folder", "reason": reason, "items": items})

    removable_folders = [
        raw for raw in audit.get("orphan_output_dirs", [])
        if not _dir_contains_finished_audio(Path(raw)) and not _dir_contains_recovery_inventory(Path(raw))
    ]
    if library_quarantined(db_path):
        held = [*audit.get("orphan_durable_sources", []), *removable_folders, *audit.get("stale_temp_uploads", [])]
        if held:
            kept.append({
                "category": "quarantined_library_files", "reason": "library_quarantined",
                "items": [{"name": Path(raw).name, "path": str(raw)} for raw in held],
            })
        audit = {**audit, **dict.fromkeys(_QUARANTINE_KEPT, [])}
        removable_folders = []
    summary: dict[str, Any] = {
        "changed": changed,
        "grace_hours": round(grace_seconds / 3600, 2),
        "removable": {
            "output_folders": len(removable_folders),
            "temp_uploads": len(audit.get("stale_temp_uploads", [])),
            "temp_stage_folders": len(audit.get("stale_temp_stage_dirs", [])),
            "pending_deletions": len(audit.get("orphan_delete_staging_dirs", [])),
            "orphan_sources": len(audit.get("orphan_durable_sources", [])),
        },
        "removable_books": removable_books,
        "kept": kept,
    }
    if actions is not None:
        summary["removed_books"] = list(actions.get("deleted_books", []))
        summary["removed"] = {
            "book_rows": len(actions.get("deleted_book_rows", [])),
            "chapter_rows": len(actions.get("deleted_chapter_rows", [])),
            "output_folders": len(actions.get("deleted_output_dirs", [])),
            "temp_uploads": len(actions.get("deleted_temp_uploads", [])),
            "temp_stage_folders": len(actions.get("deleted_temp_stage_dirs", [])),
            "pending_deletions": len(actions.get("deleted_delete_staging_dirs", [])),
            "orphan_sources": len(actions.get("deleted_durable_sources", [])),
            "moved_sources": len(actions.get("moved_sources", [])),
            "reconciled_books": len(actions.get("reconciled_playback", [])),
        }
    return summary


def _delete_directory_and_verify(path: Path) -> None:
    rmtree(path, onexc=_retry_read_only_removal)
    if path.exists():
        raise OSError(f"Failed to delete directory: {path}")


def _delete_file_and_verify(path: Path) -> None:
    from modules.contracts.durable_files import unlink_file

    unlink_file(path)
    if path.exists():
        raise OSError(f"Failed to delete file: {path}")


def _delete_symlink_and_verify(path: Path) -> None:
    """Remove a symlink itself, never following it to its (possibly external)
    target. ``Path.unlink`` deletes the link entry, so a symlink that an audit
    classified as an orphan is cleaned up without touching whatever it points
    at outside the managed roots."""
    path.unlink()
    if path.is_symlink() or path.exists():
        raise OSError(f"Failed to delete symlink: {path}")


def _is_within_root_no_follow(path: Path, root: Path) -> bool:
    """Lexical containment check that never dereferences symlinks.

    Both operands originate from the managed roots (audit lists only direct
    children of the resolved roots), so a plain ``relative_to`` is a sufficient
    guard against deleting anything outside the managed storage without
    resolving — resolving would follow a symlinked entry to its real target."""
    try:
        Path(path).expanduser().relative_to(Path(root).expanduser())
    except ValueError:
        return False
    return True


def _delete_orphan_entry(path: Path, root: Path, *, expect_dir: bool) -> bool:
    """Delete an orphan artifact under ``root`` safely; return whether removed.

    A symlink is unlinked (the link only, never its target). A real entry is
    verified to live under ``root`` without dereferencing before removal, so a
    symlinked or traversing path can never redirect deletion outside managed
    storage.
    """
    if path.is_symlink():
        _delete_symlink_and_verify(path)
        return True
    if path.is_junction():
        # A junction is a directory link that is_symlink() does not report;
        # removing it never touches the target, unlike rmtree, which refuses it.
        path.rmdir()
        if path.is_junction() or path.exists():
            raise OSError(f"Failed to delete junction: {path}")
        return True
    if not _is_within_root_no_follow(path, root):
        return False
    if expect_dir:
        if path.exists() and path.is_dir():
            _delete_directory_and_verify(path)
            return True
    elif path.exists() and path.is_file():
        _delete_file_and_verify(path)
        return True
    return False


class _PruneRows(NamedTuple):
    """The library rows cleanup decides on.

    ``books`` are ``(id, title, source_path, playback_path)``; ``chapters`` are
    ``(book_id, chapter_number, mp3_path)`` for recorded chapter audio, in
    chapter order.
    """

    books: tuple[tuple[Any, ...], ...]
    chapters: tuple[tuple[Any, ...], ...]


def _read_prune_rows(conn: sqlite3.Connection) -> _PruneRows:
    return _PruneRows(
        tuple(tuple(row) for row in conn.execute(
            "SELECT id, title, source_path, playback_path FROM books ORDER BY id"
        ).fetchall()),
        tuple(tuple(row) for row in conn.execute(
            """
            SELECT book_id, chapter_number, mp3_path FROM chapters
            WHERE mp3_path IS NOT NULL AND mp3_path != ''
            ORDER BY book_id, chapter_number, id
            """
        ).fetchall()),
    )


def _snapshot_prune_rows(db_file: Path) -> _PruneRows:
    """One consistent read of the rows, without holding any lock afterwards."""
    with closing(_connect(db_file, read_only=True)) as conn:
        conn.execute("BEGIN")
        try:
            return _read_prune_rows(conn)
        finally:
            conn.rollback()


def _chapter_paths_by_book(rows: _PruneRows) -> dict[int, list[str]]:
    paths: dict[int, list[str]] = {}
    for book_id, _number, mp3_path in rows.chapters:
        paths.setdefault(int(book_id), []).append(mp3_path)
    return paths


def _recorded_or_ondisk_audio(
    title: str | None, playback_raw: str | None, chapter_raws: list[str],
    workspace_root: Path, output_root: Path, temp_root: Path,
) -> bool | tuple[Path, str]:
    """``True`` when the recorded audio exists, else an on-disk artifact to relink, else ``False``."""
    playback_path = resolve_stored_path(
        playback_raw, workspace_root, output_root=output_root, temp_root=temp_root,
    )
    if playback_path and playback_path.exists() and playback_path.is_file():
        return True
    for chapter_raw in chapter_raws:
        chapter_path = resolve_stored_path(
            chapter_raw, workspace_root, output_root=output_root, temp_root=temp_root,
        )
        if chapter_path and chapter_path.exists():
            return True

    # The DB has no usable playback metadata, but a durable audiobook may
    # still exist on disk (e.g. a packaged .m4b whose playback_path was
    # cleared by a schema migration that never probes disk). Refuse to
    # delete such a book and reconcile its playback metadata so the row is
    # playable again and its output directory stays referenced (otherwise
    # the orphan-output-dir sweep would still rmtree the audio).
    #
    # The title-derived directory guess is only trusted for books that
    # carry no stored pointer at all (the genuine post-migration case). A
    # book whose stored playback/chapter pointer merely failed to resolve
    # (a rename, or a data-dir move that invalidated absolute paths) must
    # not be reconciled by title, or it can be cross-wired onto a
    # different book's audio.
    has_stored_pointer = bool((playback_raw or "").strip()) or bool(chapter_raws)
    artifact = _find_ondisk_playback_artifact(
        title=title,
        playback_raw=playback_raw,
        chapter_raw_paths=list(chapter_raws),
        workspace_root=workspace_root,
        output_root=output_root,
        temp_root=temp_root,
        allow_title_fallback=not has_stored_pointer,
    )
    return artifact if artifact is not None else False


def _plan_fileless_book_rows(
    rows: _PruneRows,
    recoverable_book_ids: set[int],
    workspace_root: Path,
    output_root: Path,
    temp_root: Path,
) -> dict[str, Any]:
    """Decide what cleanup does with each book that has no playable audio.

    Reads only the disk; nothing is written. ``reconciled_playback`` are the
    rows to relink to audio found on disk, and ``removable`` the rows cleanup
    deletes: no audio anywhere, and nothing left to rebuild it from.
    """
    settled: dict[str, Any] = {
        "reconciled_playback": [], "preserved": [], "held_for_unclaimed_audio": [],
        "unclaimed_audio_dirs": [], "removable": [],
    }
    chapter_paths = _chapter_paths_by_book(rows)
    # Playback paths as they stand once the relinks below are applied.
    playback_paths: list[str | None] = []
    for book_id, title, _source_path, playback_raw in rows.books:
        audio = _recorded_or_ondisk_audio(
            title, playback_raw, chapter_paths.get(int(book_id), []),
            workspace_root, output_root, temp_root,
        )
        if audio is True:
            playback_paths.append(playback_raw)
            continue
        if audio:
            artifact_path, playback_status = audio
            playback_paths.append(str(artifact_path))
            settled["reconciled_playback"].append(
                {
                    "book_id": int(book_id),
                    "playback_path": str(artifact_path),
                    "playback_status": playback_status,
                }
            )
            continue
        playback_paths.append(playback_raw)
        if int(book_id) in recoverable_book_ids:
            settled["preserved"].append(int(book_id))
            continue
        settled["removable"].append({"id": int(book_id), "title": title})

    # Safety gate before deleting any fileless row: if a finished-audio
    # directory exists on disk that no surviving book claims (counting the
    # relinks above), it may be the audio of a moved/renamed book we could
    # not link. We are then *not* confident these rows are truly orphaned, so
    # we keep them rather than destroy library metadata for a book whose
    # audio still exists. Genuine orphans (no unclaimed audio anywhere) are
    # still deleted.
    if settled["removable"]:
        unclaimed_audio = _unclaimed_finished_audio_dirs(
            output_root,
            _referenced_output_dir_keys_of(
                [*playback_paths, *(row[2] for row in rows.chapters)],
                workspace_root, output_root, temp_root,
            ),
        )
        if unclaimed_audio:
            settled["held_for_unclaimed_audio"] = [book["id"] for book in settled["removable"]]
            settled["unclaimed_audio_dirs"] = unclaimed_audio
            settled["removable"] = []
    return settled


def _relink_playback(conn: sqlite3.Connection, reconciled: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE books
        SET playback_path = ?,
            playback_status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reconciled["playback_path"], reconciled["playback_status"], reconciled["book_id"]),
    )


class _PrunePlan(NamedTuple):
    rows: _PruneRows
    protected_sources: set[Path]
    source_repairs: list[SourceRepair]
    settled: dict[str, Any]


def _plan_prune(
    rows: _PruneRows, db_file: Path, workspace_root: Path, output_root: Path, temp_root: Path,
    sources_dir: Path, recoverable_book_ids,
) -> _PrunePlan:
    """Everything cleanup decides about rows, from disk reads alone.

    Source repairs only change ``source_path``, which the settlement does not
    read, so both are decided on the same rows.
    """
    protected, recoverable, repairs = _plan_relocated_upload_repairs(
        [(book[0], book[2]) for book in rows.books],
        db_file, workspace_root, output_root, temp_root, sources_dir,
    )
    settled = _plan_fileless_book_rows(
        rows, {*recoverable_book_ids, *recoverable}, workspace_root, output_root, temp_root,
    )
    return _PrunePlan(rows, protected, repairs, settled)


def _apply_source_repairs(conn: sqlite3.Connection, repairs: list[SourceRepair]) -> None:
    """Apply planned repairs under the caller's write lock; a stale one is skipped."""
    if not repairs:
        return
    current = dict(conn.execute("SELECT id, source_path FROM books").fetchall())
    for repair in repairs:
        if repair.book_id in current and _source_repair_still_valid(repair, current[repair.book_id]):
            _apply_source_repair(conn, repair)


def _apply_prune_plan(
    conn: sqlite3.Connection, plan: _PrunePlan, db_file: Path, workspace_root: Path, output_root: Path,
    temp_root: Path, sources_dir: Path,
) -> dict[str, Any]:
    """Carry out ``plan`` under the caller's write lock, never on stale evidence.

    A row that changed since the plan was read keeps its old state: its
    repair or relink is skipped, and any change to the rows skips every
    deletion. Before a row is deleted, the evidence it depends on is read
    again here: the rows, its own audio, whether a copy can rebuild it, and
    whether unclaimed audio exists anywhere in the output folder. Only the
    hashing of copies and the scans that found nothing to delete stay
    outside the lock. The next cleanup decides skipped rows again.
    """
    current = _read_prune_rows(conn)
    _apply_source_repairs(conn, plan.source_repairs)
    planned_books = {int(book[0]): book for book in plan.rows.books}
    current_books = {int(book[0]): book for book in current.books}
    planned_chapters = _chapter_paths_by_book(plan.rows)
    current_chapters = _chapter_paths_by_book(current)

    def unchanged(book_id: int) -> bool:
        return (planned_books.get(book_id) == current_books.get(book_id)
                and planned_chapters.get(book_id) == current_chapters.get(book_id))

    settled = dict(plan.settled)
    relinked = []
    for reconciled in plan.settled["reconciled_playback"]:
        if unchanged(reconciled["book_id"]) and Path(reconciled["playback_path"]).is_file():
            _relink_playback(conn, reconciled)
            relinked.append(reconciled)
    settled["reconciled_playback"] = relinked
    removable = []
    if current == plan.rows and len(relinked) == len(plan.settled["reconciled_playback"]):
        candidates = [current_books[book["id"]] for book in plan.settled["removable"]]
        # A copy of a source that appeared since planning can rebuild its book.
        _protected, rebuildable, _repairs = _plan_relocated_upload_repairs(
            [(book[0], book[2]) for book in candidates],
            db_file, workspace_root, output_root, temp_root, sources_dir,
        ) if candidates else (set(), set(), [])
        for book_id, title, _source_path, playback_raw in candidates:
            if book_id in rebuildable:
                settled["preserved"] = [*settled["preserved"], int(book_id)]
            elif _recorded_or_ondisk_audio(
                title, playback_raw, current_chapters.get(book_id, []), workspace_root, output_root, temp_root,
            ) is False:
                removable.append({"id": int(book_id), "title": title})
    if removable:
        # The safety gate again, on the rows as they now stand (relinks
        # included) and the output folder as it is now.
        relinked_paths = {entry["book_id"]: entry["playback_path"] for entry in relinked}
        unclaimed_audio = _unclaimed_finished_audio_dirs(
            output_root,
            _referenced_output_dir_keys_of(
                [*(relinked_paths.get(int(book[0]), book[3]) for book in current.books),
                 *(row[2] for row in current.chapters)],
                workspace_root, output_root, temp_root,
            ),
        )
        if unclaimed_audio:
            settled["held_for_unclaimed_audio"] = [book["id"] for book in removable]
            settled["unclaimed_audio_dirs"] = unclaimed_audio
            removable = []
    settled["removable"] = removable
    return settled


def _books_cleanup_would_remove(
    db_path: Path | str, output_dir: Path | str, temp_dir: Path | str, audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """The books whose library entry a cleanup would delete now; nothing changes.

    Cleanup plans from the same rows with the same code before it takes the
    write lock, so a preview agrees with the cleanup that follows (unless the
    library changes in between) and never takes a write lock itself.
    """
    db_file = canonical_database_path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None
    return _plan_prune(
        _snapshot_prune_rows(db_file), db_file, workspace_root, output_root, temp_root,
        canonical_sources_dir(db_file), audit.get("recoverable_book_ids", []),
    ).settled["removable"]


def _finish_delete_staging(
    db_file: Path, entries: list[str], workspace_root: Path, output_root: Path, temp_root: Path,
    sources_dir: Path, grace_seconds: float,
) -> list[str]:
    """Settle stale deletion transactions with the policy the startup sweep uses."""
    if not entries:
        return []
    from time import time

    from modules.storage.db_ownership import acquire_database

    def stored_paths(raws) -> set[Path]:
        return {
            path for raw in raws
            if raw and (path := resolve_stored_path(
                raw, workspace_root, output_root=output_root, temp_root=temp_root,
            )) is not None
        }

    def make_durable(entry: Path) -> None:
        if not (entry / ".committed").is_file():
            # The row's deletion can still be only in the WAL; checkpoint it
            # before the staged files become unrecoverable.
            with closing(_connect(db_file)) as writer:
                busy, log_frames, checkpointed = writer.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            if busy or log_frames < 0 or log_frames != checkpointed:
                raise RuntimeError("Cannot make book deletion durable: checkpoint incomplete")
        mark_delete_transaction_committed(entry)

    cutoff = time() - max(grace_seconds, DEFAULT_CLEANUP_GRACE_SECONDS)
    finished = []
    with acquire_database(db_file) as ownership, closing(_connect(db_file, read_only=True)) as conn:
        for raw in entries:
            path = Path(raw)
            try:
                # The repository lock keeps every reference current until the staged bytes are settled.
                with ownership.lock:
                    if path.is_symlink() or path.is_junction() or not path.is_dir():
                        continue
                    references = stored_paths(row[0] for row in conn.execute(
                        "SELECT source_path FROM books UNION SELECT playback_path FROM books"
                        " UNION SELECT mp3_path FROM chapters"
                    ))
                    if (path / DELETE_MANIFEST).exists():
                        book_id, artifacts = read_delete_transaction(
                            path, allowed_roots=(output_root, temp_root, sources_dir),
                        )
                        book = conn.execute(
                            "SELECT source_path, playback_path FROM books WHERE id = ?", (book_id,),
                        ).fetchone()
                        book_paths = stored_paths([*book, *(row[0] for row in conn.execute(
                            "SELECT mp3_path FROM chapters WHERE book_id = ?", (book_id,),
                        ))]) if book is not None else set()
                        decision = delete_staging_policy(
                            path, artifacts, references, book_exists=book is not None,
                            book_has_paths=bool(book_paths),
                            database_replaced=library_quarantined(db_file),
                        )
                        done = apply_delete_staging_decision(path, artifacts, *decision, make_durable=make_durable)
                    else:
                        done = bool(_recency_filter([path], cutoff)) and remove_unmanifested_staging(
                            path, references, audio_suffixes=AUDIO_ARTIFACT_SUFFIXES,
                        )
                if done:
                    finished.append(str(path))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, sqlite3.Error):
                logger.warning("Failed to finish deletion transaction: %s", path, exc_info=True)
    return finished


def prune_stale_records_and_files(
    db_path: Path | str,
    output_dir: Path | str = "output",
    temp_dir: Path | str = "temp",
    aggressive: bool = False,
    grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    """
    Aggressively prune stale storage rows/artifacts and emit a JSON report.

    Filesystem artifacts modified within ``grace_seconds`` are preserved even
    when unreferenced; see :func:`audit_storage_state`.
    """
    if not aggressive:
        raise ValueError("Refusing prune operation without aggressive=True")

    db_file = canonical_database_path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None
    sources_dir = canonical_sources_dir(db_file)
    claim_sources_root(db_file, sources_dir)
    claim_managed_roots(db_file, output_root, temp_root)
    sources_dir.mkdir(parents=True, exist_ok=True)

    audit_before = audit_storage_state(
        db_file, output_dir=output_root, temp_dir=temp_root, grace_seconds=grace_seconds
    )
    backup_path = backup_database(db_file)

    actions: dict[str, Any] = {
        "deleted_chapter_rows": [],
        "deleted_book_rows": [],
        "deleted_books": [],
        "deleted_output_dirs": [],
        "deleted_temp_uploads": [],
        "deleted_temp_stage_dirs": [],
        "deleted_delete_staging_dirs": [],
        "deleted_durable_sources": [],
        "moved_sources": [],
        "reconciled_playback": [],
        "preserved_book_rows": [],
        "preserved_output_dirs": [],
        "unclaimed_audio_dirs": [],
        "book_rows_kept_for_unclaimed_audio": [],
    }

    actions["deleted_delete_staging_dirs"] = _finish_delete_staging(
        db_file, audit_before.get("orphan_delete_staging_dirs", []), workspace_root, output_root, temp_root,
        sources_dir, grace_seconds,
    )

    # Scanning and hashing happen before the write lock, so library writes
    # (playback saves, edits) are not held up by them; the lock covers SQL and
    # cheap re-checks that skip anything the plan no longer describes.
    plan = _plan_prune(
        _snapshot_prune_rows(db_file), db_file, workspace_root, output_root, temp_root, sources_dir,
        audit_before["recoverable_book_ids"],
    )
    protected_sources = set(plan.protected_sources)
    conn = _connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        # Delete books without any playable chapter or packaged playback file.
        fileless = _apply_prune_plan(
            conn, plan, db_file, workspace_root, output_root, temp_root, sources_dir,
        )
        actions["reconciled_playback"] = fileless["reconciled_playback"]
        actions["preserved_book_rows"] = fileless["preserved"] + fileless["held_for_unclaimed_audio"]
        actions["book_rows_kept_for_unclaimed_audio"] = fileless["held_for_unclaimed_audio"]
        actions["unclaimed_audio_dirs"] = fileless["unclaimed_audio_dirs"]
        books_to_delete = [book["id"] for book in fileless["removable"]]

        if books_to_delete:
            placeholders = ",".join(["?"] * len(books_to_delete))
            conn.execute(f"DELETE FROM books WHERE id IN ({placeholders})", books_to_delete)
            actions["deleted_book_rows"] = books_to_delete
            actions["deleted_books"] = fileless["removable"]
            actions["deleted_chapter_rows"] = [
                int(row["id"]) for row in audit_before["stale_chapter_rows"]
                if int(row["book_id"]) in books_to_delete
            ]

        # Unknown change counts keep the backup.
        row_changes = getattr(conn, "total_changes", 1)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    actions["moved_sources"] = _move_referenced_upload_sources(
        db_file=db_file,
        workspace_root=workspace_root,
        output_root=output_root,
        temp_root=temp_root,
        canonical_sources_dir=sources_dir,
    )

    protected_after_move, _, repairs = _plan_relocated_upload_repairs(
        [(book[0], book[2]) for book in _snapshot_prune_rows(db_file).books],
        db_file, workspace_root, output_root, temp_root, sources_dir,
    )
    protected_sources.update(protected_after_move)
    conn = _connect(db_file)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _apply_source_repairs(conn, repairs)
        row_changes += getattr(conn, "total_changes", 1)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Delete orphan output directories and stale temp artifacts.
    audit_mid = audit_storage_state(
        db_file, output_dir=output_root, temp_dir=temp_root, grace_seconds=grace_seconds
    )
    if library_quarantined(db_file):
        audit_mid = {**audit_mid, **dict.fromkeys(_QUARANTINE_KEPT, [])}
    for raw in audit_mid["orphan_output_dirs"]:
        path = Path(raw)
        # Never rmtree a real directory that still holds finished audio, even when
        # unreferenced: it may be a moved/renamed book's audiobook that lost its DB
        # linkage. Leaving a stale directory is infinitely preferable to destroying
        # a finished audiobook. Symlinked entries are not protected here — they are
        # unlinked (link only) by _delete_orphan_entry below.
        if _dir_contains_finished_audio(path) or _dir_contains_recovery_inventory(path):
            actions["preserved_output_dirs"].append(str(path))
            continue
        if _delete_orphan_entry(path, output_root, expect_dir=True):
            actions["deleted_output_dirs"].append(str(path))

    temp_upload_dir = temp_root / "web_uploads"
    for raw in audit_mid["stale_temp_uploads"]:
        path = Path(raw)
        if _delete_orphan_entry(path, temp_upload_dir, expect_dir=False):
            actions["deleted_temp_uploads"].append(str(path))

    for raw in audit_mid["stale_temp_stage_dirs"]:
        path = Path(raw)
        if _delete_orphan_entry(path, temp_root, expect_dir=True):
            actions["deleted_temp_stage_dirs"].append(str(path))

    protected_source_keys = {
        _path_comparison_key(source.resolve()) for source in protected_sources
    }
    for raw in audit_mid["orphan_durable_sources"]:
        path = Path(raw)
        if (path.name.startswith(".orpheus-")
                or _path_comparison_key(path.resolve()) in protected_source_keys):
            continue
        if _delete_orphan_entry(path, sources_dir, expect_dir=False):
            actions["deleted_durable_sources"].append(str(path))


    delete_staging_root = temp_root / "delete_staging"
    if delete_staging_root.exists() and delete_staging_root.is_dir():
        try:
            if not any(delete_staging_root.iterdir()):
                delete_staging_root.rmdir()
        except OSError:
            pass

    changed = bool(row_changes) or any(
        actions[key] for key in actions if key not in {
            "preserved_book_rows", "preserved_output_dirs", "unclaimed_audio_dirs", "book_rows_kept_for_unclaimed_audio",
        }
    )
    if not changed:
        # The backup only protects changes; keep the backup list meaningful.
        retire_database_file(backup_path)
    audit_after, unowned_after = audit_with_unowned_folders(
        db_file, output_root, temp_root, grace_seconds=grace_seconds,
    )
    report = {
        "timestamp": datetime.now().isoformat(),
        "database_path": str(db_file.resolve()),
        "backup_path": str(backup_path.resolve()) if changed else None,
        "aggressive": aggressive,
        "audit_before": audit_before,
        "audit_after": audit_after,
        "actions": actions,
        "summary": storage_cleanup_summary(
            db_file, output_root, temp_root, audit_after, actions=actions,
            changed=changed, grace_seconds=grace_seconds, unowned=unowned_after,
        ),
    }

    # Reports are app data, not audiobooks; older builds wrote them to
    # ``output/reports``, which stays reserved and is still left untouched.
    reports_dir = db_file.parent / "reports"
    ensure_owner_only_directory(reports_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"db_prune_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    owner_only(report_path)
    report["report_path"] = str(report_path.resolve())
    # Cap the reports directory; the report just written is newest and retained.
    _enforce_retention(reports_dir, "db_prune_*.json")
    return report

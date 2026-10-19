"""
Shared path resolution and connection helpers for storage maintenance.

Used by the audit, reconciliation and prune halves of
:mod:`modules.storage.maintenance`, which re-exports everything here.
"""

from __future__ import annotations

from contextlib import nullcontext
import errno
from pathlib import Path
import sqlite3
from typing import Optional

from modules.storage.db_ownership import acquire_database, canonical_database_path
from modules.storage.migrations import BUSY_TIMEOUT_MS
from modules.storage.deletion import LOCAL_DELETE_STAGING
from modules.storage.restore_recovery import rollback_intent_path
from modules.storage.sqlite_uri import readonly_sqlite_uri


class _OwnedConnection(sqlite3.Connection):
    def __init__(self, *args, ownership, **kwargs):
        self._ownership = ownership
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        super().close()
        if self._ownership is not None:
            self._ownership.close()

    def __del__(self):
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass


def _connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Retain process ownership until this maintenance connection closes.

    Creation remains available for backup destinations. A pending quarantine
    must be recovered by the repository before any maintenance connection opens.
    Read-only connections can proceed without ownership only when the lock file
    is absent and filesystem permissions prevent creating it.
    """
    path = canonical_database_path(db_path)
    try:
        ownership = acquire_database(path)
    except OSError as exc:
        if not read_only or isinstance(exc, BlockingIOError) or not (
            isinstance(exc, PermissionError) or exc.errno in (errno.EROFS, errno.EACCES)
        ):
            raise
        if Path(str(path) + ".lock").exists():
            raise
        ownership = None
    try:
        with ownership.lock if ownership is not None else nullcontext():
            if rollback_intent_path(path).exists() or rollback_intent_path(path).is_symlink():
                raise RuntimeError("Database restore rollback recovery is pending")
            if Path(str(path) + ".quarantine-intent.json").exists():
                raise RuntimeError("Database quarantine recovery is pending")
            conn = sqlite3.connect(
                readonly_sqlite_uri(path) if read_only else path,
                uri=read_only,
                factory=lambda *args, **kwargs: _OwnedConnection(*args, ownership=ownership, **kwargs),
            )
            try:
                conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            except BaseException:
                conn.close()
                raise
            return conn
    except BaseException:
        if ownership is not None:
            ownership.close()
        raise

# Output sub-directories owned by the maintenance tooling itself (e.g. prune
# report JSON). These must never be treated as orphan/stale artifacts, otherwise
# a later prune run would delete the prior run's own audit history.
RESERVED_OUTPUT_DIR_NAMES = frozenset({"reports", LOCAL_DELETE_STAGING})

# Artifacts newer than this are presumed to belong to queued or in-flight work
# (uploads awaiting conversion, freshly copied sources, output being written)
# that has no database reference yet, and are never classified stale/orphaned.
DEFAULT_CLEANUP_GRACE_SECONDS = 24 * 60 * 60

APP_OWNED_DB_PARENT_NAMES = ("data", "state")


def workspace_root_for_db(db_path: Path) -> Path:
    if db_path.parent.name in APP_OWNED_DB_PARENT_NAMES:
        return db_path.parent.parent.resolve()
    return db_path.parent.resolve()


def resolve_stored_path(
    raw: Optional[str],
    workspace_root: Path,
    *,
    output_root: Path | str | None = None,
    temp_root: Path | str | None = None,
) -> Optional[Path]:
    if not raw:
        return None

    path = Path(raw).expanduser()
    if path.is_absolute():
        return path

    if output_root and path.parts[:1] == ("output",):
        remainder = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
        return (Path(output_root).expanduser() / remainder).resolve()

    if temp_root and path.parts[:1] == ("temp",):
        remainder = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
        return (Path(temp_root).expanduser() / remainder).resolve()

    return (workspace_root / path).resolve()


def path_is_within_root(path: Path | str, root: Path | str) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
    except ValueError:
        return False
    return True


def resolve_app_owned_sources_root(
    workspace_root: Path | str,
    db_path: Path | str | None = None,
) -> Path:
    """Return the app-owned durable sources directory for the active database."""
    root = Path(workspace_root).expanduser().resolve()
    if db_path is not None:
        return (Path(db_path).expanduser().resolve().parent / "sources").resolve()
    for parent_name in APP_OWNED_DB_PARENT_NAMES:
        parent = root / parent_name
        if parent.is_dir():
            return (parent / "sources").resolve()
    return (root / "data" / "sources").resolve()


def canonical_sources_dir(db_path: Path | str) -> Path:
    """Return ``{db_parent}/sources`` for the database file location."""
    return Path(db_path).expanduser().resolve().parent / "sources"


def is_app_owned_durable_source(path: Path | str, sources_root: Path | str) -> bool:
    """True when ``path`` resolves inside the app-owned durable sources root."""
    return path_is_within_root(path, sources_root)


def unique_destination_path(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path

    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1

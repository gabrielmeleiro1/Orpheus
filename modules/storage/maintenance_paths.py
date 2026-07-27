"""
Shared path resolution and connection helpers for storage maintenance.

Used by the audit, reconciliation and prune halves of
:mod:`modules.storage.maintenance`, which re-exports everything here.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Optional

from modules.storage.migrations import BUSY_TIMEOUT_MS


def _connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a maintenance-owned SQLite connection.

    Maintenance bypasses the repository's shared connection/lock, so it must
    set its own ``busy_timeout``; otherwise a maintenance read/backup taken
    while a conversion holds the write lock fails immediately with
    'database is locked'.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn

# Output sub-directories owned by the maintenance tooling itself (e.g. prune
# report JSON). These must never be treated as orphan/stale artifacts, otherwise
# a later prune run would delete the prior run's own audit history.
RESERVED_OUTPUT_DIR_NAMES = frozenset({"reports"})

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

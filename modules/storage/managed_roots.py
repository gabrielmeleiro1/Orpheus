"""Durable bindings between managed files and the database allowed to remove them."""

from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
from typing import BinaryIO

from modules.contracts.durable_files import atomic_write_json
from modules.storage.db_ownership import canonical_database_path, open_process_lock, same_database_file
from modules.storage.maintenance_paths import workspace_root_for_db
from modules.storage.migrations import remap_stored_paths, store_managed_roots
from modules.storage.permissions import owner_only

STORAGE_OWNER_NAME = ".orpheus-storage-owner.json"
_claims_lock = threading.Lock()
_claim_handles: set[BinaryIO] = set()


class StorageOwnershipError(RuntimeError):
    """Managed files are already associated with another library."""


def _database_identity(database: Path | str) -> Path:
    # Callers pass the canonical path whose database lease they already hold.
    # If that path (or an ancestor) was replaced by an alias, never redirect a
    # live repository's storage permissions to the alias's new database.
    identity = Path(os.path.normcase(os.path.abspath(Path(database).expanduser())))
    if canonical_database_path(identity) != identity:
        raise StorageOwnershipError("Managed storage requires the owned canonical database path")
    return identity


def _owner(marker: Path) -> tuple[Path, str | None]:
    try:
        if marker.is_symlink():
            raise ValueError("owner record is a link")
        record = json.loads(marker.read_text(encoding="utf-8"))
        database = Path(record["database"])
        version = record["version"]
        library_id = record.get("library_id") if version == 2 else None
        if (version not in (1, 2) or not database.is_absolute()
                or (version == 2 and (not isinstance(library_id, str) or not library_id))):
            raise ValueError("invalid owner record")
        # This was canonicalized when the claim was published. Resolving it
        # again could transfer ownership if that filename becomes a symlink.
        return Path(os.path.normcase(os.path.normpath(str(database)))), library_id
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StorageOwnershipError(f"Cannot verify managed storage ownership: {marker}") from error


def _default_roots(database: Path) -> tuple[Path, ...]:
    workspace = workspace_root_for_db(database)
    return database.parent / "sources", workspace / "output", workspace / "temp"


def _legacy_layout_matches(root: Path, owner: Path, database: Path) -> bool:
    if root not in _default_roots(database):
        return False
    if owner.parent == database.parent:
        return True
    if owner.name != database.name:
        return False
    if owner.parent.name != database.parent.name and not (
        workspace_root_for_db(owner) == owner.parent and workspace_root_for_db(database) == database.parent
    ):
        return False
    # A whole-layout move leaves none of the original default roots behind.
    # An orphaned database alone cannot authorize taking their files elsewhere.
    for original in _default_roots(owner):
        try:
            original.lstat()
        except FileNotFoundError:
            continue
        return False
    return True


def _check_owner(marker: Path, database: Path, library_id: str | None = None) -> bool:
    owner, stored_id = _owner(marker)
    # A stored canonical filename must not gain a new target through a link.
    if canonical_database_path(owner) == owner:
        if same_database_file(owner, database):
            return library_id is not None and stored_id != library_id
        try:
            owner.lstat()
        except FileNotFoundError:
            lockfile = Path(str(owner) + ".lock")
            inactive = True
            if lockfile.exists() or lockfile.is_symlink():
                if lockfile.is_symlink():
                    inactive = False
                else:
                    try:
                        handle = open_process_lock(lockfile, blocking=False)
                    except OSError:
                        inactive = False
                    else:
                        handle.close()
            if inactive and library_id is not None:
                if stored_id == library_id:
                    return True
                if stored_id is None and _legacy_layout_matches(marker.parent, owner, database):
                    return True
    raise StorageOwnershipError(
        f"Managed storage at {marker.parent} belongs to {owner}. "
        "Use separate data, output and temporary directories for each library."
    )


def _check_overlaps(root: Path, database: Path, *, library_id: str | None = None) -> set[Path]:
    # Resolving the root handles directory aliases. Never follow links while
    # inspecting descendants: ownership follows their canonical targets instead.
    rewrites = set()
    for ancestor in (root, *root.parents):
        marker = ancestor / STORAGE_OWNER_NAME
        if marker.exists() or marker.is_symlink():
            if _check_owner(marker, database, library_id):
                rewrites.add(marker)
    if root.is_dir():
        def unreadable(error: OSError) -> None:
            raise StorageOwnershipError(f"Cannot inspect managed storage ownership: {root}") from error

        for directory, directories, filenames in os.walk(root, followlinks=False, onerror=unreadable):
            directories[:] = [name for name in directories if not (Path(directory) / name).is_junction()]
            if STORAGE_OWNER_NAME in filenames:
                marker = Path(directory) / STORAGE_OWNER_NAME
                if _check_owner(marker, database, library_id):
                    rewrites.add(marker)
    return rewrites


@contextmanager
def _claim_gate():
    # A library-local gate cannot serialize overlapping roots used by different
    # libraries. Keep one persistent gate for this user's cooperating processes,
    # independent of HOME, TMPDIR, AUDIOBOOK_DATA_DIR and the selected roots.
    with _claims_lock:
        directory = _claim_gate_directory()
        if any(path.is_symlink() or path.is_junction() for path in (directory, *directory.parents)):
            raise StorageOwnershipError("Storage claim lock directory must not be a link")
        # Windows Python 3.12.4+ turns 0700 into an ACL for the creating token's
        # owner only. A sandboxed process (for example, a process running under a
        # sandbox's restricted token) then locks the real account out of its own gate. The profile's Temp ACL is
        # already per-user, so inherit it there instead.
        directory.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
        if os.name != "nt" and directory.stat().st_uid != os.getuid():
            raise StorageOwnershipError("Storage claim lock directory belongs to another user")
        owner_only(directory, directory=True)
        gate = directory / "storage-claims.lock"
        if gate.is_symlink() or gate.is_junction():
            raise StorageOwnershipError("Storage claim lock must not be a link")
        opened = []

        def register(handle):
            opened.append(handle)
            _claim_handles.add(handle)

        try:
            open_process_lock(gate, blocking=True, on_open=register)
            yield
        finally:
            for handle in opened:
                _claim_handles.discard(handle)
                handle.close()


def _claim_gate_directory() -> Path:
    """Use the OS account's temporary storage, not environment overrides."""
    if os.name != "nt":
        # macOS exposes the system temp directory through /tmp -> /private/tmp.
        return Path("/tmp").resolve() / f"orpheus-storage-claims-{os.getuid()}"
    import ctypes
    from ctypes import wintypes

    # Shell32 known-folder calls can expand USERPROFILE from the environment
    # on their first invocation and cache that result. Read the account profile
    # through the process token instead, so every launch uses the same gate.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise StorageOwnershipError("Cannot locate the storage claim lock directory") from ctypes.WinError(ctypes.get_last_error())
    try:
        profile = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(profile))
        if not userenv.GetUserProfileDirectoryW(token, profile, ctypes.byref(size)):
            raise StorageOwnershipError("Cannot locate the storage claim lock directory") from ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(token)
    if not profile.value:
        raise StorageOwnershipError("Cannot locate the storage claim lock directory")
    return Path(profile.value) / "AppData" / "Local" / "Temp" / "Orpheus" / "storage-claims"


def _adopted_root(marker: Path, owner: Path, database: Path, recorded_root: str | None) -> Path | None:
    if owner == database or same_database_file(owner, database):
        return None
    if recorded_root is not None:
        return Path(recorded_root)
    for old, new in zip(_default_roots(owner), _default_roots(database)):
        if marker.parent == new:
            return old
    # Older markers omitted the configured root. A root moved with the
    # workspace retains its relative location there.
    try:
        relative = marker.parent.relative_to(workspace_root_for_db(database))
    except ValueError:
        return None
    return workspace_root_for_db(owner) / relative


def _root_history(marker: Path, database: Path) -> list[Path]:
    owner, _ = _owner(marker)
    record = json.loads(marker.read_text(encoding="utf-8"))
    previous = record.get("previous_roots", [])
    recorded_root = record.get("root")
    if (not isinstance(previous, list)
            or any(not isinstance(raw, str) or not Path(raw).is_absolute() for raw in previous)
            or (recorded_root is not None and (
                not isinstance(recorded_root, str) or not Path(recorded_root).is_absolute()
            ))):
        raise StorageOwnershipError(f"Cannot verify managed storage history: {marker}")
    history = [Path(raw) for raw in previous]
    adopted = _adopted_root(marker, owner, database, recorded_root)
    if adopted is not None:
        history.append(adopted)
    return list(dict.fromkeys(path for path in history if path != marker.parent))


def stored_path_remappings(database: Path, *roots: Path) -> list[tuple[Path, Path]]:
    """Read previous locations retained by this library's current markers."""
    mappings = []
    # Marker replacement is atomic. Readers may already hold a SQLite write
    # transaction, so they must not wait on the gate used by remapping writers.
    for root in roots:
        marker = root / STORAGE_OWNER_NAME
        if not marker.exists():
            continue
        owner, _ = _owner(marker)
        if owner != database:
            raise StorageOwnershipError(f"Managed storage at {root} belongs to {owner}")
        mappings.extend((old, root) for old in _root_history(marker, database))
    return mappings


def _claim_roots(
    database: Path, roots: tuple[Path, ...], library_id: str | None = None,
    connection: sqlite3.Connection | None = None, *, temp_root: Path | None = None,
    layout: tuple[Path, ...] | None = None,
) -> None:
    # Inspect every requested root before publishing any new claims. The gate
    # prevents another process slipping a nested claim between these checks and
    # publication; a losing claimant therefore never leaves a conflicting record.
    rewrites = set()
    for root in roots:
        rewrites.update(_check_overlaps(root, database, library_id=library_id))
    markers = rewrites | {root / STORAGE_OWNER_NAME for root in roots}
    records = {}
    mappings = []
    for marker in markers:
        history = _root_history(marker, database) if marker.exists() else []
        record = {"version": 1, "database": str(database)}
        if library_id is not None:
            record.update(version=2, library_id=library_id, root=str(marker.parent),
                          previous_roots=[str(path) for path in history])
        records[marker] = record
        if marker in rewrites:
            owner, _ = _owner(marker)
            old = _adopted_root(
                marker, owner, database, json.loads(marker.read_text(encoding="utf-8")).get("root"),
            )
            if old is not None and old != marker.parent:
                mappings.append((old, marker.parent))
    # Commit paths and layout together so backups retain their own roots.
    if mappings or layout is not None:
        if connection is not None:
            with connection:
                remap_stored_paths(connection, mappings)
                if layout is not None:
                    store_managed_roots(connection, layout)
        else:
            conn = sqlite3.connect(database)
            try:
                with conn:
                    remap_stored_paths(conn, mappings)
                    if layout is not None:
                        store_managed_roots(conn, layout)
            finally:
                conn.close()
        if mappings and temp_root is not None:
            from modules.storage.library_relocation import rebase_delete_transactions

            rebase_delete_transactions(temp_root, roots, mappings)
    # Old markers allow retrying this location; a further move requires a successful reopen.
    for marker in rewrites:
        atomic_write_json(marker, records[marker])
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        marker = root / STORAGE_OWNER_NAME
        if not marker.exists():
            atomic_write_json(marker, records[marker])
        elif library_id is not None and "root" not in json.loads(marker.read_text(encoding="utf-8")):
            atomic_write_json(marker, records[marker])
        # Every descendant/ancestor was inspected above while holding the
        # process gate. Publishing our own marker cannot add a conflicting
        # owner, so verify that record without walking the whole tree again.
        _check_owner(marker, database, library_id)


def claim_managed_roots(database: Path | str, *roots: Path | str, library_id: str | None = None) -> None:
    """Bind roots permanently, including when the owning process is stopped.

    Complete records are published under one cross-process claim gate. Records
    remain after close and process death; deleting an old database does not
    authorize deleting its managed files. Publication needs no hard-link support
    on the managed filesystem, including removable output drives.
    """
    database = _database_identity(database)
    resolved = tuple(dict.fromkeys(Path(root).expanduser().resolve() for root in roots))
    with _claim_gate():
        _claim_roots(database, resolved, library_id)


def claim_sources_root(
    database: Path | str, root: Path | str, *, library_id: str | None = None,
    connection: sqlite3.Connection | None = None, additional_roots: tuple[Path, ...] = (),
    temp_root: Path | None = None,
) -> None:
    """Refuse ambiguous legacy source trees before assigning their first owner."""
    database = _database_identity(database)
    root = Path(root).expanduser().resolve()
    with _claim_gate():
        if root.is_dir() and not (root / STORAGE_OWNER_NAME).exists() and any(root.iterdir()):
            peers = [
                path for path in database.parent.iterdir()
                if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
                and path.is_file() and not same_database_file(canonical_database_path(path), database)
            ]
            if peers:
                raise StorageOwnershipError(
                    f"Cannot assign existing sources at {root}: multiple library databases share this directory. "
                    "Keep these files intact and configure separate storage for each library."
                )
        layout = (root, *(path.expanduser().resolve() for path in additional_roots))
        resolved = tuple(dict.fromkeys(layout))
        _claim_roots(
            database, resolved, library_id, connection, temp_root=temp_root,
            layout=layout if library_id is not None and len(layout) == 3 else None,
        )


def _after_fork() -> None:
    global _claims_lock
    # A forked child must neither retain its parent's gate nor inherit a locked
    # thread mutex. Closing without LOCK_UN leaves the parent's lock intact.
    for handle in _claim_handles:
        handle.close()
    _claim_handles.clear()
    _claims_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)

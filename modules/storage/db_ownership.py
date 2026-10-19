"""Process ownership and shared thread exclusion for a canonical database path."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time as time
from typing import BinaryIO
import weakref

from modules.contracts.process_locks import open_process_lock as open_process_lock

_directory_handles: set[BinaryIO] = set()
DIRECTORY_GATE_NAME = ".orpheus-database-locks"


@contextmanager
def _directory_gate(directory: Path):
    """Serialize acquiring or retiring database lock inodes in one directory."""
    opened = []

    def register(handle):
        opened.append(handle)
        _directory_handles.add(handle)

    try:
        open_process_lock(directory / DIRECTORY_GATE_NAME, blocking=True, on_open=register)
        yield
    finally:
        for handle in opened:
            _directory_handles.discard(handle)
            handle.close()


def canonical_database_path(path: Path | str) -> Path:
    supplied = Path(path).expanduser()
    if supplied.is_symlink() and supplied.with_name(supplied.name + ".restore-rollback.json").exists():
        raise ValueError("Pending restore rollback database must not be a link")
    return Path(os.path.normcase(str(supplied.resolve())))


def same_database_file(first: Path, second: Path) -> bool:
    """Compare identical paths and verified case-only aliases."""
    first_name = os.path.normpath(first)
    second_name = os.path.normpath(second)
    if first_name == second_name:
        return True
    if os.path.normcase(first_name).casefold() != os.path.normcase(second_name).casefold():
        return False
    try:
        # Two directory entries can be hard links on a case-sensitive volume.
        for path in (first, second):
            # Case-only ancestor aliases must still identify a single entry.
            if path.stat().st_nlink != 1:
                return False
            if sum(name.casefold() == path.name.casefold() for name in os.listdir(path.parent)) != 1:
                return False
        return os.path.samefile(first, second)
    except OSError:
        return False


class _Owner:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with _directory_gate(path.parent):
            self.handle = open_process_lock(Path(str(path) + ".lock"), blocking=False)
        self.lock = threading.RLock()
        self.references = 0
        self.pid = os.getpid()


_registry: dict[Path, _Owner] = {}
_registry_lock = threading.RLock()


def _release(owner: _Owner) -> None:
    if owner.pid != os.getpid():
        return
    with _registry_lock:
        owner.references -= 1
        if owner.references == 0:
            owner.handle.close()
            del _registry[owner.path]


class DatabaseOwnership:
    """A lease retained until its repository, connection or launcher closes."""

    def __init__(self, owner: _Owner):
        self.path = owner.path
        self.lock = owner.lock
        self._finalizer = weakref.finalize(self, _release, owner)
        self._finalizer.atexit = False

    def close(self) -> None:
        self._finalizer()

    @property
    def active(self) -> bool:
        return self._finalizer.alive and self.path in _registry

    def __enter__(self) -> DatabaseOwnership:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def acquire_database(db_path: Path | str) -> DatabaseOwnership:
    """Resolve once and share one OS handle among this process's openers."""
    path = canonical_database_path(db_path)
    with _registry_lock:
        owner = _registry.get(path)
        if owner is None:
            # An alias may share a lease only while its stored path still
            # identifies the file we locked, rather than a retargeted link.
            owner = next((
                entry for entry in _registry.values()
                if canonical_database_path(entry.path) == entry.path and same_database_file(entry.path, path)
            ), None)
        if owner is None:
            owner = _Owner(path)
            _registry[path] = owner
        owner.references += 1
        return DatabaseOwnership(owner)


def retire_database_file(db_path: Path) -> bool:
    """Remove an inactive backup and its lock without splitting ownership.

    Every cooperating opener acquires the persistent directory gate first.
    Holding it across close/unlink prevents another process acquiring the old
    inode between retirement and deletion of the per-database lock file.
    """
    if db_path.is_symlink():
        return False
    path = canonical_database_path(db_path)
    with _registry_lock, _directory_gate(path.parent):
        if any(same_database_file(owned, path) for owned in _registry):
            return False
        lockfile = Path(str(path) + ".lock")
        try:
            handle = open_process_lock(lockfile, blocking=False)
        except BlockingIOError:
            return False
        try:
            path.unlink(missing_ok=True)
        finally:
            handle.close()
        lockfile.unlink()
        return True


def _after_fork() -> None:
    global _registry_lock
    # Inherited descriptors must not make a child an owner or prolong a dead
    # parent's ownership. Closing, without LOCK_UN, leaves the parent locked.
    for owner in _registry.values():
        owner.handle.close()
    for handle in _directory_handles:
        handle.close()
    _directory_handles.clear()
    _registry.clear()
    _registry_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
